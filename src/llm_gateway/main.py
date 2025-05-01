import importlib
import pkgutil
import time
import uuid
import logging
import os

from fastapi import FastAPI, Depends, APIRouter, Request, Response
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
import structlog

# --- Early Initialization ---

# Load .env file first
load_dotenv()

# Configure Logging
from .logging_config import setup_logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
setup_logging(log_level=LOG_LEVEL)
log = structlog.get_logger(__name__)

# Configure OpenTelemetry (using Arize Phoenix)
# Must be done BEFORE instrumenting libraries or importing instrumented libs
try:
    import phoenix.otel
    from opentelemetry import trace # Import trace API
    from opentelemetry.sdk.trace import TracerProvider # Import TracerProvider
    from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor # Import exporter/processor

    # Service name can be set via OTEL_SERVICE_NAME env var
    # Resource attributes can be set via OTEL_RESOURCE_ATTRIBUTES env var
    
    # Configure Phoenix connection details
    # Explicitly set endpoint and project name if needed,
    # otherwise defaults and env vars (PHOENIX_PROJECT_NAME, OTEL_EXPORTER_OTLP_ENDPOINT) are used.
    PHOENIX_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://phoenix.infinitestack.io/v1/traces") # Your custom endpoint + OTel path
    PHOENIX_PROJECT = os.getenv("PHOENIX_PROJECT_NAME", "your-next-llm-project") # Your project name

    log.info("Configuring Phoenix OTel", endpoint=PHOENIX_ENDPOINT, project=PHOENIX_PROJECT)

    # Register Phoenix FIRST
    phoenix.otel.register(
        project_name=PHOENIX_PROJECT,
        endpoint=PHOENIX_ENDPOINT,
    )
    log.info("Arize Phoenix OpenTelemetry registered successfully.")

    # --- Explicit Instrumentation (Attempt AFTER OTel setup) ---
    instrument_fastapi = False # Flag to enable/disable - SET TO FALSE
    instrument_openai = True  # Flag to enable/disable - KEEP TRUE

    # Instrument OpenAI SDK (if possible and enabled)
    # Do this EARLY, before potentially importing modules that use the SDK
    if instrument_openai:
        log.debug("Attempting to instrument OpenAI SDK...")
        try:
            # Get the global tracer provider configured by Phoenix/OTel
            tracer_provider = trace.get_tracer_provider()
            # Must import here after OTel is configured
            from opentelemetry.instrumentation.openai import OpenAIInstrumentor
            # Explicitly pass the tracer_provider
            OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
            log.info("Explicit OpenAI SDK instrumentation enabled.")
        except ImportError:
            log.warning("OpenAIInstrumentor not found, cannot instrument OpenAI SDK.")
            instrument_openai = False # Disable if import failed
        except Exception as e:
            log.exception("Failed to instrument OpenAI SDK")
            instrument_openai = False # Disable if instrumentation failed
    else:
        log.warning("OpenAI SDK instrumentation disabled by flag.")

    # --- Console Exporter (Keep commented out for now) ---
    # # Get the TracerProvider configured by phoenix.otel.register
    # tracer_provider = trace.get_tracer_provider()
    # # Add the console exporter using a SimpleSpanProcessor
    # # Note: Phoenix likely uses BatchSpanProcessor for OTLP, keep Simple for console.
    # console_exporter = ConsoleSpanExporter()
    # tracer_provider.add_span_processor(SimpleSpanProcessor(console_exporter))
    # log.info("Added ConsoleSpanExporter for OTel debugging.")
    # ------------------------------------------

    # Set default service name AFTER instrumentation might have been attempted
    if not os.getenv("OTEL_SERVICE_NAME"):
        os.environ["OTEL_SERVICE_NAME"] = "llm-gateway"

except ImportError as e:
    log.warning("Required OpenTelemetry/Phoenix libraries not found. Skipping OTel setup.", error=str(e))
    instrument_fastapi = False
    instrument_openai = False # Ensure flags are false if initial import failed
except Exception as e:
    log.exception("Failed to initialize Arize Phoenix OpenTelemetry")
    instrument_fastapi = False
    instrument_openai = False # Ensure flags are false if init failed

# --- App Creation and Core Imports (Imports happen AFTER instrumentation attempt) ---

from .auth import authenticate_api_key, get_client_identifier, api_key_header
from . import providers # Import the providers package

app = FastAPI(
    title="LLM Gateway",
    version="0.1.0",
    description="API Gateway for LLMs with streaming and observability",
    dependencies=[Depends(api_key_header)]
)

# Instrument FastAPI App (if possible and enabled)
# Needs the 'app' object, so must happen after app creation
if instrument_fastapi:
    log.debug("Attempting to instrument FastAPI...")
    try:
        # Must import here after OTel is configured
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        FastAPIInstrumentor.instrument_app(app)
        log.info("Explicit FastAPI instrumentation enabled.")
    except ImportError:
        log.warning("FastAPIInstrumentor not found, cannot instrument FastAPI.")
    except Exception as e:
        log.exception("Failed to instrument FastAPI")
else:
    log.warning("FastAPI instrumentation disabled.")

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


# --- Provider Router Loading (Imports provider modules) ---
def include_provider_routers(app: FastAPI):
    """Dynamically load and include routers from the providers package."""
    provider_package = providers
    package_name = provider_package.__name__ # e.g., llm_gateway.providers
    package_path = provider_package.__path__
    router_prefix_base = package_name.split('.')[-1] # Use 'providers'

    log.info(f"Loading routers from package", package_name=package_name, package_path=str(package_path))

    # Iterate through modules in the package path
    for finder, module_name, ispkg in pkgutil.iter_modules(package_path):
        if ispkg:
            log.debug("Skipping subpackage", module_name=module_name)
            continue # Skip subpackages for now

        full_module_name = f"{package_name}.{module_name}"
        try:
            log.debug(f"Attempting to import module", module_name=full_module_name)
            module = importlib.import_module(full_module_name)

            # Look for an APIRouter instance named 'router' in the module
            if hasattr(module, 'router') and isinstance(module.router, APIRouter):
                provider_name = module_name # e.g., 'openai'
                router_prefix = f"/{router_prefix_base}/{provider_name}"
                log.info(f"Including router", module=full_module_name, prefix=router_prefix, provider=provider_name)

                # Apply API key auth dependency to all routes in provider routers
                app.include_router(
                    module.router,
                    prefix=router_prefix,
                    tags=[f"Provider: {provider_name.capitalize()}"],
                    dependencies=[Depends(authenticate_api_key)] # Runtime check
                    # The global app dependency on api_key_header helps with docs
                )
            else:
                log.warning(f"No APIRouter named 'router' found", module_name=full_module_name)
        except ImportError as e:
             log.error(f"ImportError loading module", module_name=full_module_name, error=str(e))
        except Exception as e:
            log.exception(f"Failed to load or include router", module_name=full_module_name)

# Include provider routers on startup
include_provider_routers(app)


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