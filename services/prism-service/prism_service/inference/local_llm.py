"""Keyless local-LLM client for PRISM internal inference (task ecb90f7e).

Speaks the OpenAI-compatible /chat/completions dialect against a LOCAL
endpoint (Ollama, LM Studio, vLLM, llama.cpp server). Deliberately stdlib
urllib and deliberately NO API key — INV-1 (no cloud credential in the
service) holds by construction. This is the engine the self-learning loop
uses when PRISM_REFLECTION_BACKEND=local routes reflection at a micro
model (see services/reflection_runner._invoke_backend and the
benchmarks/micro_llm_selflearn ranking that picks the model).

Env:
  PRISM_LOCAL_LLM_URL    base URL, default http://localhost:11434/v1
  PRISM_LOCAL_LLM_MODEL  model id, default from the bench winner
"""

from __future__ import annotations

import json
import os
import time
import urllib.request

DEFAULT_URL = "http://localhost:11434/v1"
# Bench winner (benchmarks/results/micro_llm_selflearn latest.json):
# 522MB, 100% on reflect_verdict/distill/tool_call at T1 p50 3244ms —
# faster AND more reliable than the 7-8b baselines on these shapes.
DEFAULT_MODEL = "qwen3:0.6b"


def configured_url() -> str:
    return (os.environ.get("PRISM_LOCAL_LLM_URL") or DEFAULT_URL).rstrip("/")


def configured_model() -> str:
    return os.environ.get("PRISM_LOCAL_LLM_MODEL") or DEFAULT_MODEL


def _record_run(**kwargs) -> None:
    """Audit-ledger seam (services/pi_run_log): ADDITIVE, best-effort —
    a ledger failure must never raise into the inference caller path."""
    try:
        from prism_service.services import pi_run_log

        pi_run_log.record_run(**kwargs)
    except Exception:
        pass


def complete(
    prompt: str,
    *,
    model: str = "",
    base_url: str = "",
    system: str = "",
    max_tokens: int = 1024,
    json_mode: bool = False,
    temperature: float = 0.0,
    timeout: float = 300.0,
    purpose: str = "",
    project: str = "",
) -> dict:
    """One blocking completion. Returns {text, ms, tokens}. Raises OSError /
    urllib.error.URLError when the endpoint is unreachable — callers treat
    that like any other inference failure.

    Every run — success or unreachable endpoint — lands in the pi_run_log
    audit ledger as a backend=local row (the SPA's /internal-agent view).
    The new `purpose`/`project` kwargs are ADDITIVE: defaults keep every
    existing caller untouched; an unset purpose is inferred (reflect for
    the reflection engine's system marker, else adhoc)."""
    url = (base_url or configured_url()).rstrip("/")
    mdl = model or configured_model()
    sys_text = system
    if mdl.startswith("qwen3") and "/no_think" not in sys_text:
        # qwen3 thinking mode multiplies latency; self-learning payloads
        # are structured extraction, not reasoning marathons.
        sys_text = (sys_text + " /no_think").strip()
    messages = []
    if sys_text:
        messages.append({"role": "system", "content": sys_text})
    messages.append({"role": "user", "content": prompt})
    body: dict = {
        "model": mdl,
        "stream": False,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        f"{url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        from prism_service.services.pi_run_log import infer_purpose

        _purpose = infer_purpose(purpose, system)
    except Exception:
        _purpose = purpose or "adhoc"
    audit = {
        "backend": "local",
        "model": mdl,
        "project": project,
        "prompt_chars": len(prompt or ""),
        "tools_used": [],  # tool-less by design
        "purpose": _purpose,
    }
    ts_start = time.time()
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            payload = json.loads(res.read())
    except Exception as exc:
        _record_run(
            duration_ms=(time.perf_counter() - t0) * 1000.0,
            ok=False, error=f"{type(exc).__name__}: {exc}",
            ts_start=ts_start, ts_end=time.time(),
            **audit,
        )
        raise
    ms = (time.perf_counter() - t0) * 1000.0
    msg = (payload.get("choices") or [{}])[0].get("message") or {}
    usage = payload.get("usage") or {}
    out = {
        "text": msg.get("content") or "",
        "ms": round(ms, 1),
        "tokens": usage.get("completion_tokens") or 0,
        # ADDITIVE (task 5fc1683e): real usage split so callers can feed
        # the run ledger without losing token telemetry. "tokens" keeps
        # its historical meaning (completion side) for existing callers.
        "input_tokens": usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("completion_tokens") or 0,
    }
    _record_run(
        duration_ms=out["ms"], tokens=int(out["tokens"] or 0),
        ok=True, error="", ts_start=ts_start, ts_end=time.time(),
        **audit,
    )
    return out
