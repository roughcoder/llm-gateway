import logging
import sys
import structlog
import os

def setup_logging(log_level: str = "INFO"):
    """Configure structlog for JSON logging."""
    log_level_upper = log_level.upper()
    if log_level_upper not in ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]:
        log_level_upper = "INFO"

    # Processors applied to logs BOTH from structlog AND standard logging
    # Order matters!
    shared_processors: list[structlog.types.Processor] = [
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(), # Format %-style messages
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.contextvars.merge_contextvars,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        # Note: JSONRenderer comes last in the final structlog config
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Use structlog's native wrapper for stdlib compatibility
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Configure the stdlib formatter to use structlog processors
    # This formatter runs *after* wrap_for_formatter
    formatter = structlog.stdlib.ProcessorFormatter(
        # The main processor chain applied to structlog-originated logs
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        # Processors applied *first* to logs from standard logging
        # before they enter the main chain.
        foreign_pre_chain=shared_processors,
    )

    # Configure Python's standard logging handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # Configure root logger
    root_logger = logging.getLogger()
    # Clear existing handlers (important for environments like Docker/Uvicorn)
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level_upper)

    # --- Configure Specific Loggers ---

    # Silence default Uvicorn access logs if desired, let root handler process them
    logging.getLogger("uvicorn.access").handlers = []
    logging.getLogger("uvicorn.access").propagate = True

    # Ensure uvicorn error logs also use our handler
    logging.getLogger("uvicorn.error").propagate = True

    # Set OpenTelemetry log level to DEBUG for troubleshooting
    # Note: Do this AFTER setting root logger level if root level is higher than DEBUG
    otel_log_level = logging.DEBUG
    logging.getLogger("opentelemetry").setLevel(otel_log_level)
    # You might need to be more specific, e.g., "opentelemetry.sdk", "opentelemetry.exporter"
    # logging.getLogger("opentelemetry.sdk").setLevel(otel_log_level)
    # logging.getLogger("opentelemetry.exporter.otlp").setLevel(otel_log_level)
    print(f"OpenTelemetry logging level set to: {logging.getLevelName(otel_log_level)}")

    # Use standard logging for the setup confirmation message itself
    logging.basicConfig(level=log_level_upper)
    initial_log = logging.getLogger(__name__)
    initial_log.info(f"Structured logging configured")

# Example usage for getting a logger elsewhere:
# import structlog
# log = structlog.get_logger(__name__)
# log.info("This is a structlog log", key="value") 