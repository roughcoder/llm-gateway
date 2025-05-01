import os
import asyncio
import logging

# Configure basic logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# OTel Imports for manual setup
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    ConsoleSpanExporter, # Optional: Keep console exporter for debugging
)
# Import the gRPC OTLP Exporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# 1. --- Verify Environment Variables ---
# These MUST be set *before* running this script
grpc_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
headers = os.getenv("OTEL_EXPORTER_OTLP_HEADERS")
protocol = os.getenv("OTEL_EXPORTER_OTLP_PROTOCOL") # Should be unset or grpc

log.info(f"--- OTel GRPC Export Test ---         ")
log.info(f"OTEL_EXPORTER_OTLP_ENDPOINT : {grpc_endpoint}")
log.info(f"OTEL_EXPORTER_OTLP_HEADERS  : {headers}")
log.info(f"OTEL_EXPORTER_OTLP_PROTOCOL: {protocol}")

if not grpc_endpoint or not headers:
    log.error("Error: OTEL_EXPORTER_OTLP_ENDPOINT and OTEL_EXPORTER_OTLP_HEADERS must be set.")
    exit(1)
if protocol and protocol != "grpc":
    log.error("Error: OTEL_EXPORTER_OTLP_PROTOCOL must be 'grpc' or unset for this test.")
    exit(1)

# 2. --- Manual OTel SDK Setup (using GRPC Exporter) ---
try:
    # Set resource attributes (optional, but good practice)
    resource = Resource(attributes={
        "service.name": "otel-grpc-test-script"
    })

    # Create the OTLP gRPC exporter - it will use env vars by default
    # endpoint and headers are picked from OTEL_EXPORTER_OTLP_ENDPOINT / HEADERS
    otlp_exporter = OTLPSpanExporter()
    log.info(f"OTLPSpanExporter created. Endpoint: {otlp_exporter._endpoint}, Headers: {'*****' if otlp_exporter._headers else 'None'}")


    # Create a BatchSpanProcessor and add the exporter
    # Using BatchSpanProcessor is recommended for production
    span_processor = BatchSpanProcessor(otlp_exporter)
    log.info("BatchSpanProcessor created with OTLP gRPC exporter.")

    # Optional: Add console exporter for debugging
    console_processor = BatchSpanProcessor(ConsoleSpanExporter())
    log.info("BatchSpanProcessor created with Console exporter.")

    # Create a TracerProvider and add the processors
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(span_processor)
    provider.add_span_processor(console_processor) # Add console processor too

    # Set the global TracerProvider
    trace.set_tracer_provider(provider)
    log.info("Global TracerProvider set.")

except Exception as e:
    log.exception("Error during OTel SDK setup")
    exit(1)

# 3. --- Create and Export a Test Span ---
tracer = trace.get_tracer("my.tracer.name")

log.info("Creating a test span...")
with tracer.start_as_current_span("grpc-test-span") as span:
    span.set_attribute("test.attribute", "hello grpc")
    span.add_event("Test event occurred")
    log.info(f"Span '{span.name}' created with context: {span.context}")

log.info("Test span finished.")

# 4. --- Shutdown ---
# Explicitly shut down the provider to force export attempt
log.info("Shutting down TracerProvider to force flush...")
try:
    # force_flush returns False if export fails
    # timeout_millis=5000 # 5 seconds
    flushed = provider.force_flush(timeout_millis=10000) 
    if not flushed:
         log.error("force_flush() failed or timed out. Export likely failed.")
    else:
         log.info("force_flush() completed successfully.")
    provider.shutdown()
    log.info("TracerProvider shutdown complete.")
except Exception as e:
    log.exception("Error during shutdown/flush")

log.info("--- Test Script Finished ---         ") 