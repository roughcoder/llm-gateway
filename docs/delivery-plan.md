# API Gateway Delivery Plan

## Phase 1: Core Foundation (MVP Readiness)

**✅ Milestone: Internal Gateway with basic provider passthrough and auth**

| Ticket | Description | Testable Criteria |
|--------|-------------|-------------------|
| 1.1 | FastAPI App Scaffold with Docker | App boots via Docker, serves root and health check endpoint |
| 1.2 | Implement Global API Key Auth Middleware | Reject requests without `X-API-Key`; allow `/health` unauthenticated |
| 1.3 | Set Up Provider Router Structure | Can mount `/provider/<name>/` dynamically |
| 1.4 | Basic Provider Integration (e.g. OpenAI) | Sends request and proxies response; unit test with mock upstream |
| 1.5 | Structured JSON Logging | All requests log path, status, client key ID (not raw key), latency |
| 1.6 | Health Check Endpoint | Returns 200 OK and gateway version or build info |

---

## Phase 2: Streaming + Observability

**✅ Milestone: Streaming-capable gateway with Phoenix tracing**

| Ticket | Description | Testable Criteria |
|--------|-------------|-------------------|
| 2.1 | Implement SSE Streaming Support | Endpoint streams `text/event-stream`; browser EventSource demo |
| 2.2 | Implement Raw Chunked Streaming | Endpoint streams `application/json` or `text/plain` |
| 2.3 | Configurable Streaming Mode (query/header based) | `?stream_mode=sse` works; default is JSON |
| 2.4 | Integrate Arize Phoenix (OpenTelemetry) | Spans appear in Phoenix; include prompt, tokens, provider metadata |
| 2.5 | Log Prompt Metadata in Traces | `llm.prompt_template`, `llm.parameters.max_tokens` attributes set |
| 2.6 | Trace Upstream Call as Child Span | Verify parent-child span relationship from logs or Phoenix UI |

---

## Phase 3: Project-Specific Workflows

**✅ Milestone: Modular project endpoints with custom logic**

| Ticket | Description | Testable Criteria |
|--------|-------------|-------------------|
| 3.1 | Scaffold `/projects/<name>/` routing | Dynamic mounting of project routers |
| 3.2 | Implement Sample Project Endpoint | E.g. `/projects/demo/summary`; internally chains 2 upstreams |
| 3.3 | Project-specific Auth (Optional Scope Field) | If enabled, reject mismatched key-project pairs |
| 3.4 | Trace and Log Project Requests | Project ID shown in Phoenix; logs indicate internal chain calls |
| 3.5 | Docs Auto-generation for Projects | `/docs` includes project endpoint details via Pydantic schemas |

---

## Phase 4: Security, Config, and Production-Ready Ops

**✅ Milestone: Hardened, secure and operable container ready for prod**

| Ticket | Description | Testable Criteria |
|--------|-------------|-------------------|
| 4.1 | Env-based Config for Upstreams & Keys | No secrets in code; mountable config verified via `.env` |
| 4.2 | CORS Support (if enabled) | Configurable via ENV; tested via browser OPTIONS preflight |
| 4.3 | Max Payload Size + Timeout | Large inputs are rejected; upstream timeout returns 504 |
| 4.4 | Observability Metrics (e.g., Prometheus) | Basic QPS and error count exposed at `/metrics` or integrated |
| 4.5 | CI/CD Pipeline for Tagged Image Builds | Docker image built, tagged by SHA or version, and published |
| 4.6 | Nginx Config and Streaming Tuning Docs | `X-Accel-Buffering: no` or Nginx config documented and tested |

---

## Phase 5: Testing, QA and Release

**✅ Milestone: Ready for rollout and integration testing**

| Ticket | Description | Testable Criteria |
|--------|-------------|-------------------|
| 5.1 | Unit Tests for Auth, Providers, Streaming | `pytest` suite with ≥80% coverage |
| 5.2 | Integration Test Harness (e.g., mock upstreams) | Docker Compose test setup with dummy upstream |
| 5.3 | End-to-End Test Scenarios | Realistic flow: Project endpoint → Multiple providers → Streaming |
| 5.4 | Staging Deploy and Verification Checklist | Validate startup, env config, Phoenix traces, logs, and metrics |
| 5.5 | Release Candidate Tag + Documentation | Image tagged, README updated, changelog generated |

---

## Future Consideration Phase: Extensions

| Ticket | Description |
|--------|-------------|
| F1 | Rate Limiting Middleware |
| F2 | MCP Sub-App Endpoint |
| F3 | GraphQL Gateway (for structured workflows) |
| F4 | UI Debug Console or Admin API |
