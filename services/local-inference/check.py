"""Exercise the local chat API, JSON extraction, and streaming response path."""
import json
import time
import urllib.request
from tune import STATE, aspire, endpoint, request

print(aspire("wait", "inference"), flush=True)
url = endpoint()
body = {
    "model": "prism-local", "temperature": 0, "max_tokens": 256,
    "chat_template_kwargs": {"enable_thinking": False},
    "messages": [{"role": "user", "content":
                  "Extract this incident: job sync-42 failed after 3 attempts because the upstream API returned HTTP 503. "
                  "Return JSON with job_id, attempts, and http_status only."}],
    "response_format": {"type": "json_object"},
}
result = request(url, "/v1/chat/completions", body)
extracted = json.loads(result["choices"][0]["message"]["content"])
assert extracted == {"job_id": "sync-42", "attempts": 3, "http_status": 503}, extracted
body.pop("response_format")
body["stream"] = True
body["messages"][0]["content"] = "In two sentences explain why retries need idempotency keys."
start = time.monotonic()
req = urllib.request.Request(url + "/v1/chat/completions", json.dumps(body).encode(),
                             {"Content-Type": "application/json"})
chunks = []
ttft = None
finished = False
with urllib.request.urlopen(req, timeout=180) as response:
    for raw in response:
        line = raw.decode().strip()
        if line == "data: [DONE]":
            finished = True
            break
        if line.startswith("data: "):
            event = json.loads(line[6:])
            for choice in event.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if content:
                    if ttft is None:
                        ttft = time.monotonic() - start
                    chunks.append(content)
assert chunks and finished, "Incomplete streaming response"
report = {"extraction_passed": True, "streaming_passed": True,
          "time_to_first_content_seconds": ttft,
          "stream_wall_seconds": time.monotonic() - start,
          "answer": "".join(chunks), "timestamp": time.time()}
(STATE / "api-check.json").write_text(json.dumps(report, indent=2))
print(json.dumps(report, indent=2))
