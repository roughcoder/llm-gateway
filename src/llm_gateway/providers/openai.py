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

# --- Helper function for domain check ---
def check_domain_reachability(domain: str, port: int, timeout: int = 5) -> bool:
    """Attempts a socket connection to check if a hostname/IP is reachable on a specific port."""
    try:
        start_time = time.monotonic()
        log.debug(f"Checking reachability of {domain}:{port} with timeout {timeout}s")
        # Attempt connection
        sock = socket.create_connection((domain, port), timeout=timeout)
        sock.close()
        end_time = time.monotonic()
        # Log success clearly for the explicit check
        log.info(f"SUCCESS: Connection established to {domain}:{port} in {end_time - start_time:.2f}s")
        return True
    except socket.timeout:
        log.warning(f"FAILURE: Timeout ({timeout}s) trying to connect to {domain}:{port}")
        return False
    except socket.gaierror:
        log.warning(f"FAILURE: DNS resolution failed for hostname {domain}")
        return False
    except socket.error as e:
        log.warning(f"FAILURE: Could not connect to {domain}:{port}. Error: {e}")
        return False
    except Exception as e:
        log.error(f"FAILURE: Unexpected error checking reachability for {domain}:{port}: {e}")
        return False

# --- Explicit Network Reachability Test (Temporary Diagnostic) ---
# Corrected list provided by user
urls_to_check = [
    "https://phoenix.infinitestack.io", 
    "http://phoenix.infinitestack.io", 
    "http://phoenix.infinitestack.io:8000", 
    "http://phoenix.infinitestack.io:6006"
]
log.info("--- Starting Explicit Network Reachability Test (Temporary Diagnostic) ---")
all_passed = True
for url_str in urls_to_check:
    log.info(f"Testing URL: {url_str}")
    try:
        parsed_url = urlparse(url_str)
        hostname = parsed_url.hostname
        port = parsed_url.port
        # Infer default port if not specified
        if port is None:
            if parsed_url.scheme == 'https':
                port = 443
            else:
                port = 80 # Default to HTTP port 80
        
        if not hostname:
            log.warning(f"Could not parse hostname from URL: {url_str}. Skipping check.")
            all_passed = False # Consider parse failure as a failure
            continue

        # Perform the blocking check for the parsed host/port
        is_reachable = check_domain_reachability(hostname, port)
        if not is_reachable:
             all_passed = False # Mark overall test as failed if any check fails

    except ValueError as e:
        log.error(f"Invalid URL format: {url_str}. Error: {e}")
        all_passed = False
    except Exception as e:
        log.error(f"Unexpected error processing URL {url_str}: {e}")
        all_passed = False
    # Add a small delay if desired, e.g., time.sleep(0.1)

log.info(f"--- Finished Explicit Network Reachability Test. Overall Result: {'ALL PASSED (network-wise)' if all_passed else 'SOME CHECKS FAILED'} ---")
# NOTE: This test runs synchronously. The code proceeds below only after all checks complete.
# You can add 'raise ConnectionError("Network checks failed")' here if you want to halt startup on failure.
phoenix_base_url = os.environ.get("PHOENIX_BASE_URL", "https://phoenix.infinitestack.io")

# --- Explicit API Request Test (Temporary Diagnostic) ---
log.info("--- Starting Explicit API Request Test (Temporary Diagnostic) ---")
test_url = f'{phoenix_base_url}/v1/prompts/system-writer/tags/production'
test_headers = {
    'accept': 'application/json',
    'Authorization': 'Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJBcGlLZXk6MyJ9.r0w53eXJj8DGbTnFPICD0BE0nLwc_CI1IHBK1TAV4mk' # NOTE: Hardcoding tokens is generally discouraged
}

try:
    log.info(f"Making blocking GET request to: {test_url}")
    req = urllib.request.Request(test_url, headers=test_headers, method='GET')
    with urllib.request.urlopen(req, timeout=15) as response: # Add timeout
        status_code = response.getcode()
        raw_response_body = response.read()
        log.info(f"API Request SUCCESS: Status Code = {status_code}")
        try:
            # Decode assuming UTF-8, replace errors if needed
            decoded_body = raw_response_body.decode('utf-8', errors='replace') 
            log.info(f"API Request Response Body:\n{decoded_body}")
        except Exception as decode_err:
            log.error(f"Failed to decode response body: {decode_err}")
            log.info(f"Raw response body (bytes): {raw_response_body}")
            
except urllib.error.HTTPError as e:
    log.error(f"API Request FAILED: HTTP Error Code: {e.code} - {e.reason}")
    try:
        # Try reading error response body
        error_body = e.read().decode('utf-8', errors='replace')
        log.error(f"Error response body:\n{error_body}")
    except Exception:
        log.error("Could not read error response body.")
except urllib.error.URLError as e:
    # Handles connection errors, timeouts, DNS errors
    log.error(f"API Request FAILED: URL Error: {e.reason}")
except Exception as e:
    log.error(f"API Request FAILED: Unexpected error: {e}", exc_info=True) # Log traceback for unexpected errors

log.info("--- Finished Explicit API Request Test ---")

# --- Original Phoenix Client Initialization Logic --- 
# (This section should ideally use PHOENIX_BASE_URL env var as implemented previously, 
# but keeping it separate as requested for this temporary test)
# Read the base URL from environment variable, fallback to default
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