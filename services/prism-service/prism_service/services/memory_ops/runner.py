"""Shared single-tick runner for every MemoryOperation.

Ports the ``reflection_runner.run_one`` plumbing — ``claude_cli.invoke`` →
markdown-fence strip → ``json.loads`` → validation → JanitorService audit
write → memory store loop — into ONE op-agnostic path so every op family
(forget / prune / distill / ...) reuses the same inference + audit +
idempotency guarantees.

Idempotency: an item is a ``consolidation_candidates`` row id. The runner only
processes rows whose status is ``pending``/``dispensed``; ``JanitorService.submit``
marks the row ``completed``, so a second tick on the same item short-circuits
(no second inference, no duplicate ``consolidation_runs`` audit row).
"""

from __future__ import annotations

import json as _json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prism_service.inference import claude_cli
from prism_service.services.memory_ops.base import MemoryOperation

# Per-op model routing (task 9f61d484, claude-p-exit epic be898578).
#
# CLAUDE_MODEL_MAP is the SINGLE home of the claude-backend model pins: the
# opus id for the distill_procedural synthesis op lives HERE, never hardcoded
# in the op class (oracle: no hardcoded opus id remains in distill_procedural).
# distill's opus pin is the DEFAULT path (env unset) — micro-model distill
# quality is uncertain, so going local for distill is an explicit opt-in via
# PRISM_MEMORY_OPS_BACKEND=local (which routes EVERY op at the local backend).
CLAUDE_MODEL_MAP: dict[str, str] = {
    "distill_procedural": "claude-opus-4-20250514",
}
# LOCAL_MODEL_MAP pins per-op local models for the local backend; empty by
# default (ops fall through to local_llm.configured_model() — the
# bench-ranked micro model, benchmarks/micro_llm_selflearn). An op may also
# carry a ``local_model`` attribute that wins over the map.
LOCAL_MODEL_MAP: dict[str, str] = {}


def _op_backend(op_type: str) -> str:
    """Resolve the inference backend for one op. Default (env unset / any
    value but 'local') -> 'claude' (claude_cli.invoke with CLAUDE_MODEL_MAP
    routing). PRISM_MEMORY_OPS_BACKEND=local routes EVERY op at the keyless
    local micro model (local_llm.complete)."""
    backend = (os.environ.get("PRISM_MEMORY_OPS_BACKEND") or "").strip().lower()
    return "local" if backend == "local" else "claude"


def _resolve_model(op: MemoryOperation, backend: str) -> str:
    """Model id for `op` under `backend`.

    Claude: the op's own ``model`` (routing intact) else CLAUDE_MODEL_MAP's
    entry (distill_procedural -> opus) else "" (CLI default Sonnet).
    Local: the op's ``local_model`` else LOCAL_MODEL_MAP's entry else
    local_llm.configured_model() (the bench-ranked micro model)."""
    op_type = getattr(op, "op_type", "") or ""
    if backend == "local":
        from prism_service.inference import local_llm

        return (getattr(op, "local_model", "") or ""
                or LOCAL_MODEL_MAP.get(op_type)
                or local_llm.configured_model())
    return (getattr(op, "model", "") or "") or CLAUDE_MODEL_MAP.get(op_type) or ""


class _LocalOpResult:
    """claude_cli-result-shaped adapter for the local micro-LLM backend
    (mirrors memory_summary_worker._LocalBackendResult) so run_one's
    exit_code / final_text() / run_id / duration_s handling is untouched."""

    def __init__(self, text: str, run_id: str = "", duration_s: float = 0.0):
        self._text = text
        self.exit_code = 0
        self.run_id = run_id
        self.duration_s = duration_s

    def final_text(self) -> str:
        return self._text


def _invoke_local(*, prompt: str, model: str, project: str, op_type: str):
    """Route one memory op at the keyless local micro model. Every op's
    verdict is a JSON object, so json_mode=True. local_llm.complete records
    the run in the pi_run_log ledger (backend='local', purpose/project
    passthrough, input/output token split) — pi_runs is THE ledger for
    pi|local runs (task d1d4fe00); the run_id rides back on the result."""
    from prism_service.inference import local_llm

    t0 = time.perf_counter()
    out = local_llm.complete(
        prompt,
        model=model,
        json_mode=True,
        max_tokens=1024,
        purpose=f"memory-ops:{op_type}" if op_type else "memory-ops",
        project=project,
    )
    return _LocalOpResult(
        out.get("text") or "",
        run_id=out.get("run_id") or "",
        duration_s=round(time.perf_counter() - t0, 3),
    )


@dataclass
class OperationResult:
    ok: bool
    op_type: str = ""
    skipped: bool = False
    error: str = ""
    verdict: dict | None = None
    submitted: dict | None = None
    run_id: str = ""
    duration_s: float = 0.0
    item: Any = None
    detail: str = ""

    def to_dict(self) -> dict:
        d = {"ok": self.ok, "op_type": self.op_type, "skipped": self.skipped}
        if self.error:
            d["error"] = self.error
        if self.run_id:
            d["run_id"] = self.run_id
        if self.ok:
            d["verdict"] = self.verdict
            d["submitted"] = self.submitted
            d["duration_s"] = round(self.duration_s, 2)
        return d


def _strip_fence(raw_text: str) -> str:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].strip()
    return cleaned


def run_one(op: MemoryOperation, item: Any, project: str) -> OperationResult:
    """Drive one op tick against ``item``. Never raises for ordinary
    failures — returns an OperationResult.

    Short-circuits (``skipped=True``, ``ok=False``) when ``op.select(project)``
    yields nothing — nothing to do means we never pay for inference.
    """
    op_type = getattr(op, "op_type", "") or ""

    # Empty select() => no candidates => skip before any inference.
    try:
        selected = op.select(project)
    except Exception as exc:
        return OperationResult(
            ok=False, op_type=op_type, error=f"select failed: {exc}", item=item
        )
    if not selected:
        return OperationResult(ok=False, op_type=op_type, skipped=True, item=item)

    from prism_service.project_context import get_project
    ctx = get_project(project)
    scores_db = str(ctx._data_dir / "scores.db")
    if not Path(scores_db).exists():
        return OperationResult(
            ok=False, op_type=op_type, error="no scores.db", item=item
        )

    # Idempotency: only process a candidate that is still pending. A second
    # tick on the same (already-completed) item short-circuits to a skip.
    conn = sqlite3.connect(scores_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, status FROM consolidation_candidates WHERE id = ?",
            (item,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return OperationResult(
            ok=False, op_type=op_type, error="candidate not found", item=item
        )
    if row["status"] not in ("pending", "dispensed"):
        return OperationResult(
            ok=False, op_type=op_type, skipped=True,
            detail=f"candidate is {row['status']}", item=item,
        )

    from prism_service.services import source_service as ss
    try:
        source_dir = ss.source_dir_for(project)
    except Exception:
        source_dir = ctx._data_dir

    prompt = op.build_prompt(item, project)

    # Backend + model routing (task 9f61d484). Default -> claude_cli.invoke
    # with the op map's model (distill -> opus, else op.model else CLI
    # default). PRISM_MEMORY_OPS_BACKEND=local routes at the keyless local
    # micro model via local_llm.complete (json_mode — every verdict is JSON),
    # which itself records the pi_run_log ledger row (backend + token split).
    backend = _op_backend(op_type)
    model = _resolve_model(op, backend)
    try:
        if backend == "local":
            result = _invoke_local(
                prompt=prompt, model=model, project=project, op_type=op_type,
            )
        else:
            result = claude_cli.invoke(
                prompt=prompt,
                work_dir=str(source_dir),
                plugin_dir=str(source_dir),
                model=model,
                project=project,
                purpose=f"prism-{op_type}" if op_type else "prism-memory-op",
            )
    except Exception as exc:
        return OperationResult(
            ok=False, op_type=op_type,
            error=f"{backend} inference failed: {exc}", item=item,
        )

    raw_text = result.final_text() or ""
    if result.exit_code != 0 and not raw_text:
        return OperationResult(
            ok=False, op_type=op_type, error="claude returned no text",
            run_id=result.run_id, item=item,
        )

    from prism_service.services.janitor_service import JanitorService
    try:
        verdict = _json.loads(_strip_fence(raw_text))
    except Exception as exc:
        JanitorService(scores_db).abandon(item, reason=f"verdict not JSON: {exc}")
        return OperationResult(
            ok=False, op_type=op_type, error="verdict not valid JSON",
            run_id=result.run_id, item=item,
        )

    js = JanitorService(scores_db)
    try:
        submitted = js.submit(item, output_json=verdict, op_type=op_type)
    except Exception as exc:
        return OperationResult(
            ok=False, op_type=op_type, error=f"submit failed: {exc}",
            verdict=verdict, run_id=result.run_id, item=item,
        )
    if not submitted.get("accepted"):
        return OperationResult(
            ok=False, op_type=op_type,
            error=submitted.get("error") or "submit rejected",
            verdict=verdict, submitted=submitted, run_id=result.run_id,
            item=item,
        )

    # Op-specific side-effects (memory store / invalidate / prune).
    try:
        op.apply_verdict(item, verdict, project)
    except Exception as exc:
        return OperationResult(
            ok=False, op_type=op_type, error=f"apply_verdict failed: {exc}",
            verdict=verdict, submitted=submitted, run_id=result.run_id,
            item=item,
        )

    return OperationResult(
        ok=True, op_type=op_type, verdict=verdict, submitted=submitted,
        run_id=result.run_id, duration_s=result.duration_s, item=item,
    )
