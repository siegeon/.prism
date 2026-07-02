"""Drive endpoint — ONE door through the planning gates (task 4e28dcab,
C4 of the PI-orchestration build, parent 81b23574 FR-4).

POST /api/agent/drive accepts {ask?, task_id?, project?, session_id?}:
creates the task from the ask when no task_id is given, then runs
services/drive_engine.DriveEngine.plan — the deterministic server-side
state machine — so the PI surfaces stop hand-stepping conductor tools
through text interception. Every conductor step the engine takes is
telemetered through the EXISTING ingest paths (no new ledger):

  * one pi_runs row per step via services.pi_run_log.record_run with
    purpose="drive:<sdlc-step>" (the step label rides purpose because
    the manifest schema is fixed) and backend="drive", task-attributed;
  * one agent_runs row per step via
    services.agent_runs_data.upsert_agent_run (real `step` column),
    keyed (run_id, agent_id="drive-engine", step) with session_id.

Telemetry is best-effort: a ledger failure never breaks the drive.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project

router = APIRouter()


def _effective_project(query_project: Optional[str], body: dict) -> str:
    """Query param wins; string body['project'] is the MCP-shaped
    fallback; 'default' last (the conductor-API convention)."""
    if query_project:
        return query_project
    body_project = body.get("project")
    if isinstance(body_project, str) and body_project.strip():
        return body_project.strip()
    return "default"


class _TelemetryConductor:
    """Duck-typed conductor wrapper: forwards advance_task/gate_decide to
    the real ConductorService and records one step-labeled row per call
    into BOTH existing ledgers. Never raises from the recording path."""

    def __init__(self, inner: Any, *, run_id: str, project: str,
                 task_id: str, session_id: str, scores_db: str) -> None:
        self._inner = inner
        self._run_id = run_id
        self._project = project
        self._task_id = task_id
        self._session_id = session_id
        self._scores_db = scores_db
        self.steps: list[dict] = []

    def advance_task(self, task_id: str, **kw) -> dict:
        t0 = time.perf_counter()
        res = self._inner.advance_task(task_id, **kw) or {}
        ms = (time.perf_counter() - t0) * 1000.0
        # The step whose work the advance concludes. The first advance
        # has from_step "" (label: start); a refused advance (desync)
        # may carry no from_step at all — label it for the audit trail.
        step = str(res.get("from_step") or "") or (
            "start" if res.get("ok") else "advance-refused")
        self._record(step, "advance", res, ms,
                     extra={"to_step": res.get("to_step")})
        return res

    def gate_decide(self, task_id: str, *args, **kw) -> dict:
        t0 = time.perf_counter()
        res = self._inner.gate_decide(task_id, *args, **kw) or {}
        ms = (time.perf_counter() - t0) * 1000.0
        step = str(res.get("gate_step") or "") or "gate"
        self._record(step, "gate", res, ms,
                     extra={"gate_state": res.get("gate_state")})
        return res

    def _record(self, step: str, kind: str, res: dict, ms: float,
                extra: Optional[dict] = None) -> None:
        ok = bool(res.get("ok"))
        entry = {"step": step, "kind": kind, "ok": ok, "ms": round(ms, 1)}
        for k, v in (extra or {}).items():
            if v is not None:
                entry[k] = v
        self.steps.append(entry)
        # pi_runs: the step label rides purpose (fixed manifest schema).
        try:
            from prism_service.services import pi_run_log
            pi_run_log.record_run(
                backend="drive", model="", purpose=f"drive:{step}",
                project=self._project, task_id=self._task_id,
                duration_ms=ms, turns=0, ok=ok,
                error="" if ok else str(res.get("reason") or ""),
            )
        except Exception:
            pass
        # agent_runs: the spine with a REAL step column.
        try:
            from prism_service.services.agent_runs_data import (
                upsert_agent_run,
            )
            now = time.time()
            upsert_agent_run(self._scores_db, {
                "run_id": self._run_id,
                "workflow_name": "pi-drive",
                "task_id": self._task_id,
                "session_id": self._session_id,
                "agent_id": "drive-engine",
                "parent_agent_id": "",
                "role": "drive",
                "step": step,
                "model": "",
                "started_at": now - ms / 1000.0,
                "ended_at": now,
                "duration_ms": int(ms),
                "tokens": 0,
                "tool_uses": 1,
                "ok": ok,
                "gate_state": str(res.get("gate_state") or ""),
                "verdict_summary": str(res.get("reason") or ""),
                "evidence_ref": "",
            })
        except Exception:
            pass

    def __getattr__(self, name: str) -> Any:
        # Anything beyond the two drive seams passes straight through.
        return getattr(self._inner, name)


@router.post("/drive")
def drive(
    project: Optional[str] = Query(None), body: dict = Body(...),
) -> dict:
    """One POST through the planning gates: ask/task_id in, the
    DriveEngine result out. Engine refusals (unknown task, latched
    rubric gate) are 200 {ok:false, reason} — structured, never a 500
    masquerade; contract errors are 422/404."""
    effective = _effective_project(project, body)
    try:
        ctx = get_project(effective)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {effective}: {exc}")

    ask = str(body.get("ask") or "").strip()
    task_id = str(body.get("task_id") or "").strip()
    session_id = str(body.get("session_id") or "").strip()
    if not ask and not task_id:
        raise HTTPException(422, "one of 'ask' or 'task_id' is required")

    created = False
    if not task_id:
        from prism_service.models.task import TITLE_MAX_LEN

        try:
            task = ctx.task_svc.create(
                title=ask[:TITLE_MAX_LEN], description=ask,
                tags=["pi-drive"],
            )
        except ValueError as exc:
            raise HTTPException(422, f"cannot create task from ask: {exc}")
        task_id = task.id
        created = True

    # scores.db schema (incl. agent_runs) is owned by the brain engine;
    # touch it lazily so a fresh project's telemetry rows can land.
    # Best-effort — a brain init failure never blocks the drive.
    try:
        _ = ctx.brain_svc
    except Exception:
        pass

    run_id = uuid.uuid4().hex[:12]
    conductor = _TelemetryConductor(
        ctx.conductor_svc,
        run_id=run_id,
        project=effective,
        task_id=task_id,
        session_id=session_id,
        scores_db=str(ctx._data_dir / "scores.db"),
    )

    from prism_service.services.drive_engine import DriveEngine

    engine = DriveEngine(conductor, ctx.task_svc, memory_svc=ctx.memory_svc)
    result = engine.plan(task_id, session_id=session_id)
    result["run_id"] = run_id
    result["created"] = created
    result["steps"] = conductor.steps
    return result
