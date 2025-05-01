import os
import asyncio
import logging

# Configure basic logging so we can see OTel warnings
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Import Phoenix register and the V2 OpenAI instrumentor
from phoenix.otel import register
from opentelemetry.instrumentation.openai_v2 import OpenAIInstrumentor
from openai import AsyncOpenAI

log.info("--- Phoenix Smoke Test ---         ")

# Verify required environment variables are set
api_key = os.getenv("OPENAI_API_KEY")
phoenix_api_key = os.getenv("PHOENIX_API_KEY")
phoenix_endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT")

log.info(f"PHOENIX_PROJECT_NAME  : debug-smoke (hardcoded)")
log.info(f"PHOENIX_COLLECTOR_ENDPOINT: {phoenix_endpoint}")
log.info(f"PHOENIX_API_KEY        : {'*****' if phoenix_api_key else 'Not Set'}")
log.info(f"OPENAI_API_KEY       : {'*****' if api_key else 'Not Set'}")

# Check for missing critical variables
if not api_key or not phoenix_api_key or not phoenix_endpoint:
    log.error("Error: OPENAI_API_KEY, PHOENIX_API_KEY, and PHOENIX_COLLECTOR_ENDPOINT must be set.")
    exit(1)

# Register Phoenix - should use PHOENIX_COLLECTOR_ENDPOINT and PHOENIX_API_KEY from env
# Also rely on OTEL_EXPORTER_OTLP_HEADERS, OTEL_EXPORTER_OTLP_PROTOCOL if set
log.info("Registering Phoenix OTel...")
try:
    tracer_provider = register(
        project_name="debug-smoke", 
        auto_instrument=False # Explicit instrumentation below
    )
    log.info("Phoenix OTel registered successfully.")
except Exception as e:
    log.exception("Failed to register Phoenix OTel")
    exit(1)

# Instrument OpenAI using the V2 instrumentor
log.info("Instrumenting OpenAI V2...")
try:
    OpenAIInstrumentor().instrument(tracer_provider=tracer_provider)
    log.info("OpenAI V2 instrumentation complete.")
except Exception as e:
    log.exception("Failed to instrument OpenAI V2")
    exit(1)

# Create the OpenAI client AFTER instrumentation
log.info("Creating AsyncOpenAI client...")
try:
    client = AsyncOpenAI()
except Exception as e:
    log.exception("Failed to create AsyncOpenAI client")
    exit(1)

# Define the async function to make the call
async def demo():
    log.info("Running demo chat completion...")
    try:
        response = await client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": "ping from smoke test"}]
        )
        log.info(f"OpenAI Response: {response.choices[0].message.content}")
    except Exception as e:
        log.exception("Error during OpenAI call")

# Run the async function
log.info("Executing asyncio.run(demo())...")
asyncio.run(demo())

# Explicitly shut down the provider to force export attempt
log.info("Shutting down TracerProvider to force flush...")
try:
    flushed = tracer_provider.force_flush(timeout_millis=10000) # 10 second timeout
    if not flushed:
         log.error("force_flush() failed or timed out. Export likely failed.")
    else:
         log.info("force_flush() completed successfully.")
    tracer_provider.shutdown()
    log.info("TracerProvider shutdown complete.")
except Exception as e:
    log.exception("Error during OTel shutdown/flush")

log.info("--- Smoke Test Finished ---         ") 