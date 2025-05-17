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
            description="""
            Fetches a prompt template from Arize Phoenix by name and tag, formats it with variables, and calls OpenAI's chat completions API using the SDK.
            
            ## Headers
            
            - **x-llm-stream**: (optional, default: false) Controls streaming mode. Set to "true" to enable Server-Sent Events streaming.
            - **x-llm-cache**: (optional, default: true) Controls caching. Set to "false" to bypass the cache completely.
            - **x-clear-cache**: (optional, default: false) Set to "true" to force refresh the cache for this request.
            
            ## Request Body
            
            - **model**: (optional) Override the model specified in the Phoenix prompt.
            - **variables**: (required) Dictionary of variables to fill in the prompt template.
            - **temperature**: (optional) Control the randomness of the response.
            - **max_tokens**: (optional) Limit the maximum number of tokens in the response.
            - **session_id**, **user_id**, **metadata**, **tags**: (optional) Fields for tracing and grouping requests.
            
            Additional OpenAI parameters like functions, tools, etc. are also supported.
            
            ## Response
            
            - If **x-llm-stream** is "true": Returns a Server-Sent Events stream with incremental response chunks.
            - If **x-llm-stream** is "false": Returns a standard JSON response with the complete response.
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
         raise HTTPException(status_code=500, detail="OpenAI client is not available")

    # Check if Phoenix client is available
    if not phoenix_available or phoenix_client is None:
        log.error("Phoenix client is not available (arize-phoenix-client likely not installed).")
        raise HTTPException(status_code=501, detail="Dynamic prompt functionality is not available.")

    # --- Check Redis Client Availability ---
    # If caching is disabled via header, set redis_client to None for this request
    effective_redis_client = None if not use_caching else redis_client
    
    if redis_client is None and use_caching:
        log.warning("Redis client not available but caching was requested. Proceeding without cache.")
    elif not use_caching:
        log.info("Caching disabled by x-llm-cache header")
    
    # --- Prepare Payload for OpenAI SDK (potentially used for cache key and direct call) ---
    # Start with an empty dict for sdk_payload_dict, it will be populated after Phoenix call
    # or from cache key parameters if model is not in Phoenix.
    sdk_payload_dict_for_key = {}

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
    if effective_redis_client: # Only generate if redis is available and caching is enabled
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
            if force_refresh:
                log.info(f"Force refresh requested for cache key: {cache_key}. Deleting cache.")
                await effective_redis_client.delete(cache_key) # Deletes the list
                await effective_redis_client.delete(f"{cache_key}:status")
                await effective_redis_client.delete(f"{cache_key}:meta")
                # Any other related keys should also be deleted here.

    # --- Check Cache (if Redis is available and cache_key was generated) ---
    if effective_redis_client and cache_key:
        cache_status = await effective_redis_client.get(f"{cache_key}:status")
        log.info(f"Cache status for key {cache_key}: {cache_status}")

        if cache_status == "done" or (cache_status == "in_progress" and use_streaming):
            log.info(f"Serving from cache for key: {cache_key}, status: {cache_status}")
            response_headers = {}
            media_type = "text/event-stream" if use_streaming else "application/json"
            
            if use_streaming:
                response_headers = {
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                }
            
            # For non-streaming requests that hit a 'done' cache:
            if not use_streaming and cache_status == "done":
                # We need to aggregate the cached content.
                # This is a simplification; a full ChatCompletion object might be expected.
                # For now, we'll return concatenated content.
                # A more robust solution would store/reconstruct the full ChatCompletion object.
                log.info(f"Aggregating cached content for non-streaming request for key: {cache_key}")
                all_content_parts = await effective_redis_client.lrange(cache_key, 0, -1)
                full_content = "".join(all_content_parts)
                
                # Create a pseudo ChatCompletion object or a simple JSON response
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
                
                return pseudo_completion # FastAPI will convert dict to JSONResponse

            # For streaming requests ('in_progress' or 'done')
            return StreamingResponse(
                stream_from_redis(cache_key, "sse" if use_streaming else None, redis_client_for_stream=effective_redis_client),
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
    
    # Set stream parameter based on x-llm-stream header
    sdk_payload_dict['stream'] = use_streaming

    # Add any extra allowed fields
    exclude_keys = {'variables', 'session_id', 'user_id', 'metadata', 'tags', 
                    'model', 'temperature', 'max_tokens'}
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
        "llm.cache.enabled": use_caching, # Add cache enabled attribute
        "llm.stream.enabled": use_streaming, # Add stream enabled attribute
    }
    if effective_redis_client and cache_key: # Add cache key if available
        manual_span_attributes["llm.cache.key"] = cache_key

    filtered_manual_attributes = {k: v for k, v in manual_span_attributes.items() if v is not None}

    # Determine if the *actual* call to OpenAI needs to be streaming.
    # For caching, we *always* want to stream the response from OpenAI
    # to populate the Redis list chunk by chunk.
    # The client's stream preference (use_streaming)
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
        if effective_redis_client and cache_key and current_model_for_call:
            log.info(f"Streaming to Redis in background for cache key: {cache_key}")
            # Ensure current_model_for_call is valid before passing
            initial_status_set_event = asyncio.Event() # Create an event
            asyncio.create_task(stream_to_redis(
                cache_key,
                openai_sdk_stream_response,
                current_model_for_call,
                initial_status_set_event=initial_status_set_event, # Pass the event
                redis_client_for_stream=effective_redis_client # Pass the effective_redis_client
            ))

            # Wait for the background task to set the 'in_progress' status
            # before trying to stream from Redis. Add a timeout.
            try:
                log.debug(f"Waiting for initial_status_set_event for cache key: {cache_key}")
                await asyncio.wait_for(initial_status_set_event.wait(), timeout=5.0) # 5 second timeout
                log.debug(f"initial_status_set_event received or timed out for cache key: {cache_key}")
            except asyncio.TimeoutError:
                log.error(f"Timeout waiting for initial cache status to be set by stream_to_redis for key: {cache_key}")
                # If this timeout occurs, stream_from_redis will likely find no status and error out,
                # or the client will hang if stream_from_redis waits indefinitely without a status.
                # Raising an HTTP exception here is safer.
                raise HTTPException(status_code=500, detail="Cache population timed out waiting for initial status.")

            # Now, serve the client from Redis as it's being filled
            response_headers = {}
            media_type = "application/json"
            if use_streaming:
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
            # So, if use_streaming is False, we need to aggregate the output of stream_from_redis.

            if not use_streaming:
                log.info(f"Original request non-streaming. Aggregating content from Redis for key: {cache_key}")
                # Aggregate content from the stream_from_redis generator
                aggregated_content_bytes = [item async for item in stream_from_redis(cache_key, None, redis_client_for_stream=effective_redis_client)] # Use None for raw format from redis
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
                log.info(f"Streaming response from Redis (live fill) for key: {cache_key}")
                stream_format = "sse" if use_streaming else None
                return StreamingResponse(
                    stream_from_redis(cache_key, stream_format, redis_client_for_stream=effective_redis_client), # This will poll Redis
                    status_code=200,
                    media_type=media_type,
                    headers=response_headers
                )
        else:
            # Redis not available or issue with cache_key/model, fallback to direct OpenAI handling (original behavior)
            # This means if effective_redis_client is None, we hit this path.
            log.warning("Redis not available or cache_key/model invalid. Fallback to direct OpenAI call without caching.")
            # Set stream setting based on x-llm-stream header for direct call
            sdk_payload_dict['stream'] = use_streaming
            
            # Re-call OpenAI with original stream setting if we are not caching
            # (This is inefficient as we might have already called with stream=True if effective_redis_client was initially True then failed)
            # A better refactor would be to decide ONCE if we cache, then make ONE call.
            # For now, let's assume if effective_redis_client is None from the start, we make one call.
            # If it became None mid-logic, this is suboptimal.

            # If we are in this 'else' because effective_redis_client was None from the start,
            # we need to make the OpenAI call here.
            # If we are here because cache_key or current_model_for_call was None, but effective_redis_client is active,
            # it's an issue. The logic assumes cache_key and current_model_for_call are valid if effective_redis_client is.

            # Let's refine: if effective_redis_client IS available, but cache_key/model was bad, that's an error state for caching.
            # If effective_redis_client is NOT available, we simply pass through to OpenAI.

            # If we got here because effective_redis_client is None:
            if not effective_redis_client:
                log.info("Performing direct OpenAI call as Redis is not available or caching is disabled.")
                # Ensure payload for direct call uses stream setting based on x-llm-stream header
                sdk_payload_dict_direct_call = sdk_payload_dict.copy()
                sdk_payload_dict_direct_call['stream'] = use_streaming

                direct_response = await client.chat.completions.create(**sdk_payload_dict_direct_call)

                if use_streaming:
                    log.info("Streaming response as SSE (direct from OpenAI, no cache)")
                    return StreamingResponse(sdk_sse_stream_generator(direct_response), 
                                             status_code=200, 
                                             media_type="text/event-stream", 
                                             headers={"Content-Type": "text/event-stream", 
                                                     "Cache-Control": "no-cache", 
                                                     "X-Accel-Buffering": "no", 
                                                     "Connection": "keep-alive"})
                else:
                    log.info("Returning non-streaming response (direct from OpenAI, no cache)")
                    return direct_response # This is a ChatCompletion object
            else:
                # This case implies effective_redis_client is available, but cache_key or model was missing for the caching path.
                # This should ideally be prevented by earlier checks.
                log.error("Reached unexpected state: Redis available but caching path not taken due to missing key/model.")
                raise HTTPException(status_code=500, detail="Internal error in caching logic.")


    except openai.APIStatusError as e:
        log.warning("OpenAI API returned an error", status_code=e.status_code, error=str(e))
        if effective_redis_client and cache_key and sdk_payload_dict_for_openai_call.get('stream'): # Check if it was a streaming call for cache
            # If the call to OpenAI failed during streaming to cache, mark cache as error
            await effective_redis_client.set(f"{cache_key}:status", "error", ex=1800) # ex is TTL in seconds
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

async def stream_from_redis(
    cache_key: str,
    stream_format: Optional[str],
    redis_client_for_stream: Optional[aioredis.Redis] = None,
    max_wait_initial_status_seconds: int = 10 # Max time to wait for initial status to appear
) -> AsyncGenerator[bytes, None]:
    """Streams content from Redis list, polling for new items if in_progress. Includes timeout for initial status."""
    actual_redis_client = redis_client_for_stream # Use passed client directly
    if actual_redis_client is None:
        log.error(f"Redis client not available for stream_from_redis (cache_key: {cache_key}).")
        error_msg = "Error: Cache service not available"
        if stream_format == "sse":
            yield f"data: {json.dumps({'error': error_msg})}\n\n".encode('utf-8')
        else:
            yield error_msg.encode('utf-8')
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
            error_payload = json.dumps({"error": "Error accessing cache status."})
            if stream_format == "sse": yield f"data: {error_payload}\n\n".encode('utf-8')
            else: yield error_payload.encode('utf-8')
            break

        if initial_status_poll and status is None:
            if wait_time_for_initial_status >= max_wait_initial_status_seconds:
                log.error(f"Timeout waiting for initial cache status for {cache_key} (waited {wait_time_for_initial_status:.1f}s). Status remains None.")
                error_payload = json.dumps({"error": "Timeout waiting for response generation to start."})
                if stream_format == "sse": yield f"data: {error_payload}\n\n".encode('utf-8')
                else: yield error_payload.encode('utf-8')
                break
            await asyncio.sleep(0.1) # Polling interval
            wait_time_for_initial_status += 0.1
            continue # Retry getting status
        elif status is not None: # Status appeared or was already there
            initial_status_poll = False


        new_chunks = []
        try:
            # Retrieve available chunks
            # Use lrange to get all new chunks since last poll
            new_chunks = await actual_redis_client.lrange(list_key, current_index, -1)
        except Exception as e:
            log.error(f"Error getting chunks from Redis list {list_key}: {e}", exc_info=True)
            error_payload = json.dumps({"error": "Error accessing cached content."})
            if stream_format == "sse": yield f"data: {error_payload}\n\n".encode('utf-8')
            else: yield error_payload.encode('utf-8')
            break

        for chunk_content_str in new_chunks:
            if chunk_content_str is not None: # Ensure content is not None (Redis returns strings)
                # Construct a pseudo ChatCompletionChunk if SSE, otherwise raw content
                if stream_format == "sse":
                    # For SSE, reconstruct a more complete delta if possible,
                    # or ensure the client handles simple content updates.
                    # The current approach sends content as if it's chunk.choices[0].delta.content
                    sse_data = {
                        # "id": f"sse-cached-{cache_key}-{current_index}", # Optional: add an ID
                        # "object": "chat.completion.chunk",
                        # "created": int(time.time()),
                        # "model": "cached_model", # This would require fetching from :meta if needed per chunk
                        "choices": [{
                            "index": 0,
                            "delta": {"role": "assistant", "content": chunk_content_str},
                            # "finish_reason": None # Typically None until the end
                        }]
                    }
                    sse_event = f"data: {json.dumps(sse_data)}\n\n"
                    yield sse_event.encode('utf-8')
                else:
                    yield chunk_content_str.encode('utf-8') # For raw streaming
            current_index += 1 # Increment based on number of chunks processed

        if status == "done":
            log.info(f"Finished streaming from Redis (done) for key: {cache_key}")
            # Optionally send SSE [DONE] message if format is sse and client expects it
            # if stream_format == "sse":
            #     yield b"data: [DONE]\n\n" # OpenAI SDK compatible [DONE] is more complex
            break
        elif status == "error":
            log.error(f"Error status encountered for cache key: {cache_key} while streaming from Redis. Stopping stream.")
            error_payload = json.dumps({"error": "An error occurred during response generation."})
            if stream_format == "sse":
                yield f"data: {error_payload}\n\n".encode('utf-8')
            else:
                yield error_payload.encode('utf-8')
            break
        elif status == "in_progress":
            # If no new chunks and still in_progress, wait a bit
            if not new_chunks:
                await asyncio.sleep(0.1) # Polling interval
        elif status is None and not initial_status_poll : # Status disappeared after appearing
            log.warning(f"Cache key {cache_key} status disappeared or expired mid-stream (after initial appearance).")
            error_payload = json.dumps({"error": "Stream interrupted or cache expired."})
            if stream_format == "sse":
                yield f"data: {error_payload}\n\n".encode('utf-8')
            else:
                yield error_payload.encode('utf-8')
            break
        elif status is None: # Should have been caught by initial_status_poll timeout
            log.error(f"Cache key {cache_key} status is None unexpectedly. This should have been caught by timeout.")
            break
        else: # Should not happen if status is only 'in_progress', 'done', 'error'
            log.warning(f"Unknown status '{status}' for cache key: {cache_key}. Stopping stream.")
            error_payload = json.dumps({"error": f"Unknown stream status: {status}"})
            if stream_format == "sse": yield f"data: {error_payload}\n\n".encode('utf-8')
            else: yield error_payload.encode('utf-8')
            break 