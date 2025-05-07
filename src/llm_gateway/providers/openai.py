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
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    force_refresh: Optional[bool] = Field(False, description="Force a cache refresh, ignoring any existing cached response.")

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

# --- Streaming Generators ---
# Renamed and modified generator
async def sdk_content_stream_generator(stream: AsyncGenerator[ChatCompletionChunk, None]) -> AsyncGenerator[bytes, None]:
    """Yields raw content string bytes from the SDK stream chunk deltas."""
    try:
        async for chunk in stream:
            # Ensure choices, delta, and content are present before accessing
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                yield content.encode('utf-8')
    except Exception as e:
        log.error(f"Error during SDK content stream processing: {e}", exc_info=True)
        # Optionally re-raise or yield an error indicator if needed by the client

async def sdk_sse_stream_generator(stream: AsyncGenerator[ChatCompletionChunk, None]) -> AsyncGenerator[bytes, None]:
    """Yields SSE formatted events from the SDK stream chunks."""
    try:
        async for chunk in stream:
            # Convert chunk model to JSON string
            chunk_json = chunk.model_dump_json()
            # Format as SSE event
            sse_event = f"data: {chunk_json}\n\n"
            yield sse_event.encode('utf-8')
        # Optionally send a [DONE] message if needed by clients
        # yield b"data: [DONE]\n\n"
    except Exception as e:
        log.error(f"Error during SDK SSE stream processing: {e}", exc_info=True)

# --- Endpoint ---
# Correct the path: remove the redundant /providers/openai prefix
@router.post("/{prompt_name}/{tag}",
            status_code=200,
            summary="Proxy OpenAI Chat Completions using a dynamic Phoenix Prompt",
            description="Fetches a prompt template from Arize Phoenix by name and tag, formats it with variables, and calls OpenAI /v1/chat/completions using the SDK.\\n\\nSupports standard non-streaming, raw chunk streaming (`stream=True`), and Server-Sent Events (`stream=True`, `stream_format=sse`). Tracing is automatically handled by OpenInference instrumentation.")
async def proxy_openai_prompt_completions_sdk( # Rename function for clarity
    prompt_name: str,
    tag: str,
    payload: OpenAIPromptCompletionRequest, # Use the new request model
    request: Request, # Add Request to access headers
    stream_format: str | None = Query(None, description="Specify 'sse' for Server-Sent Events when stream=true. Defaults to raw chunk streaming if stream=true.")
):
    """Fetches a prompt from Phoenix, formats it, and calls OpenAI Chat Completions, with Redis caching."""
    # Check if OpenAI client initialization failed during import
    if client is None:
         log.error("OpenAI client was not initialized.")
         raise HTTPException(status_code=500, detail="OpenAI client is not available")

    # Check if Phoenix client is available
    if not phoenix_available or phoenix_client is None:
        log.error("Phoenix client is not available (arize-phoenix-client likely not installed).")
        raise HTTPException(status_code=501, detail="Dynamic prompt functionality is not available.")

    # --- Check Redis Client Availability ---
    if redis_client is None:
        log.warning("Redis client not available. Proceeding without cache.")
        # Fallback to direct OpenAI call if Redis is down (or choose to error out)
        # For this implementation, we'll proceed without caching if Redis isn't up.
        # A production system might want to error or have a more robust fallback.

    # --- Prepare Payload for OpenAI SDK (potentially used for cache key and direct call) ---
    # Start with an empty dict for sdk_payload_dict, it will be populated after Phoenix call
    # or from cache key parameters if model is not in Phoenix.
    sdk_payload_dict_for_key = {}

    # Override/add parameters from the request payload if provided for cache key generation
    # Model is crucial for the cache key. If payload.model is present, use it.
    # If not, we'll try to get it from Phoenix prompt later.
    # This means cache key generation might be slightly deferred or need re-evaluation
    # if the model comes *only* from Phoenix. For now, assume we use payload.model if present,
    # or a default/placeholder if not, then refine.

    # For cache key, we need a definitive model.
    # If payload.model is set, use that.
    # If not, and we fetch from Phoenix, the Phoenix model will be used.
    # If neither, it's an error (handled later).

    key_model = payload.model # May be None initially
    # key_temperature and key_max_tokens will use payload values if present, None otherwise
    # which is fine for generate_cache_key as it handles None.

    # --- Fetch Prompt from Phoenix (if not solely relying on cache for metadata) ---
    # This step is still needed to get the prompt template, even with caching,
    # unless the prompt itself is also cached (which is beyond current scope).
    try:
        log.debug("Fetching prompt from Phoenix", prompt_name=prompt_name, tag=tag)
        phoenix_prompt = phoenix_client.prompts.get(
            prompt_identifier=prompt_name,
            tag=tag
        )
        log.info("Successfully fetched prompt from Phoenix")

        sanitized_variables = {
            k: (v if v is not None else "") for k, v in payload.variables.items()
        }
        formatted_prompt_dict = phoenix_prompt.format(variables=sanitized_variables)
        log.debug("Formatted prompt dictionary", formatted_dict=formatted_prompt_dict)

        # If model wasn't in payload, get it from Phoenix prompt for the cache key
        if key_model is None:
            key_model = formatted_prompt_dict.get('model')
        
        # If model is STILL None here, it's an issue (will be caught before OpenAI call)

    except Exception as e:
        log.error(f"Failed to fetch or format prompt from Phoenix: {e}", exc_info=True)
        raise HTTPException(status_code=404, detail=f"Could not retrieve or format prompt '{prompt_name}' with tag '{tag}': {e}")

    # --- Generate Cache Key ---
    cache_key = ""
    if redis_client: # Only generate if redis is available
        # Ensure key_model is available before generating cache key
        if not key_model:
             log.error("Model not found in Phoenix prompt or payload for cache key generation.")
             # This specific error path might be complex if we allow proceeding without cache.
             # For now, let it proceed, and OpenAI call logic will catch missing model.
        else:
            cache_key = generate_cache_key(
                prompt_name=prompt_name,
                tag=tag,
                variables=payload.variables, # Use original variables for cache key consistency
                model=key_model, # Use the determined model
                temperature=payload.temperature,
                max_tokens=payload.max_tokens
                # Add other OpenAI params that affect the response if they are part of the cache key definition
            )
            log.info(f"Generated cache key: {cache_key}")

            # --- Cache Invalidation ---
            force_refresh = payload.model_dump().get("force_refresh") or request.headers.get("x-clear-cache") == "true"
            if force_refresh:
                log.info(f"Force refresh requested for cache key: {cache_key}. Deleting cache.")
                await redis_client.delete(cache_key) # Deletes the list
                await redis_client.delete(f"{cache_key}:status")
                await redis_client.delete(f"{cache_key}:meta")
                # Any other related keys should also be deleted here.

    # --- Check Cache (if Redis is available and cache_key was generated) ---
    if redis_client and cache_key:
        cache_status = await redis_client.get(f"{cache_key}:status")
        log.info(f"Cache status for key {cache_key}: {cache_status}")

        is_streaming_requested_for_cache_check = payload.stream is not False # True if stream=True or stream=None (defaults to streaming)

        if cache_status == "done" or (cache_status == "in_progress" and is_streaming_requested_for_cache_check):
            log.info(f"Serving from cache for key: {cache_key}, status: {cache_status}")
            response_headers = {}
            media_type = "text/plain" # Default for raw stream from cache
            if stream_format == "sse":
                media_type = "text/event-stream" # SSE specific
                response_headers = {
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                }
            
            # For non-streaming requests that hit a 'done' cache:
            if not is_streaming_requested_for_cache_check and cache_status == "done":
                # We need to aggregate the cached content.
                # This is a simplification; a full ChatCompletion object might be expected.
                # For now, we'll return concatenated content.
                # A more robust solution would store/reconstruct the full ChatCompletion object.
                log.info(f"Aggregating cached content for non-streaming request for key: {cache_key}")
                all_content_parts = await redis_client.lrange(cache_key, 0, -1)
                full_content = "".join(all_content_parts)
                
                # Create a pseudo ChatCompletion object or a simple JSON response
                # This part needs to align with what the client expects for a non-streamed cached response.
                # Here, returning a simple JSON structure.
                # Ideally, store and retrieve the original non-streaming JSON response if caching those.
                # If only content chunks are stored, this is the best we can do without more complex reconstruction.
                cached_response_model = formatted_prompt_dict.get('model', key_model) # Get model used
                pseudo_completion = {
                    "id": f"cached-{cache_key}",
                    "object": "chat.completion",
                    "created": int(time.time()), # Or store original creation time in :meta
                    "model": cached_response_model,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_content
                        },
                        "finish_reason": "stop" # Assuming 'stop' if cache is 'done'
                    }],
                    "usage": { # Usage data is not typically stored per chunk, so this would be an estimate or omitted
                        "prompt_tokens": None, # Placeholder
                        "completion_tokens": None, # Placeholder
                        "total_tokens": None # Placeholder
                    }
                }
                # Check if system_fingerprint needs to be included based on OpenAI version/response
                # if client. Completions.with_raw_response.create(...) was used originally, this would be available
                # pseudo_completion["system_fingerprint"] = await redis_client.hget(f"{cache_key}:meta", "system_fingerprint")
                
                return pseudo_completion # FastAPI will convert dict to JSONResponse

            # For streaming requests ('in_progress' or 'done')
            return StreamingResponse(
                stream_from_redis(cache_key, stream_format),
                status_code=200,
                media_type=media_type,
                headers=response_headers
            )
        elif cache_status == "error":
            log.warning(f"Cache key {cache_key} has error status. Proceeding to OpenAI.")
            # Optionally, could raise an error or clear the errored cache entry.
            # For now, we'll treat 'error' as a cache miss and try OpenAI again.

    # --- If Cache Miss or Redis Not Available, Proceed to OpenAI ---
    log.info("Cache miss or Redis not available/force_refresh. Proceeding with OpenAI API call.")

    # --- Prepare Payload for OpenAI SDK (This section is largely the same) ---
    # Start with the formatted prompt object, unpack it into a NEW dictionary
    sdk_payload_dict = {**formatted_prompt_dict}

    if payload.model: # User-provided model overrides Phoenix
        sdk_payload_dict['model'] = payload.model
    elif 'model' not in sdk_payload_dict: # Ensure model is present
         log.error("Model not found in Phoenix prompt and not provided in request.")
         raise HTTPException(status_code=400, detail="Model must be specified either in the Phoenix prompt or in the request payload.")

    # Add other standard OpenAI parameters
    if payload.temperature is not None: sdk_payload_dict['temperature'] = payload.temperature
    if payload.max_tokens is not None: sdk_payload_dict['max_tokens'] = payload.max_tokens
    # Stream parameter is crucial for how OpenAI SDK behaves and how we cache
    # Default to stream=False if not specified, but our logic assumes payload.stream could be None
    # The cache key generation used payload.stream, so this should be consistent.
    # sdk_payload_dict['stream'] will be set based on payload.stream or its default (False if not in payload)
    # However, for caching, we usually *always* stream from OpenAI to populate cache,
    # then decide how to serve to client (stream or aggregate).
    # For simplicity here, we'll honor payload.stream for the OpenAI call.
    # A more advanced caching strategy might always stream from OpenAI.

    original_request_stream_setting = payload.stream if payload.stream is not None else False
    sdk_payload_dict['stream'] = original_request_stream_setting # Honor client's stream preference for OpenAI call initially
                                                              # This might be adjusted if we decide to always stream to cache

    # Add any extra allowed fields
    exclude_keys = {'variables', 'session_id', 'user_id', 'metadata', 'tags', 
                    'model', 'temperature', 'max_tokens', 'stream', 'force_refresh'} # Add force_refresh
    extra_params = payload.model_dump(exclude_unset=True, exclude=exclude_keys)
    sdk_payload_dict.update(extra_params)
    
    current_model_for_call = sdk_payload_dict.get('model') # Model being sent to OpenAI

    # --- Context attributes for tracing (Remains the same) ---
    using_attributes_params = {
        "session_id": payload.session_id,
        "user_id": payload.user_id,
        "metadata": payload.metadata,
        "tags": payload.tags,
    }
    filtered_using_attributes = {k: v for k, v in using_attributes_params.items() if v is not None}
    manual_span_attributes = {
        "llm.request.type": "chat",
        "llm.prompt_template.name": prompt_name,
        "llm.prompt_template.tag": tag,
        "llm.prompt_template.version": phoenix_prompt.id, # phoenix_prompt must be defined
        "llm.prompt_template.variables": payload.variables,
        "llm.request.model": current_model_for_call,
        "llm.cache.hit": False, # Explicitly set cache hit to false for this path
    }
    if redis_client and cache_key: # Add cache key if available
        manual_span_attributes["llm.cache.key"] = cache_key

    filtered_manual_attributes = {k: v for k, v in manual_span_attributes.items() if v is not None}

    # Determine if the *actual* call to OpenAI needs to be streaming.
    # For caching, we *always* want to stream the response from OpenAI
    # to populate the Redis list chunk by chunk.
    # The client's stream preference (original_request_stream_setting)
    # determines if they get a StreamingResponse or an aggregated one.
    sdk_payload_dict_for_openai_call = sdk_payload_dict.copy()
    sdk_payload_dict_for_openai_call['stream'] = True # Always stream from OpenAI for caching

    try:
        log.debug("Calling OpenAI SDK chat.completions.create (streaming for cache)", payload=sdk_payload_dict_for_openai_call)
        with using_attributes(**filtered_using_attributes):
            span = trace.get_current_span()
            for key, value in filtered_manual_attributes.items():
                span.set_attribute(key, value)
            
            # This is the actual stream from OpenAI
            openai_sdk_stream_response = await client.chat.completions.create(**sdk_payload_dict_for_openai_call)

        # If Redis is available, start background task to stream to Redis
        # and then serve from Redis (even for the first requester)
        if redis_client and cache_key and current_model_for_call:
            log.info(f"Streaming to Redis in background for cache key: {cache_key}")
            # Ensure current_model_for_call is valid before passing
            asyncio.create_task(stream_to_redis(cache_key, openai_sdk_stream_response, current_model_for_call))

            # Now, serve the client from Redis as it's being filled
            response_headers = {}
            media_type = "text/plain"
            if stream_format == "sse":
                media_type = "text/event-stream"
                response_headers = {
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                }

            # If the original request was NOT for streaming, we need to wait for the cache task to complete (or read till 'done')
            # and then aggregate. This is a more complex scenario if we serve from Redis while populating.
            # For simplicity, if original request was not streaming, we'd ideally wait for stream_to_redis to finish,
            # then retrieve all from Redis.
            # However, stream_from_redis already handles polling until "done".
            # So, if original_request_stream_setting is False, we need to aggregate the output of stream_from_redis.

            if not original_request_stream_setting:
                log.info(f"Original request non-streaming. Aggregating content from Redis for key: {cache_key}")
                # Aggregate content from the stream_from_redis generator
                aggregated_content_bytes = [item async for item in stream_from_redis(cache_key, None)] # Use None for raw format from redis
                full_content = b"".join(aggregated_content_bytes).decode('utf-8')
                
                # Similar to cache hit non-streaming:
                pseudo_completion = {
                    "id": f"live-{cache_key}", # Indicate it was live then cached
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": current_model_for_call,
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": full_content
                        },
                        "finish_reason": "stop" # Assume stop once stream_from_redis finishes
                    }],
                    "usage": None # Usage typically comes with non-streamed OpenAI response or final stream object
                }
                # After aggregation, ensure the status in Redis is 'done'. stream_to_redis handles this.
                return pseudo_completion
            else:
                # Original request was for streaming, serve from Redis as it fills
                log.info(f"Streaming response from Redis (live fill) for key: {cache_key}, format: {stream_format}")
                return StreamingResponse(
                    stream_from_redis(cache_key, stream_format), # This will poll Redis
                    status_code=200,
                    media_type=media_type,
                    headers=response_headers
                )
        else:
            # Redis not available or issue with cache_key/model, fallback to direct OpenAI handling (original behavior)
            # This means if redis_client is None, we hit this path.
            log.warning("Redis not available or cache_key/model invalid. Fallback to direct OpenAI call without caching.")
            # Reset to original stream setting if we forced it to True for caching attempt
            sdk_payload_dict['stream'] = original_request_stream_setting
            
            # Re-call OpenAI with original stream setting if we are not caching
            # (This is inefficient as we might have already called with stream=True if redis_client was initially True then failed)
            # A better refactor would be to decide ONCE if we cache, then make ONE call.
            # For now, let's assume if redis_client is None from the start, we make one call.
            # If it became None mid-logic, this is suboptimal.

            # If we are in this 'else' because redis_client was None from the start,
            # we need to make the OpenAI call here.
            # If we are here because cache_key or current_model_for_call was None, but redis_client is active,
            # it's an issue. The logic assumes cache_key and current_model_for_call are valid if redis_client is.

            # Let's refine: if redis_client IS available, but cache_key/model was bad, that's an error state for caching.
            # If redis_client is NOT available, we simply pass through to OpenAI.

            # If we got here because redis_client is None:
            if not redis_client:
                log.info("Performing direct OpenAI call as Redis is not available.")
                # Ensure payload for direct call uses original stream setting
                sdk_payload_dict_direct_call = sdk_payload_dict.copy()
                sdk_payload_dict_direct_call['stream'] = original_request_stream_setting

                direct_response = await client.chat.completions.create(**sdk_payload_dict_direct_call)

                if original_request_stream_setting:
                    if stream_format == "sse":
                        log.info("Streaming response as SSE (direct from OpenAI, no cache)")
                        return StreamingResponse(sdk_sse_stream_generator(direct_response), status_code=200, media_type="text/event-stream", headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})
                    else:
                        log.info("Streaming response as raw chunks (direct from OpenAI, no cache)")
                        return StreamingResponse(sdk_content_stream_generator(direct_response), status_code=200, media_type="text/plain")
                else:
                    log.info("Returning non-streaming response (direct from OpenAI, no cache)")
                    return direct_response # This is a ChatCompletion object
            else:
                # This case implies redis_client is available, but cache_key or model was missing for the caching path.
                # This should ideally be prevented by earlier checks.
                log.error("Reached unexpected state: Redis available but caching path not taken due to missing key/model.")
                raise HTTPException(status_code=500, detail="Internal error in caching logic.")


    except openai.APIStatusError as e:
        log.warning("OpenAI API returned an error", status_code=e.status_code, error=str(e))
        if redis_client and cache_key and sdk_payload_dict_for_openai_call.get('stream'): # Check if it was a streaming call for cache
            # If the call to OpenAI failed during streaming to cache, mark cache as error
            await redis_client.set(f"{cache_key}:status", "error", ex=1800) # ex is TTL in seconds
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except openai.APIConnectionError as e:
        log.error(f"OpenAI SDK failed to connect: {e}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Failed to connect to OpenAI service: {e}")
    except openai.RateLimitError as e:
        log.warning(f"OpenAI rate limit exceeded: {e}")
        raise HTTPException(status_code=429, detail=f"OpenAI rate limit exceeded: {e}")
    except openai.AuthenticationError as e:
        log.error(f"OpenAI authentication error: {e}")
        raise HTTPException(status_code=401, detail=f"OpenAI authentication error: {e}")
    except Exception as exc:
        log.exception("Unhandled internal server error during OpenAI SDK call")
        raise HTTPException(status_code=500, detail=f"Internal server error: {exc}")

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
async def stream_to_redis(cache_key: str, stream: AsyncGenerator[ChatCompletionChunk, None], model_name: str, initial_ttl_seconds: int = 1800):
    """Streams OpenAI content to Redis list and sets status."""
    if redis_client is None:
        log.error("Redis client not available, cannot stream to Redis.")
        return

    await redis_client.set(f"{cache_key}:status", "in_progress", ex=initial_ttl_seconds) # Set TTL on status
    # Store metadata (optional, but useful)
    meta_key = f"{cache_key}:meta"
    await redis_client.hmset(meta_key, {"model": model_name, "timestamp": time.time()})
    await redis_client.expire(meta_key, initial_ttl_seconds) # Set TTL on meta

    list_key = cache_key # Use the base cache_key as the list key
    try:
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                await redis_client.rpush(list_key, content)
                # Extend TTL on each chunk received to keep active streams alive
                await redis_client.expire(list_key, initial_ttl_seconds)
                await redis_client.expire(f"{cache_key}:status", initial_ttl_seconds)
                await redis_client.expire(meta_key, initial_ttl_seconds)
        await redis_client.set(f"{cache_key}:status", "done", ex=initial_ttl_seconds) # Final TTL on done status
        log.info(f"Finished streaming to Redis for key: {cache_key}")
    except Exception as e:
        log.error(f"Error streaming to Redis for key {cache_key}: {e}", exc_info=True)
        await redis_client.set(f"{cache_key}:status", "error", ex=initial_ttl_seconds)
        # Optionally store error information in meta or another key

async def stream_from_redis(cache_key: str, stream_format: Optional[str]) -> AsyncGenerator[bytes, None]:
    """Streams content from Redis list, polling for new items if in_progress."""
    if redis_client is None:
        log.error("Redis client not available, cannot stream from Redis.")
        # This case should ideally be handled before calling this function
        # or yield an error message to the client
        yield b"Error: Cache service not available\n"
        return

    list_key = cache_key
    current_index = 0
    while True:
        # Check status first
        status = await redis_client.get(f"{cache_key}:status")

        # Retrieve available chunks
        # Use lrange to get all new chunks since last poll
        new_chunks = await redis_client.lrange(list_key, current_index, -1)
        for chunk_content in new_chunks:
            if chunk_content: # Ensure content is not None
                if stream_format == "sse":
                    # For SSE, we need to reconstruct a pseudo-chunk or send raw content with SSE envelope
                    # For simplicity, sending raw content as SSE data. A more robust solution
                    # might store the full JSON chunk if SSE format is always needed from cache.
                    # Here, we assume the client can handle raw content strings for SSE data payload.
                    sse_event = f"data: {json.dumps({'delta': {'content': chunk_content}})}\n\n"
                    yield sse_event.encode('utf-8')
                else:
                    yield chunk_content.encode('utf-8') # For raw streaming
            current_index += 1 # Increment based on number of chunks processed

        if status == "done":
            log.info(f"Finished streaming from Redis (done) for key: {cache_key}")
            # Optionally send SSE [DONE] message if format is sse
            # if stream_format == "sse":
            #     yield b"data: [DONE]\n\n"
            break
        elif status == "error":
            log.error(f"Error status encountered for cache key: {cache_key}. Stopping stream.")
            # Yield an error message to the client
            error_payload = json.dumps({"error": "An error occurred while generating the response."})
            if stream_format == "sse":
                yield f"data: {error_payload}\n\n".encode('utf-8')
            else:
                yield error_payload.encode('utf-8')
            break
        elif status == "in_progress":
            # If no new chunks and still in_progress, wait a bit
            if not new_chunks:
                await asyncio.sleep(0.1) # Polling interval
        elif status is None:
            log.warning(f"Cache key {cache_key} status disappeared or expired mid-stream.")
            # This case means the cache entry (or its status) expired or was deleted unexpectedly
            # Treat as an error or an incomplete stream
            error_payload = json.dumps({"error": "Stream interrupted or cache expired."})
            if stream_format == "sse":
                yield f"data: {error_payload}\n\n".encode('utf-8')
            else:
                yield error_payload.encode('utf-8')
            break
        else: # Should not happen if status is only 'in_progress', 'done', 'error'
            log.warning(f"Unknown status '{status}' for cache key: {cache_key}")
            break 