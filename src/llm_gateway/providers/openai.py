import os
# import httpx # No longer using httpx directly
import logging
import json
from fastapi import APIRouter, HTTPException, Request, Response, Query
from fastapi.responses import StreamingResponse
from typing import Any, Dict, AsyncGenerator, List, Optional, Union
from pydantic import BaseModel, Field

# Import OTel trace API needed for get_current_span
from opentelemetry import trace

# Import OpenAI SDK
import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionChunk

# Import context manager for adding attributes
from openinference.instrumentation import using_attributes

# --- Import Phoenix Client ---
try:
    from phoenix.client import Client as PhoenixClient
    # Initialize Phoenix client (consider making this global like the OpenAI client)
    # For simplicity here, initializing per request, but might be inefficient.
    # Ensure PHOENIX_COLLECTOR_ENDPOINT is set in env vars for the client.
    phoenix_client = PhoenixClient(base_url="https://phoenix.infinitestack.io")
    phoenix_available = True
except ImportError:
    phoenix_client = None
    phoenix_available = False
    logging.getLogger(__name__).warning("arize-phoenix-client not installed. Dynamic prompt endpoint will not work.")
# --------------------------

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
    stream_format: str | None = Query(None, description="Specify 'sse' for Server-Sent Events when stream=true. Defaults to raw chunk streaming if stream=true.")
):
    """Fetches a prompt from Phoenix, formats it, and calls OpenAI Chat Completions."""
    # Check if OpenAI client initialization failed during import
    if client is None:
         log.error("OpenAI client was not initialized.")
         raise HTTPException(status_code=500, detail="OpenAI client is not available")

    # Check if Phoenix client is available
    if not phoenix_available or phoenix_client is None:
        log.error("Phoenix client is not available (arize-phoenix-client likely not installed).")
        raise HTTPException(status_code=501, detail="Dynamic prompt functionality is not available.")

    # --- Fetch Prompt from Phoenix ---
    try:
        log.debug("Fetching prompt from Phoenix", prompt_name=prompt_name, tag=tag)
        # Use get with identifier and tag
        phoenix_prompt = phoenix_client.prompts.get(
            prompt_identifier=prompt_name,
            tag=tag
        )
        log.info("Successfully fetched prompt from Phoenix")

        # --- Format Prompt ---
        # The phoenix prompt object's .format() method should return a dictionary
        # suitable for **kwargs unpacking into the OpenAI client create method.
        # This includes 'messages', 'model', and potentially other params defined in the prompt.
        formatted_prompt_dict = phoenix_prompt.format(variables=payload.variables)
        log.debug("Formatted prompt dictionary", formatted_dict=formatted_prompt_dict)

    except Exception as e: # Catch potential errors from phoenix client (e.g., prompt not found)
        log.error(f"Failed to fetch or format prompt from Phoenix: {e}", exc_info=True)
        # Consider more specific exceptions if the phoenix client provides them
        raise HTTPException(status_code=404, detail=f"Could not retrieve or format prompt '{prompt_name}' with tag '{tag}': {e}")
    # ------------------------------

    # --- Prepare Payload for OpenAI SDK ---
    # Start with the formatted prompt object, unpack it into a NEW dictionary
    sdk_payload_dict = {**formatted_prompt_dict}

    # Override/add parameters from the request payload if provided
    # Allow overriding the model specified in the Phoenix prompt
    if payload.model:
        sdk_payload_dict['model'] = payload.model
    elif 'model' not in sdk_payload_dict:
         log.error("Model not found in Phoenix prompt and not provided in request.")
         raise HTTPException(status_code=400, detail="Model must be specified either in the Phoenix prompt or in the request payload.")

    # Add other standard OpenAI parameters if present in the payload
    if payload.temperature is not None:
        sdk_payload_dict['temperature'] = payload.temperature
    if payload.max_tokens is not None:
        sdk_payload_dict['max_tokens'] = payload.max_tokens
    if payload.stream is not None:
        sdk_payload_dict['stream'] = payload.stream
    
    # Add any extra allowed fields from the payload (e.g., tools, tool_choice)
    # Exclude fields already handled or part of tracing context
    exclude_keys = {'variables', 'session_id', 'user_id', 'metadata', 'tags', 
                    'model', 'temperature', 'max_tokens', 'stream'} # Fields already handled
    extra_params = payload.model_dump(exclude_unset=True, exclude=exclude_keys)
    sdk_payload_dict.update(extra_params)

    # --- Extract context attributes for tracing ---
    # Only include keys directly supported by using_attributes
    using_attributes_params = {
        "session_id": payload.session_id,
        "user_id": payload.user_id,
        "metadata": payload.metadata,
        "tags": payload.tags,
        # Optionally re-add prompt_template_variables if using_attributes supports it
        # "prompt_template_variables": payload.variables,
    }
    # Filter out None values for using_attributes
    filtered_using_attributes = {k: v for k, v in using_attributes_params.items() if v is not None}

    # Prepare other attributes to be set manually on the span
    manual_span_attributes = {
        "llm.request.type": "chat",
        "llm.prompt_template.name": prompt_name,
        "llm.prompt_template.tag": tag,
        "llm.prompt_template.version": phoenix_prompt.id,
        "llm.prompt_template.variables": payload.variables,
        "llm.request.model": sdk_payload_dict.get('model'),
    }
    # Filter out None values for manual attributes
    filtered_manual_attributes = {k: v for k, v in manual_span_attributes.items() if v is not None}


    is_streaming_requested = sdk_payload_dict.get("stream", False)

    try:
        # --- Call OpenAI SDK ---
        log.debug("Calling OpenAI SDK chat.completions.create", payload=sdk_payload_dict)
        # Pass only supported args to using_attributes
        with using_attributes(**filtered_using_attributes):
            # Get the current span and set the other attributes manually
            span = trace.get_current_span()
            for key, value in filtered_manual_attributes.items():
                span.set_attribute(key, value)

            response = await client.chat.completions.create(**sdk_payload_dict)
        # Auto-instrumentation should handle the rest

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
                log.info("Streaming response as raw content chunks via SDK")
                # Default to content-only stream
                return StreamingResponse(
                    sdk_content_stream_generator(response), # Use the modified generator
                    status_code=200,
                    media_type="text/plain" # Change media type
                )
        else:
            # Non-streaming response: response is ChatCompletion object
            log.info("Returning non-streaming response via SDK")
            return response

    except openai.APIStatusError as e:
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