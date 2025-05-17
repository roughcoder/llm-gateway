import os
# import httpx # No longer using httpx directly
import logging
import json
from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse
from typing import Any, Dict, AsyncGenerator, List, Optional, Union
from pydantic import BaseModel, Field
import socket
import time # Import time for potential delays if needed, though not used in check
from urllib.parse import urlparse # Import urlparse
import urllib.request
import asyncio # For background tasks and Redis
import hashlib # For cache key generation
import redis.asyncio as aioredis # For Redis connection

# Import OTel trace API needed for get_current_span
from opentelemetry import trace

# Import OpenAI SDK
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

# Import context manager for adding attributes
from openinference.instrumentation import using_attributes

log = logging.getLogger(__name__)

# --- Log Environment Variables ---
log.info("--- Dumping Environment Variables (Masking sensitive keywords) ---")
sensitive_keywords = ["KEY", "SECRET", "PASSWORD", "TOKEN", "CONN_STR"]
try:
    # Sort items for consistent logging order
    for key, value in sorted(os.environ.items()):
        is_sensitive = any(keyword in key.upper() for keyword in sensitive_keywords)
        if is_sensitive:
            log.info(f"ENV: {key}=********")
        else:
            log.info(f"ENV: {key}={value}")
except Exception as e:
    log.error(f"Error dumping environment variables: {e}")
log.info("--- Finished Dumping Environment Variables ---")

# --- Original Phoenix Client Initialization Logic --- 
# (This section should ideally use PHOENIX_BASE_URL env var as implemented previously, 
# but keeping it separate as requested for this temporary test)
# Read the base URL from environment variable, fallback to default
phoenix_base_url = os.environ.get("PHOENIX_BASE_URL", "https://phoenix.infinitestack.io")
log.info(f"Target Phoenix base URL configured as: {phoenix_base_url}")

# --- Import and Initialize Phoenix Client --- 
try:
    from phoenix.client import Client as PhoenixClient
    # Initialize Phoenix client using the configured URL
    log.info(f"Initializing PhoenixClient with base_url: {phoenix_base_url}")
    phoenix_client = PhoenixClient(base_url=phoenix_base_url)
    phoenix_available = True
except ImportError:
    phoenix_client = None
    phoenix_available = False
    logging.getLogger(__name__).warning("arize-phoenix-client library not installed. Dynamic prompt endpoint will not work.")
# --------------------------

# --- Initialize Redis Client ---
# Read Redis URL from environment variable, fallback to default localhost
redis_url = os.environ.get("REDIS_URL", "redis://localhost")
log.info(f"Connecting to Redis at: {redis_url}")
try:
    redis_client = aioredis.from_url(redis_url, decode_responses=True) # decode_responses=True for string keys/values
    log.info("Successfully connected to Redis.")
except Exception as e:
    log.error(f"Failed to connect to Redis: {e}", exc_info=True)
    redis_client = None # Set to None to indicate failure, handle in endpoint
# -----------------------------

LOCK_TTL_SECONDS = int(os.environ.get("REDIS_LOCK_TTL_SECONDS", 60)) # Time-to-live for distributed locks

# log = logging.getLogger(__name__) # Original position - REMOVED
router = APIRouter()

# OPENAI_API_BASE_URL = "https://api.openai.com/v1" # SDK handles base URL

# Initialize the client once at the module level.
# This assumes instrumentation in main.py has already run via import side effects.
log.info("Initializing module-level OpenAI SDK client")
try:
    client = AsyncOpenAI()
    # SDK automatically uses OPENAI_API_KEY env var
    log.info("Module-level OpenAI SDK client initialized successfully.")
except Exception as e:
    log.exception("Failed to initialize module-level OpenAI SDK client during import")
    # Raise or handle appropriately - perhaps set client to None and check in endpoint?
    # For now, let the exception propagate to prevent app startup on critical failure.
    raise e

# --- Pydantic Models for Request Body ---
# Remove or comment out the old request model
# class OpenAIChatMessage(BaseModel):
#     role: str
#     content: str

# class OpenAIChatCompletionRequest(BaseModel):
#     # Core OpenAI fields
#     model: str
#     messages: List[OpenAIChatMessage] # This will be replaced by variables
#     stream: Optional[bool] = False
#     temperature: Optional[float] = None
#     max_tokens: Optional[int] = None
#     # ... (rest of the old model)

# New Pydantic model for dynamic prompt requests
class OpenAIPromptCompletionRequest(BaseModel):
    # Core OpenAI fields (model comes from prompt, but can be overridden)
    model: Optional[str] = None # Model can now be optional, fetched from prompt or overridden
    variables: Dict[str, Any] = Field(..., description="Dictionary of variables to fill the prompt template.")
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None

    # --- Context fields for Tracing ---
    session_id: Optional[str] = Field(None, description="Session ID for grouping related traces.")
    user_id: Optional[str] = Field(None, description="User ID associated with the request.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Arbitrary key-value metadata for the trace.")
    tags: Optional[List[str]] = Field(None, description="List of tags to add to the trace.")
    # Note: prompt_template fields are less relevant now as the template comes from Phoenix
    # Keep them if you want to log overrides or additional context.
    # prompt_template: Optional[str] = Field(None, description="The raw prompt template string used (if any).")
    # prompt_template_version: Optional[str] = Field(None, description="Version identifier for the prompt template.")
    # prompt_template_variables: Optional[Dict[str, Any]] = Field(None, description="Dictionary of variables used to fill the prompt template.") # This is now 'variables'

    # Allow other fields supported by OpenAI API (like functions, tools, etc.)
    class Config:
        extra = 'allow'

# --- Helper function for custom SSE events ---
def generate_custom_sse_event(cache_key: str, cache_hit: bool, event_type: str, message: str) -> bytes:
    """Generates a Server-Sent Event string with the custom JSON payload."""
    payload = {
        "id": cache_key,
        "cacheHit": cache_hit,
        "event": event_type,
        "message": message
    }
    json_payload = json.dumps(payload)
    sse_event_string = f"data: {json_payload}\n\n"
    return sse_event_string.encode('utf-8')

# --- Streaming Generators ---
# sdk_content_stream_generator is removed as we are creating a custom SSE format

# sdk_sse_stream_generator is removed as we are creating a custom SSE format

# --- Endpoint ---
# Correct the path: remove the redundant /providers/openai prefix
@router.post("/{prompt_name}/{tag}",
            status_code=200,
            summary="Proxy OpenAI Chat Completions using a dynamic Phoenix Prompt",
            description="""
            Fetches a prompt template from Arize Phoenix by name and tag, formats it with variables, 
            and calls OpenAI's chat completions API using the SDK. Responses are always cached in Redis.
            
            ## Headers
            
            - **x-llm-stream**: (optional, default: false) Controls response mode.
                - If "true": Returns a Server-Sent Events stream (`Content-Type: text/event-stream`). Each event is a JSON object:
                  `{"id": "<cache_key>", "cacheHit": <true|false>, "event": "start"|"message"|"close", "message": "<content>"}`
                - If "false" or not provided: Returns a single JSON object (`Content-Type: application/json`):
                  `{"id": "<cache_key>", "cacheHit": <true|false>, "event": "finished", "message": "<full_text>"}`
            - **x-llm-cache**: (optional, default: true) Must be true for this endpoint version. If set to "false", it will be ignored or raise an error as all responses go via Redis.
            - **x-clear-cache**: (optional, default: false) Set to "true" to force refresh the cache for this request (results in `cacheHit: false`).
            
            ## Request Body
            
            - **model**: (optional) Override the model specified in the Phoenix prompt.
            - **variables**: (required) Dictionary of variables to fill in the prompt template.
            - **temperature**: (optional) Control the randomness of the response.
            - **max_tokens**: (optional) Limit the maximum number of tokens in the response.
            - **session_id**, **user_id**, **metadata**, **tags**: (optional) Fields for tracing and grouping requests.
            
            Additional OpenAI parameters like functions, tools, etc. are also supported.
            
            ## Response Details
            
            - **id**: The Redis cache key for the request.
            - **cacheHit**: Boolean indicating if a pre-existing, completed cache entry was used (`true`) or if the response was generated fresh (`false`).
            - **event**: 
                - For streaming: `start` (stream begins), `message` (a chunk of text), `close` (stream ends).
                - For non-streaming: `finished` (complete response).
            - **message**: 
                - For `start`/`close`/`finished` events: Informational message or the full text.
                - For `message` events: A chunk of the generated text.
            """)
async def proxy_openai_prompt_completions_sdk( # Rename function for clarity
    prompt_name: str,
    tag: str,
    payload: OpenAIPromptCompletionRequest, # Use the new request model
    request: Request, # Add Request to access headers
):
    """
    Dynamic prompt endpoint that retrieves a prompt from Phoenix, formats it with variables, and sends it to OpenAI.
    
    Path Parameters:
    - **prompt_name**: The name of the prompt template in Phoenix
    - **tag**: The tag of the prompt template version to use
    
    Headers:
    - **x-llm-stream**: Set to "true" to enable streaming (default: "false")
    - **x-llm-cache**: Set to "false" to disable caching (default: "true")
    - **x-clear-cache**: Set to "true" to invalidate and refresh the cache (default: "false")
    
    The endpoint supports both streaming and non-streaming responses, and includes
    automatic caching for improved performance and reduced API costs.
    """
    # Extract header values with defaults
    use_streaming = request.headers.get("x-llm-stream", "false").lower() == "true"
    use_caching = request.headers.get("x-llm-cache", "true").lower() == "true"
    force_refresh = request.headers.get("x-clear-cache", "false").lower() == "true"
    
    # Check if OpenAI client initialization failed during import
    if client is None:
         log.error("OpenAI client was not initialized.")
         # For streaming, we should try to send a structured error if possible
         if use_streaming:
             # Cannot generate cache_key here easily if Phoenix fails or is bypassed
             # So, send a generic error, not our custom SSE format if cache_key is unknown
             return StreamingResponse(iter([f"data: {json.dumps({'error': 'OpenAI client not available'})}\n\n"]), media_type="text/event-stream", status_code=500)
         raise HTTPException(status_code=500, detail="OpenAI client is not available")

    # Check if Phoenix client is available
    if not phoenix_available or phoenix_client is None:
        log.error("Phoenix client is not available (arize-phoenix-client likely not installed).")
        if use_streaming:
            return StreamingResponse(iter([f"data: {json.dumps({'error': 'Dynamic prompt backend not available'})}\n\n"]), media_type="text/event-stream", status_code=501)
        raise HTTPException(status_code=501, detail="Dynamic prompt functionality is not available.")

    effective_redis_client = redis_client # All paths now go through Redis
    if effective_redis_client is None:
        log.error("Redis client is not available. This service requires Redis.")
        # This is a critical failure for the new design
        if use_streaming:
             return StreamingResponse(iter([f"data: {json.dumps({'error': 'Cache service (Redis) not available'})}\n\n"]), media_type="text/event-stream", status_code=503)
        raise HTTPException(status_code=503, detail="Cache service (Redis) is not available. All responses are cached.")

    # --- Prepare for Cache Key and Phoenix --- 
    key_model = payload.model 

    try:
        log.debug("Fetching prompt from Phoenix", prompt_name=prompt_name, tag=tag)
        phoenix_prompt = phoenix_client.prompts.get(prompt_identifier=prompt_name, tag=tag)
        log.info("Successfully fetched prompt from Phoenix")
        sanitized_variables = {k: (v if v is not None else "") for k, v in payload.variables.items()}
        formatted_prompt_dict = phoenix_prompt.format(variables=sanitized_variables)
        if key_model is None: key_model = formatted_prompt_dict.get('model')
    except Exception as e:
        log.error(f"Failed to fetch or format prompt from Phoenix: {e}", exc_info=True)
        detail_msg = f"Could not retrieve or format prompt '{prompt_name}' with tag '{tag}': {e}"
        if use_streaming: # Try to send SSE error
             # Attempt to form a dummy cache key for error reporting consistency if possible
             # This is best-effort; if key_model is still None, the ID will reflect that.
             temp_cache_key_for_error = generate_cache_key(prompt_name, tag, payload.variables, key_model, payload.temperature, payload.max_tokens)
             error_event = generate_custom_sse_event(temp_cache_key_for_error, False, "close", f"Error: {detail_msg}")
             return StreamingResponse(iter([error_event]), media_type="text/event-stream", status_code=404)
        raise HTTPException(status_code=404, detail=detail_msg)

    # --- Generate Cache Key (guaranteed to have key_model or fail above) ---
    if not key_model: # Should be caught by Phoenix fetch logic if not in payload
        err_msg = "Model not found in Phoenix prompt or payload."
        log.error(err_msg)
        # This path should ideally not be hit if Phoenix logic is robust
        temp_cache_key_for_error = generate_cache_key(prompt_name, tag, payload.variables, None, payload.temperature, payload.max_tokens)
        if use_streaming:
            error_event = generate_custom_sse_event(temp_cache_key_for_error, False, "close", f"Error: {err_msg}")
            return StreamingResponse(iter([error_event]), media_type="text/event-stream", status_code=400)
        raise HTTPException(status_code=400, detail=err_msg)

    cache_key = generate_cache_key(
        prompt_name=prompt_name, tag=tag, variables=payload.variables,
        model=key_model, temperature=payload.temperature, max_tokens=payload.max_tokens
    )
    log.info(f"Generated cache key: {cache_key}")

    # --- Cache Invalidation ---
    if force_refresh:
        log.info(f"Force refresh requested for cache key: {cache_key}. Deleting cache.")
        await effective_redis_client.delete(cache_key) 
        await effective_redis_client.delete(f"{cache_key}:status")
        await effective_redis_client.delete(f"{cache_key}:meta")

    # --- Determine Cache Hit Status --- 
    cache_hit_status = False
    initial_cache_status = await effective_redis_client.get(f"{cache_key}:status")
    if not force_refresh and (initial_cache_status == "done" or initial_cache_status == "in_progress"):
        cache_hit_status = True
    log.info(f"Cache key: {cache_key}, Initial status: {initial_cache_status}, Force refresh: {force_refresh}, Determined cacheHit: {cache_hit_status}")

    # --- Tracing Attributes (common for both streaming/non-streaming after this point) ---
    current_model_for_call = key_model # This is the model associated with the cache_key
    using_attributes_params = {
        "session_id": payload.session_id, "user_id": payload.user_id,
        "metadata": payload.metadata, "tags": payload.tags,
    }
    filtered_using_attributes = {k: v for k, v in using_attributes_params.items() if v is not None}
    manual_span_attributes = {
        "llm.request.type": "chat", "llm.prompt_template.name": prompt_name,
        "llm.prompt_template.tag": tag, "llm.prompt_template.version": phoenix_prompt.id,
        "llm.prompt_template.variables": payload.variables, "llm.request.model": current_model_for_call,
        "llm.cache.hit": cache_hit_status, "llm.cache.key": cache_key,
        "llm.stream.enabled": use_streaming,
    }
    filtered_manual_attributes = {k: v for k, v in manual_span_attributes.items() if v is not None}

    # --- Main Logic: All responses go through Redis --- 
    # Step 1: If not a cache hit, or cache is incomplete, start OpenAI call and stream_to_redis task.
    if not cache_hit_status or initial_cache_status == "in_progress": # in_progress means we join, but OpenAI call might still be needed if first requester failed
        
        # Check if another process is already handling this (relevant for "in_progress" or very fast subsequent requests)
        # This is a simplified lock; a more robust distributed lock might use SET NX EX.
        lock_key = f"{cache_key}:lock"
        if await effective_redis_client.set(lock_key, "1", ex=LOCK_TTL_SECONDS, nx=True):
            log.info(f"Acquired lock for {cache_key}. Proceeding with OpenAI call.")
            # This process is the first to try and populate the cache for this key.
            # Prepare payload for OpenAI SDK
            sdk_payload_dict = {**formatted_prompt_dict}
            if payload.model: sdk_payload_dict['model'] = payload.model # User override
            # Ensure model is present (should be from Phoenix or payload by now)
            if 'model' not in sdk_payload_dict: 
                 # This should be an extreme edge case given earlier checks
                 m_err = "Critical: Model unexpectedly missing before OpenAI call."
                 log.error(m_err, cache_key=cache_key)
                 await effective_redis_client.delete(lock_key) # Release lock
                 # Send error (SSE or HTTP)
                 error_event_msg = generate_custom_sse_event(cache_key, cache_hit_status, "close", f"Error: {m_err}")
                 if use_streaming: return StreamingResponse(iter([error_event_msg]), media_type="text/event-stream", status_code=500)
                 else: return Response(content=json.dumps({"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": f"Error: {m_err}"}), media_type="application/json", status_code=500)

            current_model_for_call = sdk_payload_dict['model'] # Update if overridden for the actual call
            if payload.temperature is not None: sdk_payload_dict['temperature'] = payload.temperature
            if payload.max_tokens is not None: sdk_payload_dict['max_tokens'] = payload.max_tokens
            exclude_keys = {'variables', 'session_id', 'user_id', 'metadata', 'tags', 'model', 'temperature', 'max_tokens'}
            extra_params = payload.model_dump(exclude_unset=True, exclude=exclude_keys)
            sdk_payload_dict.update(extra_params)
            sdk_payload_dict['stream'] = True # Always stream from OpenAI for caching

            try:
                with using_attributes(**filtered_using_attributes):
                    span = trace.get_current_span()
                    for key, value in filtered_manual_attributes.items(): span.set_attribute(key, value)
                    # Update span with actual model being called if it changed from key_model
                    if current_model_for_call != key_model: span.set_attribute("llm.request.model", current_model_for_call)

                    log.debug("Calling OpenAI SDK chat.completions.create (streaming for cache)", payload=sdk_payload_dict)
                    openai_sdk_stream_response = await client.chat.completions.create(**sdk_payload_dict)
                
                initial_status_set_event = asyncio.Event()
                asyncio.create_task(stream_to_redis(
                    cache_key, openai_sdk_stream_response, current_model_for_call,
                    initial_status_set_event=initial_status_set_event,
                    redis_client_for_stream=effective_redis_client,
                    lock_key_to_release=lock_key # Pass lock key to be released by stream_to_redis
                ))
                try:
                    await asyncio.wait_for(initial_status_set_event.wait(), timeout=10.0) # Increased timeout
                except asyncio.TimeoutError:
                    log.error(f"Timeout waiting for stream_to_redis initial status for {cache_key}")
                    # Lock will be released by stream_to_redis eventually or expire.
                    # Send error (SSE or HTTP)
                    error_msg_timeout = "Error: Cache population timed out waiting for initial status."
                    error_event_timeout = generate_custom_sse_event(cache_key, cache_hit_status, "close", error_msg_timeout)
                    if use_streaming: return StreamingResponse(iter([error_event_timeout]), media_type="text/event-stream", status_code=500)
                    else: return Response(content=json.dumps({"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": error_msg_timeout}), media_type="application/json", status_code=500)

            except openai.APIStatusError as e:
                log.warning("OpenAI API returned an error", status_code=e.status_code, error=str(e))
                await effective_redis_client.set(f"{cache_key}:status", "error", ex=1800)
                await effective_redis_client.delete(lock_key) # Release lock on OpenAI error
                error_event_openai = generate_custom_sse_event(cache_key, cache_hit_status, "close", f"Error: OpenAI API Error - {e.status_code}: {str(e)}")
                if use_streaming: return StreamingResponse(iter([error_event_openai]), media_type="text/event-stream", status_code=e.status_code)
                else: return Response(content=json.dumps({"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": f"Error: OpenAI API Error - {e.status_code}: {str(e)}"}), media_type="application/json", status_code=e.status_code)
            except Exception as exc:
                log.exception("Unhandled internal server error during OpenAI SDK call setup or execution")
                await effective_redis_client.set(f"{cache_key}:status", "error", ex=1800)
                await effective_redis_client.delete(lock_key) # Release lock on general error
                error_msg_unhandled = f"Error: Internal server error - {str(exc)}"
                error_event_unhandled = generate_custom_sse_event(cache_key, cache_hit_status, "close", error_msg_unhandled)
                if use_streaming: return StreamingResponse(iter([error_event_unhandled]), media_type="text/event-stream", status_code=500)
                else: return Response(content=json.dumps({"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": error_msg_unhandled}), media_type="application/json", status_code=500)
        else:
            log.info(f"Another process is already populating cache for {cache_key} or lock acquisition failed. Will stream from Redis.")
            # If lock not acquired, another process is handling it. We just wait and stream from Redis.
            pass # Proceed to streaming/non-streaming response logic which reads from Redis.
    
    # Step 2: Serve response from Redis (either pre-existing or being filled by the background task)
    if use_streaming:
        async def event_stream_generator() -> AsyncGenerator[bytes, None]:
            log.info(f"Starting SSE event stream for {cache_key}, cacheHit: {cache_hit_status}")
            yield generate_custom_sse_event(cache_key, cache_hit_status, "start", "Stream started.")
            
            final_event_message = "Stream ended."
            error_occurred_in_stream = False
            try:
                async for sse_event_data in stream_custom_events_from_redis(
                    cache_key, cache_hit_status, effective_redis_client
                ):
                    # Check if the event from stream_custom_events_from_redis is a closing error event
                    try:
                        event_str = sse_event_data.decode('utf-8')
                        if event_str.startswith("data:"):
                            json_part = event_str[len("data:"):].strip()
                            parsed_event = json.loads(json_part)
                            if parsed_event.get("event") == "close" and "Error:" in parsed_event.get("message", ""):
                                log.warning(f"Error event received from stream_custom_events_from_redis for {cache_key}: {parsed_event.get('message')}")
                                final_event_message = parsed_event.get("message") # Propagate error message
                                error_occurred_in_stream = True
                                # We still yield this error event as it came from the source
                    except Exception as parsing_exc:
                        log.error(f"Error parsing event from stream_custom_events_from_redis: {parsing_exc}")
                        # Fallback, potentially a malformed event, but we should still try to close gracefully.

                    yield sse_event_data
                    if error_occurred_in_stream:
                        break # Stop processing further if a closing error was received from Redis stream
            except Exception as e_stream_gen:
                log.error(f"Error in event_stream_generator for {cache_key}: {e_stream_gen}", exc_info=True)
                final_event_message = f"Error: Stream generation failed - {str(e_stream_gen)}"
                error_occurred_in_stream = True # Mark error to ensure this is the final event if not already closed
                # Yield the error as a close event if one hasn't been sent from stream_custom_events_from_redis
                yield generate_custom_sse_event(cache_key, cache_hit_status, "close", final_event_message)
            
            # Only send a final "close" event if one wasn't already sent by stream_custom_events_from_redis due to its own error handling
            if not error_occurred_in_stream:
                log.info(f"Closing SSE event stream for {cache_key} with message: {final_event_message}")
                yield generate_custom_sse_event(cache_key, cache_hit_status, "close", final_event_message)
            else:
                log.info(f"SSE event stream for {cache_key} already closed due to an error: {final_event_message}")

        return StreamingResponse(
            event_stream_generator(), 
            media_type="text/event-stream",
            headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
        )
    else: # Non-streaming response
        log.info(f"Aggregating non-streaming response for {cache_key}, cacheHit: {cache_hit_status}")
        full_content_parts = []
        # Wait for the cache to be marked as "done" or "error"
        timeout_seconds = 60 # Max wait for non-streaming aggregation
        start_wait_time = time.time()
        final_cache_status = None
        while time.time() - start_wait_time < timeout_seconds:
            final_cache_status = await effective_redis_client.get(f"{cache_key}:status")
            if final_cache_status == "done" or final_cache_status == "error":
                break
            await asyncio.sleep(0.2) # Poll Redis status
        
        if final_cache_status == "done":
            all_content_parts = await effective_redis_client.lrange(cache_key, 0, -1)
            full_content = "".join(all_content_parts)
            response_payload = {"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": full_content}
            return Response(content=json.dumps(response_payload), media_type="application/json")
        elif final_cache_status == "error":
            err_msg = "Error: Response generation failed (marked as error in cache)."
            log.error(f"{err_msg} for cache key {cache_key}")
            response_payload = {"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": err_msg}
            return Response(content=json.dumps(response_payload), media_type="application/json", status_code=500)
        else: # Timeout or unexpected status
            err_msg = f"Error: Timeout or unexpected status ('{final_cache_status}') waiting for non-streaming response aggregation."
            log.error(f"{err_msg} for cache key {cache_key}")
            response_payload = {"id": cache_key, "cacheHit": cache_hit_status, "event": "finished", "message": err_msg}
            return Response(content=json.dumps(response_payload), media_type="application/json", status_code=500)

# Add other OpenAI endpoints as needed following the same proxy pattern 

# --- Cache Key Generation ---
def generate_cache_key(prompt_name: str, tag: str, variables: Dict[str, Any], model: Optional[str], temperature: Optional[float], max_tokens: Optional[int]) -> str:
    """Generates a deterministic cache key based on request parameters."""
    key_input = {
        "prompt_name": prompt_name,
        "tag": tag,
        "variables": variables,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    # Sort keys to ensure consistent hash, encode to bytes for hashing
    sorted_key_input = json.dumps(key_input, sort_keys=True).encode('utf-8')
    return "chat:" + hashlib.sha256(sorted_key_input).hexdigest()

# --- Redis Streaming Helper Functions ---
async def stream_to_redis(
    cache_key: str,
    stream: AsyncGenerator[ChatCompletionChunk, None],
    model_name: str,
    initial_ttl_seconds: int = 1800,
    initial_status_set_event: Optional[asyncio.Event] = None,
    redis_client_for_stream: Optional[aioredis.Redis] = None,
    lock_key_to_release: Optional[str] = None
):
    """Streams OpenAI content to Redis list, sets status, and releases lock."""
    actual_redis_client = redis_client_for_stream # Use passed client directly
    if actual_redis_client is None:
        log.error(f"Redis client not available for stream_to_redis (cache_key: {cache_key}). Cannot stream or release lock {lock_key_to_release}.")
        # Signal event if provided, so caller doesn't hang indefinitely, though it's an error state.
        if initial_status_set_event and not initial_status_set_event.is_set():
            initial_status_set_event.set() # Unblock waiter, but it will likely face an error.
        return

    status_set_successfully = False
    try:
        await actual_redis_client.set(f"{cache_key}:status", "in_progress", ex=initial_ttl_seconds) # Set TTL on status
        status_set_successfully = True
        if initial_status_set_event:
            initial_status_set_event.set() # Signal that the 'in_progress' status has been set
    except Exception as e:
        log.error(f"Failed to set initial 'in_progress' status for {cache_key} or signal event: {e}", exc_info=True)
        # If this fails, the caller waiting on initial_status_set_event will time out or error.
        # The lock should still be released in finally.
        # Ensure event is set to unblock, caller will handle the timeout/error.
        if initial_status_set_event and not initial_status_set_event.is_set():
            initial_status_set_event.set()
        # Do not proceed with streaming if initial status failed.
        # The 'finally' block will handle lock release.
        return # Critical to return here to prevent further operations if status not set.

    # Store metadata (optional, but useful)
    meta_key = f"{cache_key}:meta"
    try:
        await actual_redis_client.hmset(meta_key, {"model": model_name, "timestamp": time.time()})
        await actual_redis_client.expire(meta_key, initial_ttl_seconds) # Set TTL on meta

        list_key = cache_key # Use the base cache_key as the list key
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                await actual_redis_client.rpush(list_key, content)
                # Extend TTL on each chunk received to keep active streams alive
                await actual_redis_client.expire(list_key, initial_ttl_seconds)
                await actual_redis_client.expire(f"{cache_key}:status", initial_ttl_seconds)
                await actual_redis_client.expire(meta_key, initial_ttl_seconds)
        await actual_redis_client.set(f"{cache_key}:status", "done", ex=initial_ttl_seconds) # Final TTL on done status
        log.info(f"Finished streaming to Redis for key: {cache_key}")
    except Exception as e:
        log.error(f"Error streaming to Redis for key {cache_key}: {e}", exc_info=True)
        if status_set_successfully: # Only try to set error status if 'in_progress' was successfully set
            try:
                await actual_redis_client.set(f"{cache_key}:status", "error", ex=initial_ttl_seconds)
            except Exception as e_status:
                log.error(f"Failed to set 'error' status for {cache_key} after streaming error: {e_status}", exc_info=True)
        # Ensure event is set if it wasn't, to unblock waiters, even on error.
        if initial_status_set_event and not initial_status_set_event.is_set():
            initial_status_set_event.set()
    finally:
        if lock_key_to_release and actual_redis_client:
            try:
                log.info(f"Releasing lock in stream_to_redis: {lock_key_to_release}")
                deleted_count = await actual_redis_client.delete(lock_key_to_release)
                if deleted_count > 0:
                    log.info(f"Successfully released lock: {lock_key_to_release}")
                else:
                    log.warning(f"Attempted to release lock {lock_key_to_release}, but it was not found (possibly expired or released by another process).")
            except Exception as e_lock_release:
                log.error(f"Failed to release lock {lock_key_to_release} in stream_to_redis: {e_lock_release}", exc_info=True)

async def stream_custom_events_from_redis(
    cache_key: str,
    cache_hit_status: bool, # Added cache_hit_status
    redis_client_for_stream: Optional[aioredis.Redis] = None,
    max_wait_initial_status_seconds: int = 10
) -> AsyncGenerator[bytes, None]:
    """Streams content from Redis list as custom SSE/JSON events, polling for new items if in_progress."""
    actual_redis_client = redis_client_for_stream
    if actual_redis_client is None:
        log.error(f"Redis client not available for stream_custom_events_from_redis (cache_key: {cache_key}).")
        yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: Cache service not available")
        return

    list_key = cache_key
    current_index = 0
    wait_time_for_initial_status = 0.0
    initial_status_poll = True

    while True:
        status = None
        try:
            status = await actual_redis_client.get(f"{cache_key}:status")
        except Exception as e:
            log.error(f"Error getting cache status for {cache_key} from Redis: {e}", exc_info=True)
            yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: Error accessing cache status.")
            break

        if initial_status_poll and status is None:
            if wait_time_for_initial_status >= max_wait_initial_status_seconds:
                log.error(f"Timeout waiting for initial cache status for {cache_key} (waited {wait_time_for_initial_status:.1f}s). Status remains None.")
                yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: Timeout waiting for response generation to start.")
                break
            await asyncio.sleep(0.1) # Polling interval
            wait_time_for_initial_status += 0.1
            continue # Retry getting status
        elif status is not None:
            initial_status_poll = False

        new_chunks = []
        try:
            new_chunks = await actual_redis_client.lrange(list_key, current_index, -1)
        except Exception as e:
            log.error(f"Error getting chunks from Redis list {list_key}: {e}", exc_info=True)
            yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: Error accessing cached content.")
            break

        for chunk_content_str in new_chunks:
            if chunk_content_str is not None:
                yield generate_custom_sse_event(cache_key, cache_hit_status, "message", chunk_content_str)
            current_index += 1

        if status == "done":
            log.info(f"Finished streaming from Redis (done) for key: {cache_key}")
            break
        elif status == "error":
            log.error(f"Error status encountered for cache key: {cache_key} while streaming from Redis. Stopping stream.")
            yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: An error occurred during response generation.")
            break
        elif status == "in_progress":
            if not new_chunks:
                await asyncio.sleep(0.1)
        elif status is None and not initial_status_poll:
            log.warning(f"Cache key {cache_key} status disappeared or expired mid-stream (after initial appearance).")
            yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: Stream interrupted or cache expired.")
            break
        elif status is None:
            log.error(f"Cache key {cache_key} status is None unexpectedly. This should have been caught by timeout.")
            yield generate_custom_sse_event(cache_key, cache_hit_status, "close", "Error: Cache status unexpectedly became None.")
            break
        else: 
            log.warning(f"Unknown status '{status}' for cache key: {cache_key}. Stopping stream.")
            yield generate_custom_sse_event(cache_key, cache_hit_status, "close", f"Error: Unknown stream status: {status}")
            break 