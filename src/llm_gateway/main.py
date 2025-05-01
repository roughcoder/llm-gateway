import time
import uuid
import logging
import os

from fastapi import FastAPI, Depends, APIRouter, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import structlog

# --- OTel Imports ---
import phoenix.otel
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.trace import Span, SpanKind
from typing import Any # Add Any import back
# --- End OTel Imports ---

# --- Early Initialization ---

# Load .env file first
load_dotenv()

# Configure Logging
from .logging_config import setup_logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
setup_logging(log_level=LOG_LEVEL)
log = structlog.get_logger(__name__)

# --- Restore OTel Initialization --- 

# Set default service name EARLY, before OTel initialization
if not os.getenv("OTEL_SERVICE_NAME"):
    os.environ["OTEL_SERVICE_NAME"] = "llm-gateway"
    log.info("Setting default OTEL_SERVICE_NAME", service_name="llm-gateway")

# --- OTel Span Hook --- 
# def rename_openai_span_hook(span: "Span", response: "Any"): <-- Remove hook function
# [...]
#             log.warning("Failed to modify OpenAI span in hook", error=str(e), span_id=span.context.span_id)

# Configure OpenTelemetry (using Arize Phoenix)
try:
    # Remove Endpoint Configuration from code - Rely purely on Env Vars
    # PHOENIX_COLLECTOR_ENDPOINT = os.getenv(
    #     "PHOENIX_COLLECTOR_ENDPOINT",
    #     "https://phoenix.infinitestack.io" # Use user's endpoint BASE, remove /v1/traces
    # )
    # # Ensure this is set for phoenix.otel.register
    # os.environ["PHOENIX_COLLECTOR_ENDPOINT"] = PHOENIX_COLLECTOR_ENDPOINT
    PHOENIX_PROJECT = os.getenv("PHOENIX_PROJECT_NAME", "llm-gateway") 

    # Log the values being used (reading from environment)
    log.info("Configuring Phoenix OTel", 
             endpoint_env=os.getenv("PHOENIX_COLLECTOR_ENDPOINT"), 
             project=PHOENIX_PROJECT)

    # Register Phoenix FIRST - Rely on env var for endpoint
    tracer_provider = phoenix.otel.register(
        project_name=PHOENIX_PROJECT,
        auto_instrument=True,
    )
    log.info("Arize Phoenix OpenTelemetry registered successfully.")

    # --- Explicit Instrumentation ---
    instrument_fastapi = False # Keep disabled
    instrument_openai = True  # Keep enabled

    if instrument_openai:
        log.debug("Attempting to instrument OpenAI SDK V2...")
        try:
            # Use the instrumentation from openinference, matching the working script
            from openinference.instrumentation.openai import OpenAIInstrumentor # Changed import
            # Don't pass tracer_provider, let it use global (like working script)
            OpenAIInstrumentor().instrument() # Removed tracer_provider argument
            log.info("Explicit OpenAI SDK instrumentation enabled (using openinference).") # Updated log message
        except ImportError as e:
            log.warning("OpenAIInstrumentor (openinference) not found, cannot instrument OpenAI SDK.", error=str(e))
            instrument_openai = False
        except Exception as e:
            log.exception("Failed to instrument OpenAI SDK") # Keep generic catch
    else:
        log.warning("OpenAI SDK instrumentation disabled by flag.")

    # --- Console Exporter (Keep commented out) ---
    # ...

except ImportError as e:
    log.warning("Required OpenTelemetry/Phoenix libraries not found. Skipping OTel setup.", error=str(e))
    instrument_fastapi = False
    instrument_openai = False 
except Exception as e:
    log.exception("Failed to initialize Arize Phoenix OpenTelemetry")
    instrument_fastapi = False
    instrument_openai = False

# --- End OTel Restore ---

# --- App Creation and Core Imports ---

from .auth import authenticate_api_key, get_client_identifier, api_key_header
from .providers.openai import router as openai_router # Direct import

app = FastAPI(
    title="LLM Gateway",
    version="0.1.0",
    description="API Gateway for LLMs with streaming and observability",
    dependencies=[Depends(api_key_header)]
)

# --- Add CORS Middleware ---
# TODO: Restrict origins for production environments for security
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Allows all origins
    allow_credentials=True,
    allow_methods=["*"], # Allows all methods
    allow_headers=["*"], # Allows all headers
)
# --- End CORS Middleware ---

# --- OTel FastAPI Instrumentation Moved (or disabled) ---
# Instrument FastAPI App (if possible and enabled)
# Needs the 'app' object, so must happen after app creation
# if instrument_fastapi: <--- Assuming this flag is false based on previous state
log.warning("FastAPI instrumentation disabled.") 

# --- Direct Router Inclusion (Replacing Dynamic Loading) ---
log.info("Including OpenAI router directly")
app.include_router(
    openai_router,
    prefix="/providers/openai", # Set prefix explicitly
    tags=["Provider: Openai"], # Set tag explicitly
    dependencies=[Depends(authenticate_api_key)] # Add auth dependency
)

# --- Middleware for request logging and context ---
@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    request_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    # Bind request context to structlog contextvars
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        client_ip=request.client.host if request.client else "unknown",
        http_method=request.method,
        http_path=request.url.path,
    )

    log.info("Request started")

    try:
        response = await call_next(request)
        process_time = time.perf_counter() - start_time
        status_code = response.status_code

        # Add response info to context
        structlog.contextvars.bind_contextvars(
            http_status_code=status_code,
            duration_ms=round(process_time * 1000, 2)
        )
        log.info("Request finished")

    except Exception as e:
        process_time = time.perf_counter() - start_time
        # Log unhandled exceptions before they turn into 500 responses
        structlog.contextvars.bind_contextvars(
            http_status_code=500,
            duration_ms=round(process_time * 1000, 2)
        )
        log.exception("Unhandled exception during request") # This logs the exception info
        # Re-raise or return a generic error response
        # Note: FastAPI might have its own exception handlers that take precedence
        response = JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"}
        )
    finally:
        structlog.contextvars.clear_contextvars() # Clean up context

    # Add request ID header to response
    if isinstance(response, Response):
        response.headers["X-Request-ID"] = request_id

    return response


@app.get("/health", tags=["Management"])
def health_check():
    """Check the health of the service."""
    log.info("Health check requested")
    return {"status": "OK", "version": app.version}


# Example secured endpoint - Now explicitly add the security scheme for docs
@app.get(
    "/secure",
    tags=["Test"],
    dependencies=[Depends(authenticate_api_key)],
)
def secure_endpoint(client_id: str = Depends(get_client_identifier)):
    """An example endpoint protected by API key authentication."""
    # Now using the dependency to get the client ID
    log.info("Secure endpoint accessed", client_id=client_id)
    return {"message": "You have accessed the secure endpoint!"}


# Placeholder for future Project routes/logic (will likely follow similar pattern)


if __name__ == "__main__":
    # Note: Uvicorn logs will now be structured JSON as well
    # The --app-dir should still be used if running directly
    # poetry run uvicorn llm_gateway.main:app --reload --app-dir src
    import uvicorn
    # Pass log_level to uvicorn if needed, though root logger is configured
    uvicorn.run(
        "llm_gateway.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=LOG_LEVEL.lower(), # Match uvicorn level
        # Use default logger config, structlog handles root
    ) 