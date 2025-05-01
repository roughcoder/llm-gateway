import os
import httpx
import logging
from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse
from typing import Any, Dict, AsyncGenerator, List, Optional
from pydantic import BaseModel, Field

# Note: Using httpx directly for now, but could switch to openai SDK later
# from openai import AsyncOpenAI

log = logging.getLogger(__name__)
router = APIRouter()

OPENAI_API_BASE_URL = "https://api.openai.com/v1"

# --- Client Management ---
# Global client instance (consider lifespan management for production)
_async_client: httpx.AsyncClient | None = None

def get_openai_client() -> httpx.AsyncClient:
    """Get the shared httpx client."""
    global _async_client
    if _async_client is None:
        log.info("Initializing OpenAI HTTPX client")
        _async_client = httpx.AsyncClient(base_url=OPENAI_API_BASE_URL)
    return _async_client

# TODO: Add lifespan manager to close the client on shutdown
# @app.on_event("shutdown")
# async def shutdown_event():
#     global _async_client
#     if _async_client:
#         await _async_client.aclose()
#         _async_client = None
#         log.info("Closed OpenAI HTTPX client")

# --- Pydantic Models for Request Body ---
class OpenAIChatMessage(BaseModel):
    role: str
    content: str

class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: List[OpenAIChatMessage]
    stream: Optional[bool] = False
    # Add other common OpenAI parameters as needed for validation/docs
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    # ... other params

    # Allow extra fields to pass through to OpenAI
    class Config:
        extra = 'allow'

# --- Streaming Generators ---
async def raw_stream_generator(response: httpx.Response) -> AsyncGenerator[bytes, None]:
    """Yields raw byte chunks from the upstream response."""
    try:
        async for chunk in response.aiter_bytes():
            yield chunk
    except Exception as e:
        log.error(f"Error during raw stream processing: {e}", exc_info=True)
    finally:
        await response.aclose()

async def sse_stream_generator(response: httpx.Response) -> AsyncGenerator[bytes, None]:
    """Yields SSE formatted events from the upstream response lines."""
    try:
        async for line in response.aiter_lines():
            if line:
                # Format as SSE 'data: ...\n\n'
                # OpenAI usually sends lines already starting with 'data: ', but we ensure format.
                if line.startswith("data:"):
                    yield f"{line}\n\n".encode('utf-8')
                else:
                    # If line doesn't start with 'data:', wrap it
                    yield f"data: {line}\n\n".encode('utf-8')
            else:
                # Yield empty lines if needed, or handle specific events like [DONE]
                yield b'\n' # Send keep-alive or separator if necessary
    except Exception as e:
        log.error(f"Error during SSE stream processing: {e}", exc_info=True)
    finally:
        await response.aclose()

# --- Endpoint ---
@router.post("/chat/completions",
            # No response_model defined as we proxy directly
            status_code=200,
            summary="Proxy OpenAI Chat Completions",
            description="Proxies requests to the OpenAI /v1/chat/completions endpoint.\n\nSupports standard non-streaming, raw chunk streaming (`stream=True`), and Server-Sent Events (`stream=True`, `stream_format=sse`).")
async def proxy_openai_chat_completions(
    payload: OpenAIChatCompletionRequest, # Use Pydantic model here
    stream_format: str | None = Query(None, description="Specify 'sse' for Server-Sent Events when stream=true. Defaults to raw chunk streaming.")
):
    """Proxy requests to the OpenAI Chat Completions endpoint, supporting non-streaming, raw chunk streaming, and SSE."""
    client = get_openai_client()
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        log.error("OPENAI_API_KEY not configured")
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

    # Convert Pydantic model back to dict for sending to OpenAI
    # Use exclude_unset=True to only send parameters explicitly set by the client
    request_payload_dict = payload.model_dump(exclude_unset=True)

    headers = {
        "Authorization": f"Bearer {openai_api_key}",
        "Content-Type": "application/json",
    }

    is_streaming_requested = request_payload_dict.get("stream", False)
    url = f"/chat/completions"

    try:
        upstream_request = client.build_request(
            method="POST",
            url=url,
            json=request_payload_dict, # Send the dict
            headers=headers
        )

        log.debug("Sending request to OpenAI", url=url, method="POST", stream=is_streaming_requested, payload=request_payload_dict)
        upstream_response = await client.send(upstream_request, stream=is_streaming_requested)
        log.debug("Received response from OpenAI", status_code=upstream_response.status_code)

        # Handle potential errors from OpenAI API itself (non-2xx status)
        if upstream_response.status_code < 200 or upstream_response.status_code >= 300:
            # Read error body whether streaming or not
            error_body = await upstream_response.aread()
            await upstream_response.aclose()
            log.warning("OpenAI API returned error", status=upstream_response.status_code, detail=error_body.decode())
            # Forward the exact status and body from OpenAI
            return Response(
                content=error_body,
                status_code=upstream_response.status_code,
                media_type=upstream_response.headers.get("content-type", "application/json")
            )

        # Handle successful responses (2xx)
        if is_streaming_requested:
            if stream_format == "sse":
                log.info("Streaming response as SSE")
                # SSE stream
                response_headers = {
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "Connection": "keep-alive",
                }
                return StreamingResponse(
                    sse_stream_generator(upstream_response),
                    status_code=200, # Always 200 for successful stream start
                    headers=response_headers
                )
            else:
                log.info("Streaming response as raw chunks")
                # Raw chunk stream (default)
                return StreamingResponse(
                    raw_stream_generator(upstream_response),
                    status_code=200,
                    media_type=upstream_response.headers.get("content-type") # Proxy content type
                )
        else:
            # Non-streaming response
            log.info("Returning non-streaming response")
            response_data = await upstream_response.aread()
            await upstream_response.aclose()
            return Response(
                content=response_data,
                status_code=upstream_response.status_code,
                media_type=upstream_response.headers.get("content-type")
            )

    except httpx.RequestError as exc:
        log.error(f"HTTPX RequestError contacting OpenAI: {exc}", exc_info=True)
        raise HTTPException(status_code=503, detail=f"Error contacting OpenAI service: {exc}")
    except Exception as exc:
        log.exception("Unhandled internal server error during OpenAI proxy") # Catches unexpected errors
        raise HTTPException(status_code=500, detail=f"Internal server error: {exc}")

# Add other OpenAI endpoints as needed following the same proxy pattern 