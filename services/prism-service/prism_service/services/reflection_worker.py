"""Opt-in background reflection daemon.

PRISM is a zero-LLM service by default — reflection_runner shells out
to `claude -p`, which COSTS tokens against the user's subscription.
That's why the panel at /api/consolidation/workers used to call this
out as "NOT RUNNING by design": we don't want a daemon burning the
user's quota the moment the service boots.

v6.0.18 adds an explicit opt-in:

  PRISM_REFLECTION_WORKER=on          # off by default
  PRISM_REFLECTION_WORKER_INTERVAL=900  # seconds between candidates, default 900 (15 min)
  PRISM_REFLECTION_WORKER_PROJECT=prism # single-project mode; omit to sweep all
  PRISM_REFLECTION_WORKER_MAX_PER_CYCLE=1

When on, the loop picks the oldest pending consolidation_candidate
per project, dispatches reflection_runner.run_one, sleeps the
interval, and repeats. Failures (claude not logged in, transient
errors) are logged but never crash the thread.
"""

from __future__ import annotations

import json as _json
import os
import sqlite3
import sys as _sys
import threading
import time
from pathlib import Path


def _is_noise_candidate(scores_db: str, candidate_id: str) -> bool:
    """True when a candidate carries no usable signal — task_id NULL AND
    every signal_counts bucket zero. Mirrors the v6.2.18 enqueue-time
    noise filter so the self-draining loop reflects real signal only and
    never burns claude tokens on an empty brief."""
    try:
        conn = sqlite3.connect(scores_db, timeout=5.0)
        try:
            row = conn.execute(
                "SELECT task_id, scope_json FROM consolidation_candidates "
                "WHERE id=?", (candidate_id,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    if not row:
        return False
    if row[0] is not None:
        return False
    try:
        sc = (_json.loads(row[1] or "{}").get("signal_counts")) or {}
    except Exception:
        sc = {}
    return all(int(sc.get(k) or 0) == 0 for k in (
        "pushbacks", "bg_signals", "tool_failures", "memory_writes",
    ))


def _env_truthy(name: str, default: bool = False) -> bool:
    """Mirror memory_summary_worker._env_truthy: tri-state with a default.

    Recognises on/off synonyms; anything unset returns ``default``. The
    reflection worker now defaults ON (the learning loop must self-drain),
    so start_reflection_worker spawns without an explicit
    PRISM_REFLECTION_WORKER=on, while =off still fully opts out.
    """
    raw = os.environ.get(name, "").lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def is_enabled() -> bool:
    """Honor PRISM_REFLECTION_WORKER=off but default to ON.

    Surfaced for /api/consolidation/workers so the activity panel's
    'running' dot reflects reality, and read by start_reflection_worker.
    """
    return _env_truthy("PRISM_REFLECTION_WORKER", default=True)


def _interval_s() -> int:
    try:
        return max(60, int(os.environ.get("PRISM_REFLECTION_WORKER_INTERVAL", "900")))
    except ValueError:
        return 900


def _projects_in_scope() -> list[str]:
    """Return the list of projects this worker should sweep. Defaults to
    every known project; PRISM_REFLECTION_WORKER_PROJECT pins it."""
    pinned = os.environ.get("PRISM_REFLECTION_WORKER_PROJECT", "").strip()
    if pinned:
        return [pinned]
    from prism_service.config import list_projects
    try:
        return list_projects()
    except Exception:
        return []


def _max_per_cycle() -> int:
    try:
        return max(1, int(os.environ.get("PRISM_REFLECTION_WORKER_MAX_PER_CYCLE", "1")))
    except ValueError:
        return 1


def _log(msg: str) -> None:
    print(f"[reflection_worker] {msg}", file=_sys.stderr, flush=True)


def run_once() -> dict:
    """Sweep every in-scope project once. Returns a small summary so
    callers/tests can assert what happened without inspecting logs.

    For each project: claim up to N (max_per_cycle) pending candidates
    and dispatch reflection_runner.run_one for each.
    """
    from prism_service.services.reflection_runner import (
        next_pending_candidate_id, run_one,
    )
    from prism_service.project_context import get_project

    n_max = _max_per_cycle()
    summary: dict = {"projects": [], "ran": 0, "errors": 0, "skipped": 0}

    for project in _projects_in_scope():
        try:
            ctx = get_project(project)
        except Exception as exc:
            _log(f"{project}: get_project failed: {exc}")
            summary["errors"] += 1
            continue
        scores_db = str(ctx._data_dir / "scores.db")
        if not Path(scores_db).exists():
            summary["skipped"] += 1
            continue

        ran_here = 0
        seen_noise: set[str] = set()
        for _ in range(n_max):
            cid = next_pending_candidate_id(scores_db, exclude=seen_noise)
            if not cid:
                break
            # v6.2.29 — honor the noise filter at drain time. A no-signal
            # candidate (task_id NULL, all signal_counts zero) is skipped,
            # never dispatched to claude. Exclude it so the loop advances
            # to the next real-signal candidate instead of spinning.
            if _is_noise_candidate(scores_db, cid):
                _log(f"{project}: skip noise candidate={cid}")
                seen_noise.add(cid)
                summary["skipped"] += 1
                continue
            _log(f"{project}: dispatching reflection for candidate={cid}")
            try:
                result = run_one(project=project, candidate_id=cid)
            except Exception as exc:
                _log(f"{project}: run_one raised: {exc}")
                summary["errors"] += 1
                break
            ran_here += 1
            summary["ran"] += 1
            if result.ok:
                _log(
                    f"{project}: candidate={cid} ok score="
                    f"{(result.verdict or {}).get('qualitative_score')} "
                    f"memories={len(result.memories_stored or [])} "
                    f"duration_s={round(result.duration_s, 1)}"
                )
            else:
                _log(f"{project}: candidate={cid} failed: {result.error}")
                summary["errors"] += 1
                # Don't continue this project on auth failure — every
                # subsequent attempt will fail the same way until login.
                if "not logged in" in (result.error or "").lower():
                    break
        summary["projects"].append({"project": project, "ran": ran_here})
    return summary


def _loop() -> None:
    interval = _interval_s()
    _log(f"started; interval={interval}s")
    while True:
        try:
            run_once()
        except Exception as exc:
            _log(f"run_once raised: {exc}")
        time.sleep(interval)


def start_reflection_worker() -> threading.Thread | None:
    """Start the daemon thread when PRISM_REFLECTION_WORKER is truthy.
    Returns the thread (or None when disabled). Idempotent if called
    multiple times — the lifespan call site is the only legitimate
    one, but tests may opt in directly."""
    if not is_enabled():
        return None
    t = threading.Thread(
        target=_loop, name="prism-reflection-worker", daemon=True,
    )
    t.start()
    return t
