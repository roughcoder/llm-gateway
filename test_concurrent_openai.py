import asyncio
import httpx
import time
import json
from typing import List, Tuple, Any, Dict

# --- Configuration ---
BASE_URL = "http://localhost:8000"  # Your FastAPI app's base URL
ENDPOINT_PATH = "/providers/openai/generate-article-suggestions/production" # <--- CHANGE THIS to your actual prompt_name and tag
NUM_REQUESTS = 5  # Number of concurrent requests to send
REQUEST_TIMEOUT_SECONDS = 180  # Timeout for each individual request (generous for OpenAI)
CONCURRENCY_TEST_ID = f"concurrency-test-{int(time.time())}"

# --- Request Details ---
# Adjust this payload to match what your endpoint expects
REQUEST_PAYLOAD = {
    "variables": {"keyword": "python scripts", "addition_information": ""},
    # "model": "gpt-3.5-turbo", # Optional: if you want to override the model from Phoenix
    "temperature": 0.7,
    "session_id": CONCURRENCY_TEST_ID,
    "user_id": "test-user-123",
    "metadata": {"test_run": "locking_v1"}
}

# --- Headers ---
# Test both streaming and non-streaming, and cache variations
# Variation 1: Streaming, Caching Enabled (Default)
HEADERS_STREAM_CACHE = {
    "X-API-Key": "anotherkey",
    "x-llm-stream": "true",
    "Content-Type": "application/json",
    "Accept": "text/event-stream"
}
# Variation 2: Non-Streaming, Caching Enabled (Default)
HEADERS_NON_STREAM_CACHE = {
    "X-API-Key": "anotherkey",
    "x-llm-stream": "false",
    "Content-Type": "application/json",
    "Accept": "application/json"
}
# Variation 3: Streaming, Caching Disabled
HEADERS_STREAM_NO_CACHE = {
    "X-API-Key": "anotherkey",
    "x-llm-stream": "true",
    "x-llm-cache": "false",
    "Content-Type": "application/json",
    "Accept": "text/event-stream"
}
# Variation 4: Streaming, Caching Enabled, Force Refresh
HEADERS_STREAM_CACHE_REFRESH = {
    "X-API-Key": "anotherkey",
    "x-llm-stream": "true",
    "x-clear-cache": "true",
    "Content-Type": "application/json",
    "Accept": "text/event-stream"
}

async def make_request(
    client: httpx.AsyncClient,
    request_id: int,
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str]
) -> Tuple[int, int, bytes, float]:
    start_time = time.monotonic()
    print(f"[{time.time():.4f}] Request {request_id} (Thread: {asyncio.get_running_loop()}) starting to {url} with headers {headers.get('x-llm-stream', 'N/A_stream')}, {headers.get('x-llm-cache', 'N/A_cache')}, {headers.get('x-clear-cache', 'N/A_clear')}")
    try:
        if headers.get("x-llm-stream") == "true":
            async with client.stream("POST", url, json=payload, headers=headers) as response:
                response_content = b""
                async for chunk in response.aiter_bytes():
                    response_content += chunk
                # response.raise_for_status() # Check after consuming stream
                if response.status_code >= 400:
                     print(f"[{time.time():.4f}] Request {request_id} HTTP Error in stream: {response.status_code} - {response_content.decode(errors='ignore')[:200]}")
                return request_id, response.status_code, response_content, time.monotonic() - start_time
        else:
            response = await client.post(url, json=payload, headers=headers)
            # response.raise_for_status()
            response_content = response.content
            if response.status_code >= 400:
                print(f"[{time.time():.4f}] Request {request_id} HTTP Error: {response.status_code} - {response_content.decode(errors='ignore')[:200]}")
            return request_id, response.status_code, response_content, time.monotonic() - start_time

    except httpx.HTTPStatusError as e:
        elapsed_time = time.monotonic() - start_time
        print(f"[{time.time():.4f}] Request {request_id} HTTPStatusError: {e.response.status_code} - {e.response.text[:200]}")
        return request_id, e.response.status_code, e.response.content, elapsed_time
    except httpx.RequestError as e:
        elapsed_time = time.monotonic() - start_time
        print(f"[{time.time():.4f}] Request {request_id} RequestError: {type(e).__name__} - {str(e)}")
        return request_id, 0, str(e).encode(), elapsed_time # 0 for connection errors
    except Exception as e:
        elapsed_time = time.monotonic() - start_time
        print(f"[{time.time():.4f}] Request {request_id} General Error: {type(e).__name__} - {str(e)}")
        return request_id, -1, str(e).encode(), elapsed_time # -1 for other errors

async def run_test_scenario(
    scenario_name: str,
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    num_concurrent_requests: int = NUM_REQUESTS
):
    print(f"\\n--- Starting Test Scenario: {scenario_name} ---")
    print(f"Target URL: {url}")
    print(f"Headers: {headers}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    print(f"Sending {num_concurrent_requests} concurrent requests...")

    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS, connect=10)
    limits = httpx.Limits(max_keepalive_connections=num_concurrent_requests, max_connections=num_concurrent_requests)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        tasks = [
            make_request(client, i, url, payload, headers)
            for i in range(num_concurrent_requests)
        ]
        results: List[Tuple[int, int, bytes, float]] = await asyncio.gather(*tasks, return_exceptions=False)

    print(f"--- Results for Scenario: {scenario_name} ---")
    successful_responses_content = {}
    failed_requests = 0

    for req_id, status, content, duration in sorted(results, key=lambda x: x[0]):
        print(f"Request {req_id:02d}: Status {status:<5} Duration {duration:.3f}s Content Length: {len(content)}")
        if status == 200:
            # For streaming, SSE events might have slight variations in IDs/timestamps if we were to parse them deeply.
            # For this test, we primarily care that the *core content* (e.g., the LLM's text) is the same.
            # A simple way for SSE: extract just the 'content' parts of deltas.
            # For non-streaming, the whole JSON should be identical.
            
            processed_content = content # Store raw bytes for now
            if headers.get("x-llm-stream") == "true":
                try:
                    # Attempt to normalize SSE by extracting only AI content
                    actual_llm_text = []
                    for line in content.decode(errors='ignore').splitlines():
                        if line.startswith("data:"):
                            try:
                                data_json = json.loads(line[len("data:"):].strip())
                                if isinstance(data_json, dict) and 'choices' in data_json and \
                                   isinstance(data_json['choices'], list) and data_json['choices'] and \
                                   isinstance(data_json['choices'][0], dict) and 'delta' in data_json['choices'][0] and \
                                   isinstance(data_json['choices'][0]['delta'], dict) and \
                                   'content' in data_json['choices'][0]['delta'] and \
                                   data_json['choices'][0]['delta']['content'] is not None: # Check for not None
                                    actual_llm_text.append(data_json['choices'][0]['delta']['content'])
                            except json.JSONDecodeError:
                                pass # Ignore lines that aren't valid JSON after "data:"
                    processed_content = "".join(actual_llm_text).encode('utf-8') # Compare based on concatenated content
                except Exception as e:
                    print(f"Error processing SSE content for comparison: {e}")
                    # Fallback to raw content if processing fails
                    processed_content = content


            if processed_content not in successful_responses_content:
                successful_responses_content[processed_content] = []
            successful_responses_content[processed_content].append(req_id)
        else:
            failed_requests += 1
            print(f"  Failed Content Snippet (req {req_id}): {content[:200].decode(errors='ignore')}...")


    if not successful_responses_content and failed_requests == num_concurrent_requests:
        print("\\nRESULT: All requests failed.")
    elif len(successful_responses_content) == 1 and failed_requests == 0 :
        print("\\nRESULT: SUCCESS! All successful responses are identical.")
        # first_content_key = list(successful_responses_content.keys())[0]
        # print(f"  Content Snippet: {first_content_key[:200].decode(errors='ignore')}...")
    elif len(successful_responses_content) == 1 and failed_requests > 0:
        print(f"\\nRESULT: PARTIAL SUCCESS. All {num_concurrent_requests - failed_requests} successful responses are identical, but {failed_requests} requests failed.")
    else:
        print(f"\\nRESULT: FAILURE! Mismatch in successful responses or some failed.")
        print(f"  Number of different successful response contents: {len(successful_responses_content)}")
        print(f"  Number of failed requests: {failed_requests}")
        for i, (key_content, r_ids) in enumerate(successful_responses_content.items()):
            print(f"  Response Group {i+1} (Requests: {r_ids}): Snippet: {key_content[:200].decode(errors='ignore')}...")
    print(f"--- End of Scenario: {scenario_name} ---\n")
    return len(successful_responses_content) == 1 and failed_requests == 0


async def main():
    full_url = f"{BASE_URL}{ENDPOINT_PATH}"

    # --- Scenario 1: Standard Concurrent Requests (Streaming, Cache On) ---
    # This is the main test for the locking mechanism.
    # All requests should get the same data; only one OpenAI call.
    await run_test_scenario(
        "Concurrent Streaming (Cache On)",
        full_url,
        REQUEST_PAYLOAD,
        HEADERS_STREAM_CACHE
    )

    # --- Scenario 2: Standard Concurrent Requests (Non-Streaming, Cache On) ---
    # Similar to above, but for non-streaming.
    non_stream_payload = REQUEST_PAYLOAD.copy()
    non_stream_payload["session_id"] = f"{CONCURRENCY_TEST_ID}-nonstream"
    await run_test_scenario(
        "Concurrent Non-Streaming (Cache On)",
        full_url,
        non_stream_payload,
        HEADERS_NON_STREAM_CACHE
    )

    # --- Scenario 3: Force Cache Refresh ---
    # First request clears, subsequent ones build new cache.
    # Send one request to clear and populate.
    print("\\n--- Test Step: Clearing cache first for 'Force Cache Refresh' scenario ---")
    refresh_payload_setup = REQUEST_PAYLOAD.copy()
    refresh_payload_setup["session_id"] = f"{CONCURRENCY_TEST_ID}-refresh-setup"
    async with httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS)) as client:
         await make_request(client, 999, full_url, refresh_payload_setup, HEADERS_STREAM_CACHE_REFRESH)
    print("--- Cache potentially cleared/refreshed by request 999 ---")
    
    # Now test concurrent access to this (newly) cached item
    refresh_payload_test = REQUEST_PAYLOAD.copy() # Use same payload as cleared
    refresh_payload_test["session_id"] = f"{CONCURRENCY_TEST_ID}-refresh-setup" # Match session for cache hit
    await run_test_scenario(
        "Concurrent Streaming (After Single Cache Refresh)",
        full_url,
        refresh_payload_test,
        HEADERS_STREAM_CACHE, # Subsequent requests don't need x-clear-cache
        num_concurrent_requests=NUM_REQUESTS 
    )


    # --- Scenario 4: Caching Disabled ---
    # Each request should go directly to OpenAI. Responses might differ slightly if temp > 0.
    # For this test, we mainly observe logs to confirm no caching/locking attempts.
    # If temperature is 0, responses should be identical.
    no_cache_payload = REQUEST_PAYLOAD.copy()
    no_cache_payload["session_id"] = f"{CONCURRENCY_TEST_ID}-nocache"
    if "temperature" in no_cache_payload and no_cache_payload["temperature"] > 0:
        print("\\nWARN: For 'Caching Disabled' test with temperature > 0, responses MAY differ. Check server logs for direct OpenAI calls.")
    # To ensure identical responses for this test if temp > 0, temporarily set it to 0
    # original_temp = no_cache_payload.get("temperature")
    # no_cache_payload["temperature"] = 0.0
    await run_test_scenario(
        "Concurrent Streaming (Cache Off)",
        full_url,
        no_cache_payload,
        HEADERS_STREAM_NO_CACHE
    )
    # if original_temp is not None: # Restore temp if we changed it
    #    no_cache_payload["temperature"] = original_temp


if __name__ == "__main__":
    # IMPORTANT: Ensure your FastAPI server is running before executing this script.
    # Remember to CHANGE `ENDPOINT_PATH` at the top of this script.
    if ENDPOINT_PATH == "/providers/openai/your-prompt-name/your-tag":
        print("ERROR: Please update ENDPOINT_PATH in the script before running.")
    else:
        asyncio.run(main())
