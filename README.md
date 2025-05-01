# LLM Gateway

API Gateway for LLMs with streaming and observability.

## Development

Install dependencies:
```bash
poetry install
```

Run the server:
```bash
# Make sure to set necessary environment variables (e.g., in a .env file)
# Required: VALID_API_KEYS
# Optional/Provider-specific: OPENAI_API_KEY, PHOENIX_API_KEY, etc.

poetry run uvicorn llm_gateway.main:app --reload --app-dir src --host 0.0.0.0 --port 8000
```

## Configuration

Configuration is managed via environment variables (or a `.env` file).

- `VALID_API_KEYS`: Comma-separated list of allowed client API keys (e.g., `key1,key2`).
- `OPENAI_API_KEY`: Your API key for OpenAI (if using the OpenAI provider).
- `PHOENIX_API_KEY`: Your Arize Phoenix API key for tracing (if enabled).
- `PHOENIX_COLLECTOR_ENDPOINT`: Endpoint for the Phoenix collector (if not using default).

**Required:**
  - `VALID_API_KEYS`: Comma-separated list of allowed client API keys (e.g., `key1,key2`).

**Optional (Provider Specific):**
  - `OPENAI_API_KEY`: Your API key for OpenAI (if using the OpenAI provider).

**Optional (Observability - Arize Phoenix):**
  - `OTEL_SERVICE_NAME`: Sets the service name identifier for traces (defaults to `llm-gateway`).
  - `PHOENIX_API_KEY`: Your Arize API key (required to send traces to Arize cloud).
  - `OTEL_EXPORTER_OTLP_ENDPOINT`: The OTLP endpoint URL if not using the default Arize endpoint (e.g., `http://localhost:4317` for a local collector).
  - `OTEL_EXPORTER_OTLP_HEADERS`: Alternative way to provide authentication (e.g., `x-phoenix-api-key=YOUR_KEY`).
  - `PHOENIX_PROJECT_NAME`: Optionally specify an Arize project name for traces.
  - `LOG_LEVEL`: Set logging level (e.g., `DEBUG`, `INFO`, `WARNING`). Defaults to `INFO`. 