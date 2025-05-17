# LLM Gateway

API Gateway for LLMs with streaming, caching, dynamic prompts, and observability.

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

## Features

### Dynamic Prompts with Phoenix
The gateway integrates with Arize Phoenix to load prompt templates by name and tag, allowing for centralized prompt management and versioning.

### Streaming Support
Server-Sent Events (SSE) streaming is supported for real-time responses controlled via HTTP headers.

### Response Caching
Redis-based caching of LLM responses to reduce costs and latency for repeated queries.

### Request Control Headers
- `x-llm-stream: true/false` - Enable or disable streaming responses (default: false)
- `x-llm-cache: true/false` - Enable or disable response caching (default: true)
- `x-clear-cache: true/false` - Force invalidate existing cache entries (default: false)

### Observability
Integrated with OpenTelemetry for comprehensive tracing and monitoring of LLM requests.

## Configuration

Configuration is managed via environment variables (or a `.env` file).

**Required:**
  - `VALID_API_KEYS`: Comma-separated list of allowed client API keys (e.g., `key1,key2`).

**Optional (Provider Specific):**
  - `OPENAI_API_KEY`: Your API key for OpenAI (if using the OpenAI provider).
  - `REDIS_URL`: Redis connection string for caching (e.g., `redis://localhost`).

**Optional (Phoenix Prompt Integration):**
  - `PHOENIX_BASE_URL`: Base URL for Arize Phoenix (default: `https://phoenix.infinitestack.io`).

**Optional (Observability - Arize Phoenix):**
  - `OTEL_SERVICE_NAME`: Sets the service name identifier for traces (defaults to `llm-gateway`).
  - `PHOENIX_API_KEY`: Your Arize API key (required to send traces to Arize cloud).
  - `OTEL_EXPORTER_OTLP_ENDPOINT`: The OTLP endpoint URL if not using the default Arize endpoint (e.g., `http://localhost:4317` for a local collector).
  - `OTEL_EXPORTER_OTLP_HEADERS`: Alternative way to provide authentication (e.g., `x-phoenix-api-key=YOUR_KEY`).
  - `PHOENIX_PROJECT_NAME`: Optionally specify an Arize project name for traces.
  - `LOG_LEVEL`: Set logging level (e.g., `DEBUG`, `INFO`, `WARNING`). Defaults to `INFO`.

## API Endpoints

### Dynamic OpenAI Chat Completions
```
POST /providers/openai/{prompt_name}/{tag}
```

Fetches a prompt template from Arize Phoenix, formats it with variables, and calls OpenAI's chat completions API.

**Path Parameters:**
- `prompt_name`: The identifier of the prompt template in Phoenix
- `tag`: The tag/version of the prompt to use

**Request Body:**
```json
{
  "model": "gpt-4o",             // Optional, overrides model in prompt template
  "variables": {                  // Required, variables to fill the prompt template
    "user_query": "Hello world",
    "context": "This is some context"
  },
  "temperature": 0.7,            // Optional OpenAI parameters
  "max_tokens": 500,
  "session_id": "session123",    // Optional tracing fields
  "user_id": "user456",
  "metadata": {"source": "web"},
  "tags": ["production", "test"]
}
```

**Example Request with Headers:**
```bash
curl -X POST "http://localhost:8000/providers/openai/chat_template/latest" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "x-llm-stream: true" \
  -H "x-llm-cache: true" \
  -d '{
    "variables": {
      "user_query": "Tell me about LLM gateways"
    },
    "temperature": 0.7
  }' 
```

## API Response Format

The LLM Gateway returns responses in JSON format. The specific structure depends on whether streaming is enabled via the `x-llm-stream` header. All responses include an `id` (which is the Redis cache key for the request) and a `cacheHit` boolean flag.

### Common Fields

-   `id` (string): The unique cache key generated for this request. This key is used to identify the request in the Redis cache.
-   `cacheHit` (boolean):
    -   `true`: Indicates that the response was served (or partially served for an in-progress stream) from a pre-existing, valid cache entry in Redis.
    -   `false`: Indicates that the response was generated fresh by the LLM (and then cached). This also applies if `x-clear-cache: true` was used.

### Non-Streaming Response (`x-llm-stream: false` or header not provided)

When streaming is disabled, the API returns a single JSON object.

-   **Content-Type**: `application/json`
-   **Structure**:
    ```json
    {
      "id": "<cache_key>",
      "cacheHit": true_or_false,
      "event": "finished",
      "message": "<full_aggregated_text_response>"
    }
    ```
-   **Fields**:
    -   `event` (string): Always `"finished"` for a complete, non-streamed response.
    -   `message` (string): The full, aggregated text content generated by the LLM. In case of an error during processing, this field might contain an error message.

### Streaming Response (`x-llm-stream: true`)

When streaming is enabled, the API returns a stream of Server-Sent Events (SSE).

-   **Content-Type**: `text/event-stream`
-   **Structure**: Each event in the stream is a JSON object formatted as follows:
    ```json
    data: {"id": "<cache_key>", "cacheHit": true_or_false, "event": "<event_type>", "message": "<content_or_info>"}

    ```
    *(Note: Each `data:` line is followed by two newlines `\n\n` as per SSE spec)*

-   **Event Types (`event` field)**:
    -   `"start"`: The first event sent when the stream is initiated.
        -   `message` (string): Typically an informational message, e.g., `"Stream started."`.
    -   `"message"`: Represents a chunk of the generated text from the LLM.
        -   `message` (string): The actual text chunk.
    -   `"close"`: The final event sent when the stream is successfully completed or terminated due to an error.
        -   `message` (string): Typically an informational message, e.g., `"Stream ended."`. If the stream was closed due to an error, this field will contain the error details.

**Example Flow for Streaming Events:**

1.  `data: {"id": "chat:xyz...", "cacheHit": false, "event": "start", "message": "Stream started."}\n\n`
2.  `data: {"id": "chat:xyz...", "cacheHit": false, "event": "message", "message": "This is the first "}\n\n`
3.  `data: {"id": "chat:xyz...", "cacheHit": false, "event": "message", "message": "part of the response."}\n\n`
4.  `data: {"id": "chat:xyz...", "cacheHit": false, "event": "close", "message": "Stream ended."}\n\n`

This structured response format ensures that clients can easily parse the output, understand the cache status, and handle both complete and incremental updates effectively.