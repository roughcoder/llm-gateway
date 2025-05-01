# Python API Gateway Project Brief

## Overview
This project involves developing a **Python-based API Gateway** using **FastAPI** to manage requests to multiple upstream APIs. It will be deployed using **Docker** and support:

- Multiple API providers, each with their own endpoint.
- Project-specific custom endpoints.
- Streaming (SSE and HTTP streaming).
- API key authentication.
- Observability and tracing via [Arize Phoenix](https://arize.com/phoenix).
- Prompt loading into Arize for monitoring and debugging.

## Objectives
- Provide a unified, secure gateway for interacting with multiple APIs.
- Offer streaming capabilities.
- Support project-specific business logic.
- Log traces and prompts to [Arize Phoenix](https://arize.com/phoenix).
- Modular and extensible architecture to accommodate tools like MCP (Model Context Protocol).

## Architecture

### Core Technologies
- **Language:** Python
- **Framework:** FastAPI (asynchronous)
- **Dependency Management:** Poetry
- **Containerisation:** Docker
- **Streaming:** SSE and raw HTTP streaming

### Endpoint Types
- **Provider Endpoints:** Dedicated paths for each upstream API.
- **Project Endpoints:** Custom workflows per project.

### Authentication
- **Method:** API Key
- **Mechanism:** Passed via `X-API-Key` header

### Streaming Support
- **SSE:** `Content-Type: text/event-stream`
- **HTTP Streaming:** `application/json` or similar, chunked

### Observability
- **Tracing:** OpenTelemetry instrumentation
- **Destination:** [Arize Phoenix](https://arize.com/phoenix)
- **Prompt Logging:** Via span attributes

## Functional Requirements

### 1. Multi-Provider API Gateway
- Support multiple providers.
- Translate and route requests accordingly.
- Stream responses if upstream supports it.

### 2. Project Endpoints
- Custom endpoints under `/projects/<project_name>`
- Internal logic may chain or enrich upstream calls.

### 3. Authentication
- API key verification on all routes (except `/health`).
- Future scope: per-project access control.

### 4. Streaming
- Support both SSE and raw chunked responses.
- Configurable per endpoint or via query/header.

### 5. Observability (Arize Phoenix)
- Use OpenTelemetry SDK from [Arize](https://arize.com/phoenix).
- Log prompts, timings, model metadata.
- Ensure span creation and propagation across upstream calls.

### 6. Extensibility
- Modular components (providers, projects, tools).
- Future support for MCP:
  - Embedded or external server
  - Streaming interaction (via SSE)

## Non-Functional Requirements

### Performance
- Efficient async handling.
- Scalable with Docker containers.
- Minimal overhead on top of upstream latency.

### Security
- No access without API key.
- Environment variable-based secrets.
- CORS only if required.

### Observability & Monitoring
- Structured logging.
- Metrics via Prometheus if needed.
- Full traceability in Phoenix.

### Maintainability
- Clear module structure.
- Developer guide and README.
- Auto-generated OpenAPI docs via FastAPI.

## Integration Details

### Arize Phoenix
- Init with `phoenix.otel.register()` ([Arize Docs](https://arize.com/phoenix))
- Use span attributes like `llm.prompt_template`, `llm.parameters.max_tokens`.
- Capture upstream latency, token usage.

### MCP (Pluggable Future Integration)
- Flexible routing and SSE support enables adding MCP easily.
- Can host or communicate with an MCP server.

## Deployment Considerations

### Docker
- Use `python:3.11-slim` base image.
- Expose port 8000.
- Read config from ENV vars.

### Production Ready
- Place behind HTTPS reverse proxy.
- Tune timeouts for streaming endpoints.
- SSE-safe Nginx config: `X-Accel-Buffering: no`

### Logging & CI/CD
- Logs via stdout/stderr.
- Traces sent to Phoenix.
- Use CI pipeline to build and tag Docker images.

### Scaling
- Stateless: run multiple instances.
- Use Kubernetes or Docker Swarm as appropriate.

## Health Checks & Docs
- `GET /health` returns 200 OK
- FastAPI Swagger UI with API Key auth setup

---

**Note:** For implementation, documentation, and environment config, use the official FastAPI and Arize Phoenix docs as references. Contact your DevOps lead for deployment integration and security compliance.

