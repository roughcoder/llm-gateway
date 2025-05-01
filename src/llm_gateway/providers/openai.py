import os
# import httpx # No longer using httpx directly
import logging
import json
from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse
from typing import Any, Dict, AsyncGenerator, List, Optional
from pydantic import BaseModel, Field

# Import OpenAI SDK
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

# Import context manager for adding attributes
from openinference.instrumentation import using_attributes

log = logging.getLogger(__name__)
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
class OpenAIChatMessage(BaseModel):
    role: str
    content: str

class OpenAIChatCompletionRequest(BaseModel):
    # Core OpenAI fields
    model: str
    messages: List[OpenAIChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    
    # --- Context fields for Tracing ---
    session_id: Optional[str] = Field(None, description="Session ID for grouping related traces.")
    user_id: Optional[str] = Field(None, description="User ID associated with the request.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Arbitrary key-value metadata for the trace.")
    tags: Optional[List[str]] = Field(None, description="List of tags to add to the trace.")
    prompt_template: Optional[str] = Field(None, description="The raw prompt template string used (if any).")
    prompt_template_version: Optional[str] = Field(None, description="Version identifier for the prompt template.")
    prompt_template_variables: Optional[Dict[str, Any]] = Field(None, description="Dictionary of variables used to fill the prompt template.")
    # ------------------------------------

    # Allow other fields supported by OpenAI API
    class Config:
        extra = 'allow'

# --- Streaming Generators ---
async def sdk_raw_stream_generator(stream: AsyncGenerator[ChatCompletionChunk, None]) -> AsyncGenerator[bytes, None]:
    """Yields raw JSON string bytes from the SDK stream chunks."""
    try:
        async for chunk in stream:
            # Each chunk is a Pydantic model (ChatCompletionChunk)
            # Convert it to JSON string and then bytes
            yield chunk.model_dump_json().encode('utf-8') + b"\n" # Add newline for separation
    except Exception as e:
        log.error(f"Error during SDK raw stream processing: {e}", exc_info=True)

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
@router.post("/chat/completions",
            status_code=200,
            summary="Proxy OpenAI Chat Completions via SDK",
            description="Proxies requests to OpenAI /v1/chat/completions using the OpenAI SDK.\n\nSupports standard non-streaming, raw chunk streaming (`stream=True`), and Server-Sent Events (`stream=True`, `stream_format=sse`). Tracing is automatically handled by OpenInference instrumentation.")
async def proxy_openai_chat_completions_sdk(
    payload: OpenAIChatCompletionRequest,
    stream_format: str | None = Query(None, description="Specify 'sse' for Server-Sent Events when stream=true. Defaults to raw chunk streaming if stream=true.")
):
    """Proxy requests to the OpenAI Chat Completions endpoint using the SDK."""
    # Check if client initialization failed during import
    if client is None: # Modify this check if you handle the exception differently above
         log.error("OpenAI client was not initialized.")
         raise HTTPException(status_code=500, detail="OpenAI client is not available")

    # Extract context attributes for tracing
    trace_attributes = {
        "session_id": payload.session_id,
        "user_id": payload.user_id,
        "metadata": payload.metadata,
        "tags": payload.tags,
        "prompt_template": payload.prompt_template,
        "prompt_template_version": payload.prompt_template_version,
        "prompt_template_variables": payload.prompt_template_variables,
    }
    # Filter out None values from trace attributes
    filtered_trace_attributes = {k: v for k, v in trace_attributes.items() if v is not None}

    # Prepare payload for OpenAI SDK (exclude our custom context fields)
    sdk_payload_dict = payload.model_dump(exclude_unset=True, exclude=set(trace_attributes.keys()))

    is_streaming_requested = sdk_payload_dict.get("stream", False)

    # --- Manual Span Creation ---

    try:
        # --- Revert to simple SDK call wrapped with using_attributes ---
        log.debug("Calling OpenAI SDK chat.completions.create", payload=sdk_payload_dict)
        with using_attributes(**filtered_trace_attributes):
             response = await client.chat.completions.create(**sdk_payload_dict)
        # Auto-instrumentation should handle span creation if enabled

        # --- Process and return response (Streaming/Non-streaming) ---
        if is_streaming_requested:
            # response is an AsyncStream[ChatCompletionChunk]
            if stream_format == "sse":
                log.info("Streaming response as SSE via SDK")
                response_headers = {
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                }
                # Note: The stream type hint might differ based on openai version
                return StreamingResponse(
                    sdk_sse_stream_generator(response), # type: ignore
                    status_code=200,
                    headers=response_headers
                )
            else:
                log.info("Streaming response as raw JSON chunks via SDK")
                # Raw chunk stream (yield JSON representation of each chunk)
                # Content-Type might be application/x-ndjson or similar
                return StreamingResponse(
                    sdk_raw_stream_generator(response), # type: ignore
                    status_code=200,
                    media_type="application/x-ndjson" # Newline-delimited JSON
                )
        else:
            # Non-streaming response: response is ChatCompletion object
            log.info("Returning non-streaming response via SDK")
            return response

    except openai.APIStatusError as e: # Revert exception handling to simpler form
        log.warning("OpenAI API returned an error", status_code=e.status_code, error=str(e))
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