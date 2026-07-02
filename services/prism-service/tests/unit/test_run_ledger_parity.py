"""Red suite — run-ledger parity/reconciliation
(task d1d4fe00, claude-p-exit epic be898578).

Wave 1 wired the local-backend seams (memory summary, graph enrich,
brain ask) into claude_runs via claude_run_log.record_local_run; the
audit lane wired pi_run_log recording INSIDE local_llm.complete. Every
local run therefore DOUBLE-RECORDS: one claude_runs row + one pi_runs
row for the same completion.

Reconciled architecture under test here:
  * pi_runs is THE ledger for backend pi|local (the /internal-agent
    audit surface) — local_llm.complete is the single recording point,
    carries the caller's purpose/project, records the input/output
    token split on the row, and additively returns the row's run_id.
  * claude_runs stays claude-only — the seams stop writing it and
    claude_run_log.record_local_run is removed.

Every test drives the REAL local_llm.complete (urlopen stubbed) so the
single-recording-point claim is exercised end to end, not around it.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def ledgers(tmp_path, monkeypatch):
    """Isolate BOTH ledgers so cross-talk between them is observable."""
    import prism_service.services.claude_run_log as crl
    import prism_service.services.pi_run_log as prl
    c_dir = tmp_path / "claude_runs"
    p_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(crl, "_RUNS_DIR", c_dir)
    monkeypatch.setattr(crl, "_MANIFEST", c_dir / "manifest.jsonl")
    monkeypatch.setattr(prl, "_RUNS_DIR", p_dir)
    monkeypatch.setattr(prl, "_MANIFEST", p_dir / "manifest.jsonl")
    return SimpleNamespace(claude_dir=c_dir, pi_dir=p_dir)


def _rows(runs_dir):
    mf = runs_dir / "manifest.jsonl"
    if not mf.exists():
        return []
    return [json.loads(ln) for ln in
            mf.read_text(encoding="utf-8").splitlines() if ln.strip()]


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _stub_endpoint(monkeypatch, text: str,
                   prompt_tokens: int = 19, completion_tokens: int = 7):
    from prism_service.inference import local_llm
    payload = json.dumps({
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens},
    }).encode()
    monkeypatch.setattr(
        local_llm.urllib.request, "urlopen",
        lambda req, timeout=0: _FakeResponse(payload))


@pytest.fixture
def forbid_claude(monkeypatch):
    from prism_service.inference import claude_cli

    def forbidden(*a, **k):
        raise AssertionError(
            "claude_cli.invoke must not run on the local backend")
    monkeypatch.setattr(claude_cli, "invoke", forbidden)


# ----------------------------------------------------------------------
# AC-4 — local_llm.complete returns run_id + records the token split.
# ----------------------------------------------------------------------
def test_complete_returns_run_id_and_token_split(ledgers, monkeypatch):
    from prism_service.inference import local_llm
    import prism_service.services.pi_run_log as prl

    _stub_endpoint(monkeypatch, "hello", prompt_tokens=23,
                   completion_tokens=11)
    out = local_llm.complete("brief", model="stub-micro",
                             purpose="adhoc", project="proj-z")

    assert out["run_id"], "complete must return the pi ledger run_id"
    runs = prl.list_recent(limit=10)
    assert len(runs) == 1
    row = runs[0]
    assert row["run_id"] == out["run_id"]
    assert row["backend"] == "local"
    assert row["tokens"] == 11          # completion side, unchanged meaning
    assert row["input_tokens"] == 23    # additive split — never lost
    assert row["output_tokens"] == 11
    assert _rows(ledgers.claude_dir) == [], \
        "a plain local completion must not touch claude_runs"


# ----------------------------------------------------------------------
# AC-1 — memory-summary local run: ONE pi row, ZERO claude_runs rows.
# ----------------------------------------------------------------------
def test_memory_summary_local_single_pi_row(ledgers, monkeypatch,
                                            forbid_claude):
    import prism_service.services.pi_run_log as prl
    from prism_service.services import memory_summary_worker as msw

    monkeypatch.setenv("PRISM_MEMORY_SUMMARY_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")
    _stub_endpoint(monkeypatch, "One clean tile sentence.",
                   prompt_tokens=41, completion_tokens=9)

    out = msw.summarize_one("mem-name", "mem-desc", "proj-z")
    assert out == "One clean tile sentence."

    pi_rows = prl.list_recent(limit=10)
    assert len(pi_rows) == 1, \
        "exactly one pi_runs row per local summary (no double-record)"
    row = pi_rows[0]
    assert row["backend"] == "local"
    assert row["purpose"] == "memory_summary"
    assert row["project"] == "proj-z"
    assert row["input_tokens"] == 41
    assert row["output_tokens"] == 9
    assert _rows(ledgers.claude_dir) == [], \
        "claude_runs must stay claude-only (no local seam rows)"


# ----------------------------------------------------------------------
# AC-2 — graph-enrich local run: ONE pi row, ZERO claude_runs rows.
# ----------------------------------------------------------------------
def test_graph_enrich_local_single_pi_row(ledgers, monkeypatch,
                                          forbid_claude):
    import prism_service.services.pi_run_log as prl
    from prism_service.services import graph_enrich as ge

    monkeypatch.setenv("PRISM_GRAPH_ENRICH_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")
    _stub_endpoint(monkeypatch,
                   '{"name": "Pi Ledger", "purpose": "Audits runs."}',
                   prompt_tokens=52, completion_tokens=13)

    scope = {"scope_id": "prism/services", "level": 1,
             "files": ["a.py", "b.py"], "symbols": ["Alpha"],
             "input_hash": "cafebabe1234"}
    assert ge.enrich_one(scope, "proj-z") == ("Pi Ledger", "Audits runs.")

    pi_rows = prl.list_recent(limit=10)
    assert len(pi_rows) == 1, \
        "exactly one pi_runs row per local enrich (no double-record)"
    row = pi_rows[0]
    assert row["backend"] == "local"
    assert row["purpose"] == "graph_enrich"
    assert row["project"] == "proj-z"
    assert row["input_tokens"] == 52
    assert row["output_tokens"] == 13
    assert _rows(ledgers.claude_dir) == [], \
        "claude_runs must stay claude-only (no local seam rows)"


# ----------------------------------------------------------------------
# AC-3 — brain-ask local run: ONE pi row whose run_id fills the response.
# ----------------------------------------------------------------------
def test_brain_ask_local_single_pi_row(ledgers, monkeypatch, forbid_claude):
    import prism_service.services.pi_run_log as prl
    from prism_service.api import brain as brain_api

    monkeypatch.setenv("PRISM_BRAIN_ASK_BACKEND", "local")
    monkeypatch.setenv("PRISM_LOCAL_LLM_MODEL", "stub-micro")
    _stub_endpoint(monkeypatch, "Grounded local answer [1].",
                   prompt_tokens=77, completion_tokens=15)

    body = brain_api.AskBody(q="where is the ledger?", project="proj-z")
    resp = brain_api._ask_local(
        body, body.q, "## Retrieved context\n\n(no matching context found)",
        [{"source_file": "a.py", "entity_name": "f",
          "entity_kind": "function", "rrf_score": 0.5, "content": "x"}])

    assert resp["answer"] == "Grounded local answer [1]."
    assert resp["tokens"] == {"input": 77, "output": 15}

    pi_rows = prl.list_recent(limit=10)
    assert len(pi_rows) == 1, \
        "exactly one pi_runs row per local ask (no double-record)"
    row = pi_rows[0]
    assert row["backend"] == "local"
    assert row["purpose"].startswith("brain_ask@")
    assert row["project"] == "proj-z"
    assert row["input_tokens"] == 77
    assert row["output_tokens"] == 15
    assert resp["run_id"] == row["run_id"], \
        "the /ask response run_id must be the pi ledger row id"
    assert _rows(ledgers.claude_dir) == [], \
        "claude_runs must stay claude-only (no local seam rows)"


# ----------------------------------------------------------------------
# AC-5 — claude_runs is claude-only: record_local_run is gone,
#         record_run (the claude path) still works.
# ----------------------------------------------------------------------
def test_claude_run_log_is_claude_only(ledgers, tmp_path):
    import prism_service.services.claude_run_log as crl

    assert not hasattr(crl, "record_local_run"), \
        "record_local_run must be removed — pi|local rows belong in pi_runs"

    run_id, stream_path = crl.new_run()
    stream_path.write_text("", encoding="utf-8")
    crl.record_run(
        run_id=run_id, stream_path=stream_path,
        ts_start=1.0, ts_end=2.0, project="proj-z",
        purpose="tour_builder@abc", exit_code=0,
        usage={"input_tokens": 5, "output_tokens": 3},
        stderr_text="",
    )
    rows = _rows(ledgers.claude_dir)
    assert len(rows) == 1
    assert rows[0]["backend"] == "claude"
    assert rows[0]["input_tokens"] == 5
    assert rows[0]["output_tokens"] == 3
