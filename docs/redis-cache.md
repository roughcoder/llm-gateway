# Redis-Backed Streaming Cache for OpenAI Proxy

## Overview

This document outlines the design and implementation plan for enhancing a FastAPI-based OpenAI proxy endpoint with Redis-based streaming cache. The goal is to support resumable streaming, background completion persistence, deduplication, and cache invalidation.

---

## 🧩 Use Case Summary

### User Requirements:

1. A user can call the endpoint and receive streamed content (SSE or raw).
2. If a user disconnects mid-stream, the backend continues receiving content from OpenAI.
3. A user can reconnect and resume the stream.
4. A user can request cached results for identical inputs (instant return).
5. A user can force-refresh a request by sending a header or body field.

### System Requirements:

* Persist OpenAI streaming completions into a Redis store.
* Serve client streams from Redis (live or historical).
* Prevent duplicate calls to OpenAI when cached data is already available.
* Use background tasks to decouple OpenAI and client lifetimes.

---

## 🔧 Technology Stack

* **FastAPI** – HTTP API layer
* **OpenAI Python SDK (async)** – Streaming completions
* **Redis (with `redis.asyncio`)** – Stream buffer and cache
* **Phoenix** – Prompt templating service
* **OpenTelemetry + OpenInference** – Tracing and instrumentation

---

## 🗂 Redis Key Design

| Key                  | Type            | Purpose                                    |
| -------------------- | --------------- | ------------------------------------------ |
| `chat:{hash}`        | List            | Stores streamed content chunks             |
| `chat:{hash}:status` | String          | `in_progress`, `done`, or `error`          |
| `chat:{hash}:meta`   | Hash (optional) | Metadata like model, timestamp, session ID |

### Hash Input

The key hash is derived from:

```json
{
  "prompt_name": "...",
  "tag": "...",
  "variables": { ... },
  "model": "...",
  "temperature": 0.7,
  "max_tokens": 100
}
```

Sorted and hashed via SHA256 to form a deterministic cache key.

---

## 🔁 Streaming Logic

### When a Request is Received:

1. Compute the Redis key hash based on the request.
2. If `x-clear-cache: true` header or `force_refresh` field is present:

   * Delete `chat:{hash}` and `chat:{hash}:status`.
3. If `chat:{hash}:status == done`:

   * Stream content from Redis list.
4. If `status == in_progress`:

   * Stream content live from Redis list, polling for new items.
5. If no key exists:

   * Spawn an `asyncio.create_task` to stream OpenAI output to Redis.
   * Serve the user stream from Redis as it fills.

---

## 📦 Code Example: Redis Streaming

### Redis Setup

```python
import redis.asyncio as aioredis
redis = aioredis.from_url("redis://localhost", decode_responses=True)
```

### Cache Key Function

```python
import hashlib, json

def generate_cache_key(prompt_name, tag, variables, model, temperature, max_tokens):
    key_input = {
        "prompt_name": prompt_name,
        "tag": tag,
        "variables": variables,
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens
    }
    return "chat:" + hashlib.sha256(json.dumps(key_input, sort_keys=True).encode()).hexdigest()
```

### Stream to Redis

```python
async def stream_to_redis(cache_key, stream):
    await redis.set(f"{cache_key}:status", "in_progress")
    async for chunk in stream:
        if chunk.choices and chunk.choices[0].delta.content:
            await redis.rpush(cache_key, chunk.choices[0].delta.content)
    await redis.set(f"{cache_key}:status", "done")
```

### Stream from Redis

```python
async def stream_from_redis(cache_key):
    index = 0
    while True:
        length = await redis.llen(cache_key)
        while index < length:
            yield (await redis.lindex(cache_key, index)).encode("utf-8")
            index += 1
        if await redis.get(f"{cache_key}:status") == "done":
            break
        await asyncio.sleep(0.1)
```

### Cache Invalidation

```python
# Clear cache if forced by header or body
force_refresh = payload.dict().get("force_refresh") or request.headers.get("x-clear-cache") == "true"
if force_refresh:
    await redis.delete(cache_key)
    await redis.delete(f"{cache_key}:status")
```

---

## 🧠 Benefits

* ⚡ **Performance**: Instant return on duplicate requests.
* 🔁 **Resilience**: User disconnects don’t break OpenAI sessions.
* 📡 **Resumability**: Clients can rejoin live sessions without loss.
* 🔍 **Observability**: Traceable via OpenTelemetry.
* 🧼 **Control**: Cache can be explicitly refreshed.

---

## 📌 Next Steps

* [ ] Integrate Redis code into existing endpoint (`proxy_openai_prompt_completions_sdk`).
* [ ] Add a Redis expiry policy (e.g., 30 min TTL).
* [ ] Optional: implement WebSocket support for smarter reconnect logic.
* [ ] Add tests to validate cache hits, background continuations, and edge cases.

---

## 🔚 Closing Thoughts

This caching layer adds reliability and performance to your LLM-powered API, letting clients interact more fluidly and ensuring backend resources are used efficiently.

Let Stevie know if you want full endpoint code merged or test scaffolding built.
