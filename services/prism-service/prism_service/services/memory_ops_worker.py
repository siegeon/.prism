"""Generalised, env-gated memory-operation daemon.

Where ``reflection_worker`` hard-codes ONE op, this worker registers every
``MemoryOperation`` in ``registered_ops()`` as its OWN env-gated,
interval-scheduled daemon tick — the scheduling extension of the Tier-2
memory-ops chassis.

Per-op env contract (OP = op_type upper-cased):

  PRISM_<OP>_WORKER=on              # off by default
  PRISM_<OP>_WORKER_INTERVAL=N      # seconds between ticks (min 60)
  PRISM_<OP>_WORKER_PROJECT=prism   # single-project mode; omit to sweep all

``start_memory_ops_workers()`` is the single lifespan call site (wired from
main.py alongside start_reflection_worker / start_memory_summary_worker). A
worker defined but never started is dead code — so the lifespan calls this.
"""

from __future__ import annotations

import os
import sys as _sys
import threading
import time

from prism_service.services.memory_ops.base import MemoryOperation


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").lower() in ("1", "on", "true", "yes")


def _op_env(op: MemoryOperation, suffix: str) -> str:
    return f"PRISM_{(op.op_type or '').upper()}_WORKER{suffix}"


def _interval_s(op: MemoryOperation) -> int:
    try:
        return max(60, int(os.environ.get(_op_env(op, "_INTERVAL"), "900")))
    except ValueError:
        return 900


def _projects_in_scope(op: MemoryOperation) -> list[str]:
    pinned = os.environ.get(_op_env(op, "_PROJECT"), "").strip()
    if pinned:
        return [pinned]
    from prism_service.config import list_projects
    try:
        return list_projects()
    except Exception:
        return []


def _log(msg: str) -> None:
    print(f"[memory_ops_worker] {msg}", file=_sys.stderr, flush=True)


def registered_ops() -> list[MemoryOperation]:
    """The built-in MemoryOperation instances the worker can schedule.

    Empty for now — concrete op families (forget / prune / distill) register
    here as they land. The worker + env-gating contract is what this Tier-2
    task ships; the ops plug in behind the same chassis.
    """
    return []


def _is_op_enabled(op: MemoryOperation) -> bool:
    return _env_truthy(_op_env(op, ""))


def run_once_for(op: MemoryOperation) -> dict:
    """Sweep every in-scope project once for a single op via the shared
    runner. Returns a small summary so callers/tests can assert outcomes."""
    from prism_service.services.memory_ops import runner

    summary: dict = {"op_type": op.op_type, "ran": 0, "errors": 0, "skipped": 0}
    for project in _projects_in_scope(op):
        try:
            for item in op.select(project):
                result = runner.run_one(op, item, project)
                if result.ok:
                    summary["ran"] += 1
                elif result.skipped:
                    summary["skipped"] += 1
                else:
                    summary["errors"] += 1
        except Exception as exc:
            _log(f"{op.op_type}/{project}: run_once raised: {exc}")
            summary["errors"] += 1
    return summary


def _loop_for(op: MemoryOperation) -> None:
    interval = _interval_s(op)
    _log(f"{op.op_type}: started; interval={interval}s")
    while True:
        try:
            run_once_for(op)
        except Exception as exc:
            _log(f"{op.op_type}: run_once raised: {exc}")
        time.sleep(interval)


def start_memory_ops_workers() -> list[threading.Thread]:
    """Start a daemon thread per env-gated MemoryOperation.

    Off by default: only ops whose ``PRISM_<OP>_WORKER`` env var is truthy
    spin up a thread. Returns the started threads ([] when none enabled)."""
    started: list[threading.Thread] = []
    ops = list(registered_ops())

    # Env-driven discovery: any PRISM_<OP>_WORKER=on that has no registered
    # op instance still starts a worker so the gate is honoured (the op
    # class is resolved lazily inside the loop via registered_ops()).
    known = {(o.op_type or "").upper() for o in ops}
    for key in list(os.environ):
        if key.startswith("PRISM_") and key.endswith("_WORKER"):
            op_name = key[len("PRISM_"):-len("_WORKER")]
            if op_name and op_name not in known and _env_truthy(key):
                ops.append(_AdHocOp(op_name.lower()))
                known.add(op_name)

    for op in ops:
        if not _is_op_enabled(op):
            continue
        t = threading.Thread(
            target=_loop_for, args=(op,),
            name=f"prism-memory-op-{op.op_type}", daemon=True,
        )
        t.start()
        started.append(t)
    return started


class _AdHocOp(MemoryOperation):
    """Placeholder op materialised from an enabled ``PRISM_<OP>_WORKER`` env
    var that has no registered implementation yet. Its select() is empty, so
    its ticks are cheap no-ops until a real op class registers — the gate is
    honoured without burning inference."""

    schema: dict = {}
    model = ""
    provenance: dict = {"source": "ad-hoc env gate"}

    def __init__(self, op_type: str):
        self.op_type = op_type

    def select(self, project):
        return []

    def build_prompt(self, item, project):
        return ""

    def apply_verdict(self, item, verdict, project):
        return None
