"""Red scaffold — env-gated local backend for the memory-ops runner.

Task 9f61d484 (claude-p-exit epic be898578): `memory_ops.runner.run_one`
routes every op (merge / forget / verify_staleness / distill_procedural)
through `claude_cli.invoke` with per-op model routing, including a
HARDCODED `claude-opus-4-20250514` in `distill_procedural.py`. These
tests pin the acceptance criteria for the backend seam:

  * AC-1  PRISM_MEMORY_OPS_BACKEND=local (local_llm stubbed) drives
          run_one end-to-end to a janitor submit with the SAME verdict
          JSON contract for each op family — durable consolidation_runs
          row (op_type populated, read through a separate connection),
          zero claude_cli calls;
  * AC-2  default env -> claude_cli with op.model routing intact, distill
          resolving to the opus id from the runner's per-op map;
  * AC-3  no hardcoded opus id remains in distill_procedural.py — the id
          lives ONLY in runner.CLAUDE_MODEL_MAP;
  * AC-4  local runs carry telemetry through the pi_runs single recording
          point in local_llm.complete: purpose="memory-ops:<op_type>" +
          project passthrough, run_id surfaced on OperationResult;
  * AC-5  per-op local model resolution (op.local_model, then
          LOCAL_MODEL_MAP, then local_llm.configured_model()) with
          json_mode=True on every op (all four op contracts are
          JSON-only verdicts).

Every test here FAILS today: the runner has no backend seam, no model
maps, and distill_procedural.py still carries the opus hardcode.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers (mirrors test_memory_ops_framework.py)
# ---------------------------------------------------------------------------

def _make_scores_db(tmp_path: Path) -> str:
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


def _insert_candidate(scores_db: str, cid: str) -> None:
    c = sqlite3.connect(scores_db)
    try:
        c.execute(
            "INSERT INTO consolidation_candidates "
            "(id, task_id, session_id, trigger, status, queued_at, scope_json) "
            "VALUES (?, NULL, ?, ?, 'pending', ?, '{}')",
            (cid, "sess-1", "test", "2026-07-02T10:00:00+00:00"),
        )
        c.commit()
    finally:
        c.close()


def _patch_project(monkeypatch, scores_db: str, tmp_path: Path):
    class _Ctx:
        _data_dir = Path(scores_db).parent

        class memory_svc:  # noqa: N801 — attribute namespace
            @staticmethod
            def store(**kw):
                class _E:
                    id = "mem-1"
                return _E()

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda p: tmp_path, raising=False,
    )
    return _Ctx


def _stub_op_cls(op_type: str, model: str = "", local_model: str = ""):
    """A minimal concrete MemoryOperation for a given op family."""
    from prism_service.services.memory_ops.base import MemoryOperation

    _op_type, _model, _local_model = op_type, model, local_model

    class _StubOp(MemoryOperation):
        op_type = _op_type
        schema: dict = {}
        model = _model
        provenance = {"source": "stub"}

        def __init__(self, items=None):
            self._items = items if items is not None else ["item-1"]
            self.applied = []

        def select(self, project):
            return list(self._items)

        def build_prompt(self, item, project):
            return f"{self.op_type}? item={item} project={project}"

        def apply_verdict(self, item, verdict, project):
            self.applied.append((item, verdict))

    if _local_model:
        _StubOp.local_model = _local_model
    return _StubOp


class _FakeCliResult:
    def __init__(self, text: str, run_id: str = "claude-run-1"):
        self._text = text
        self.exit_code = 0
        self.run_id = run_id
        self.duration_s = 0.1

    def final_text(self) -> str:
        return self._text


def _local_stub(captured: list, text: str, run_id: str = "pi-run-1"):
    """Stub for inference.local_llm.complete: captures kwargs, returns the
    dict contract local_llm.complete promises (text/ms/tokens/run_id)."""

    def _complete(prompt, **kw):
        captured.append({"prompt": prompt, **kw})
        return {
            "text": text, "ms": 5.0, "tokens": 12,
            "input_tokens": 34, "output_tokens": 12, "run_id": run_id,
        }

    return _complete


# The five-field JanitorService.submit envelope every op's prompt demands,
# plus the op-specific verdict fields — the per-op JSON contract.
_ENVELOPE = (
    '"qualitative_score": 0.6, "narrative": "n", "new_memories": [], '
    '"invalidate_memory_ids": [], "confidence": 0.7'
)
_OP_VERDICTS = {
    "merge": '{"same_fact": false, ' + _ENVELOPE + "}",
    "forget": '{"decision": "keep", "reason": "still load-bearing", '
              + _ENVELOPE + "}",
    "verify_staleness": '{"decision": "keep", "correction": "", '
                        + _ENVELOPE + "}",
    "distill_procedural": "{" + _ENVELOPE + "}",
}


# ---------------------------------------------------------------------------
# AC-1: local backend drives every op family end-to-end to a janitor submit
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op_type", sorted(_OP_VERDICTS))
def test_local_backend_end_to_end_submits_verdict(
        monkeypatch, tmp_path, op_type):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)
    monkeypatch.setenv("PRISM_MEMORY_OPS_BACKEND", "local")

    claude_calls = {"n": 0}

    def _no_claude(**kw):
        claude_calls["n"] += 1
        return _FakeCliResult(_OP_VERDICTS[op_type])

    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _no_claude)
    captured: list = []
    monkeypatch.setattr(
        "prism_service.inference.local_llm.complete",
        _local_stub(captured, _OP_VERDICTS[op_type]),
    )

    op = _stub_op_cls(op_type)(items=["item-1"])
    result = runner.run_one(op, "item-1", "p")

    assert result.ok, getattr(result, "error", result)
    assert claude_calls["n"] == 0, (
        "PRISM_MEMORY_OPS_BACKEND=local must not touch claude_cli"
    )
    assert len(captured) == 1, "local_llm.complete must carry the inference"
    assert op.applied and op.applied[0][0] == "item-1", (
        "apply_verdict must receive the parsed verdict on the local path"
    )

    # Durable audit row — separate connection, op_type populated (AC-1).
    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT op_type, output_json FROM consolidation_runs "
            "WHERE candidate_id = ?", ("item-1",),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "no consolidation_runs row was written"
    assert row[0] == op_type, f"op_type column not populated: {row[0]!r}"


# ---------------------------------------------------------------------------
# AC-2: default env -> claude_cli, distill model resolved from the map
# ---------------------------------------------------------------------------

def test_default_env_routes_claude_with_distill_opus_from_map(
        monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)
    monkeypatch.delenv("PRISM_MEMORY_OPS_BACKEND", raising=False)

    seen = {}

    def _capture(**kw):
        seen.update(kw)
        return _FakeCliResult(_OP_VERDICTS["distill_procedural"])

    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _capture)
    local_calls = {"n": 0}
    monkeypatch.setattr(
        "prism_service.inference.local_llm.complete",
        lambda *a, **kw: local_calls.__setitem__("n", local_calls["n"] + 1),
    )

    # op.model is EMPTY: the opus pin must come from the runner's map now.
    op = _stub_op_cls("distill_procedural", model="")(items=["item-1"])
    result = runner.run_one(op, "item-1", "p")

    assert result.ok, getattr(result, "error", result)
    assert local_calls["n"] == 0, "default env must not touch local_llm"
    assert seen.get("model") == "claude-opus-4-20250514", (
        "distill_procedural's claude model must resolve to the opus id "
        f"from runner.CLAUDE_MODEL_MAP, got {seen.get('model')!r}"
    )
    assert seen.get("purpose") == "prism-distill_procedural", (
        "default claude path must stay byte-identical (purpose arg)"
    )


def test_default_env_op_model_attribute_still_wins(monkeypatch, tmp_path):
    """A non-empty op.model must keep winning over the map (routing intact)."""
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)
    monkeypatch.delenv("PRISM_MEMORY_OPS_BACKEND", raising=False)

    seen = {}

    def _capture(**kw):
        seen.update(kw)
        return _FakeCliResult(_OP_VERDICTS["forget"])

    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _capture)

    op = _stub_op_cls("forget", model="claude-haiku-test")(items=["item-1"])
    runner.run_one(op, "item-1", "p")
    assert seen.get("model") == "claude-haiku-test"


# ---------------------------------------------------------------------------
# AC-3: no hardcoded opus id in distill_procedural.py; map owns the pin
# ---------------------------------------------------------------------------

def test_no_hardcoded_opus_in_distill_module():
    from prism_service.services.memory_ops import distill_procedural, runner

    src = Path(distill_procedural.__file__).read_text(encoding="utf-8")
    assert "claude-opus" not in src, (
        "distill_procedural.py still hardcodes an opus model id — the pin "
        "must live in runner.CLAUDE_MODEL_MAP"
    )
    assert getattr(runner, "CLAUDE_MODEL_MAP", {}).get(
        "distill_procedural") == "claude-opus-4-20250514", (
        "runner.CLAUDE_MODEL_MAP must carry distill_procedural's opus pin"
    )


# ---------------------------------------------------------------------------
# AC-4: local telemetry — purpose/project passthrough + run_id receipt
# ---------------------------------------------------------------------------

def test_local_run_telemetry_purpose_project_run_id(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _insert_candidate(scores_db, "item-1")
    _patch_project(monkeypatch, scores_db, tmp_path)
    monkeypatch.setenv("PRISM_MEMORY_OPS_BACKEND", "local")

    captured: list = []
    monkeypatch.setattr(
        "prism_service.inference.local_llm.complete",
        _local_stub(captured, _OP_VERDICTS["forget"], run_id="pi-run-42"),
    )

    op = _stub_op_cls("forget")(items=["item-1"])
    result = runner.run_one(op, "item-1", "proj-x")

    assert result.ok, getattr(result, "error", result)
    kw = captured[0]
    assert kw.get("purpose") == "memory-ops:forget", (
        f"local runs must record purpose='memory-ops:<op>': {kw.get('purpose')!r}"
    )
    assert kw.get("project") == "proj-x", "project must pass through to the ledger"
    assert result.run_id == "pi-run-42", (
        "the pi_runs run_id receipt must surface on OperationResult.run_id"
    )


# ---------------------------------------------------------------------------
# AC-5: per-op local model resolution + json_mode on every op
# ---------------------------------------------------------------------------

def test_local_model_resolution_and_json_mode(monkeypatch, tmp_path):
    from prism_service.services.memory_ops import runner

    scores_db = _make_scores_db(tmp_path)
    _patch_project(monkeypatch, scores_db, tmp_path)
    monkeypatch.setenv("PRISM_MEMORY_OPS_BACKEND", "local")
    monkeypatch.setattr(
        "prism_service.inference.local_llm.configured_model",
        lambda: "default-micro",
    )

    captured: list = []
    monkeypatch.setattr(
        "prism_service.inference.local_llm.complete",
        _local_stub(captured, _OP_VERDICTS["merge"]),
    )
    # Hermetic: a mis-routed claude path must fail fast, not pay real
    # inference (pre-implementation the runner ignores the env gate).
    monkeypatch.setattr(
        "prism_service.inference.claude_cli.invoke",
        lambda **kw: _FakeCliResult("not-the-local-path"),
    )

    # (a) op.local_model override wins.
    _insert_candidate(scores_db, "item-a")
    op = _stub_op_cls("merge", local_model="qwen3:4b")(items=["item-a"])
    assert runner.run_one(op, "item-a", "p").ok
    assert captured[-1].get("model") == "qwen3:4b", (
        f"op.local_model must win: {captured[-1].get('model')!r}"
    )

    # (b) LOCAL_MODEL_MAP is consulted when the op carries no override.
    monkeypatch.setitem(runner.LOCAL_MODEL_MAP, "merge", "mapped-model")
    _insert_candidate(scores_db, "item-b")
    op = _stub_op_cls("merge")(items=["item-b"])
    assert runner.run_one(op, "item-b", "p").ok
    assert captured[-1].get("model") == "mapped-model"

    # (c) fallback: local_llm.configured_model().
    monkeypatch.delitem(runner.LOCAL_MODEL_MAP, "merge", raising=False)
    _insert_candidate(scores_db, "item-c")
    op = _stub_op_cls("merge")(items=["item-c"])
    assert runner.run_one(op, "item-c", "p").ok
    assert captured[-1].get("model") == "default-micro"

    # json_mode=True on EVERY local call (all op contracts are JSON-only).
    assert captured and all(kw.get("json_mode") is True for kw in captured), (
        "every op verdict is a JSON contract — json_mode must be True"
    )
