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