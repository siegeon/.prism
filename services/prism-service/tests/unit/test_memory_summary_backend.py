"""Red suite — env-gated local backend for the memory-summary seam
(task 5fc1683e, claude-p-exit epic be898578).

memory_summary_worker.summarize_one must route through a
`_invoke_backend` seam mirroring reflection_runner._invoke_backend:
PRISM_MEMORY_SUMMARY_BACKEND=local -> inference/local_llm.complete with
the unchanged render_prompt distillation prompt; default (unset) stays
claude_cli.invoke byte-for-byte. The local run is recorded by
local_llm.complete ITSELF into the pi_runs audit ledger (backend='local'
+ input/output token split, purpose/project passthrough) — pi_runs is
THE ledger for pi|local runs (task d1d4fe00); claude_runs stays
claude-only. local_llm.complete additively reports input_tokens/
output_tokens and the pi row's run_id.

All stubs — pytest never needs Ollama or the claude CLI.
"""

from __future__ import annotations

import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.fixture
def isolated_runs_dir(tmp_path, monkeypatch):
    """Repoint claude_run_log at tmp_path (module captures _RUNS_DIR /
    _MANIFEST from DATA_DIR at import)."""
    import prism_service.services.claude_run_log as crl
    runs_dir = tmp_path / "claude_runs"
    monkeypatch.setattr(crl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(crl, "_MANIFEST", runs_dir / "manifest.jsonl")
    return runs_dir


@pytest.fixture
def forbid_claude(monkeypatch):
    from prism_service.inference import claude_cli

    def forbidden(*a, **k):
        raise AssertionError(
            "claude_cli.invoke must not run on the local backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)
    return claude_cli


def _manifest_rows(runs_dir):
    mf = runs_dir / "manifest.jsonl"
    if not mf.exists():
        return []
    return [json.loads(ln) for ln in
            mf.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ----------------------------------------------------------------------
# AC-1 — default (env unset) stays claude_cli with the current contract.
# ----------------------------------------------------------------------
def test_default_routes_claude_cli(monkeypatch):
    from prism_service.inference import claude_cli, local_llm
    from prism_service.services import memory_summary_worker as msw

    monkeypatch.delenv("PRISM_MEMORY_SUMMARY_BACKEND", raising=False)

    def no_local(*a, **k):
        raise AssertionError("local_llm must not run on the default backend")
    monkeypatch.setattr(local_llm, "complete", no_local)

    seen: list[dict] = []

    class _FakeResult:
        exit_code = 0
        run_id = "fake-claude"

        def final_text(self):
            return '"A clean sentence."'

    def fake_invoke(**kw):
        seen.append(kw)
        return _FakeResult()
    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    out = msw.summarize_one("mem-name", "mem-desc", "no-such-project")
    assert out == "A clean sentence."
    assert seen, "claude_cli.invoke was not called on the default path"
    kw = seen[0]
    assert kw["prompt"] == msw.render_prompt("mem-name", "mem-desc")
    assert kw["max_turns"] == 1
    assert kw["allowed_tools"] == ()
    assert kw["purpose"] == "memory_summary"


# ----------------------------------------------------------------------
# AC-2 — PRISM_MEMORY_SUMMARY_BACKEND=local routes local_llm.complete.
# ----------------------------------------------------------------------
def test_local_backend_routes_local_llm(
        monkeypatch, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import memory_summary_worker as msw

    monkeypatch.setenv("PRISM_MEMORY_SUMMARY_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    calls: list[dict] = []

    def fake_complete(prompt, **kw):
        calls.append({"prompt": prompt, **kw})
        return {"text": '"Concise tile sentence."', "ms": 5.0, "tokens": 7,
                "input_tokens": 11, "output_tokens": 7}
    monkeypatch.setattr(local_llm, "complete", fake_complete)

    out = msw.summarize_one("mem-name", "mem-desc", "no-such-project")
    assert out == "Concise tile sentence."   # _strip removed the quotes
    assert calls, "local_llm.complete was not called on the local backend"
    assert calls[0]["prompt"] == msw.render_prompt("mem-name", "mem-desc")
    assert calls[0].get("model") == "stub-micro"


# ----------------------------------------------------------------------
# AC-3 — the local run lands in the pi_runs audit ledger with the token
# split; claude_runs stays claude-only (task d1d4fe00 reconciliation).
# ----------------------------------------------------------------------
def test_local_run_lands_in_ledger(
        monkeypatch, tmp_path, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl
    from prism_service.services import memory_summary_worker as msw

    pi_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", pi_dir)
    monkeypatch.setattr(prl, "_MANIFEST", pi_dir / "manifest.jsonl")

    monkeypatch.setenv("PRISM_MEMORY_SUMMARY_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")

    # Real local_llm.complete (the single recording point) — only the
    # HTTP endpoint is stubbed.
    import io

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    payload = json.dumps({
        "choices": [{"message": {"content": "Ledger sentence."}}],
        "usage": {"prompt_tokens": 21, "completion_tokens": 4},
    }).encode()
    monkeypatch.setattr(local_llm.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(payload))

    out = msw.summarize_one("mem-name", "mem-desc", "no-such-project")
    assert out == "Ledger sentence."

    rows = prl.list_recent(limit=10)
    assert len(rows) == 1, "local run must append exactly one pi_runs row"
    row = rows[0]
    assert row["backend"] == "local"
    assert row["model"] == "stub-micro"
    assert row["purpose"] == "memory_summary"
    assert row["project"] == "no-such-project"
    assert row["ok"] is True
    assert row["input_tokens"] == 21
    assert row["output_tokens"] == 4
    assert row["tokens"] == 4  # completion side, unchanged meaning
    assert _manifest_rows(isolated_runs_dir) == [], \
        "claude_runs must stay claude-only — no local seam rows"


# ----------------------------------------------------------------------
# AC-4 — local_llm.complete additively reports input/output tokens.
# ----------------------------------------------------------------------
class _StubHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        payload = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            "usage": {"prompt_tokens": 13, "completion_tokens": 5},
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def test_complete_reports_prompt_and_completion_tokens():
    from prism_service.inference import local_llm

    srv = HTTPServer(("127.0.0.1", 0), _StubHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        out = local_llm.complete(
            "hello", model="stub-model",
            base_url=f"http://127.0.0.1:{srv.server_port}/v1",
        )
    finally:
        srv.shutdown()
    # Existing keys keep their meaning (additive change only).
    assert out["text"] == "hi"
    assert out["tokens"] == 5
    # New keys: real usage from the endpoint.
    assert out["input_tokens"] == 13
    assert out["output_tokens"] == 5


# ----------------------------------------------------------------------
# AC-5 — a local failure returns "" (retry next cycle), no success row.
# ----------------------------------------------------------------------
def test_local_failure_returns_empty(
        monkeypatch, isolated_runs_dir, forbid_claude):
    from prism_service.inference import local_llm
    from prism_service.services import memory_summary_worker as msw

    monkeypatch.setenv("PRISM_MEMORY_SUMMARY_BACKEND", "local")
    attempts: list[int] = []

    def down(*a, **k):
        attempts.append(1)
        raise urllib.error.URLError("endpoint down")
    monkeypatch.setattr(local_llm, "complete", down)

    out = msw.summarize_one("mem-name", "mem-desc", "no-such-project")
    assert out == ""
    assert attempts, "local backend was never attempted"
    assert all(r.get("exit_code") == 0 for r in
               _manifest_rows(isolated_runs_dir)) or not _manifest_rows(
        isolated_runs_dir), "a failed local run must not claim success"
