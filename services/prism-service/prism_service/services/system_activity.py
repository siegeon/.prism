"""In-memory system activity feed -- lightweight, thread-safe, answers from
memory in well under 5ms (never touches sqlite or git).

The daemon runs many background passes (task_runner ticks, gate_adjudicator
sweeps, resume_actuator, deploy sweep, reap sweep, the language-alignment
worker, the drift reindexer, the maintenance-clock's brain-hygiene passes,
ship_worker) and until this module existed none of it was visible AS it
happened -- the owner only saw CPU burn and a dark /live canvas ("until we
can see the work being done most all things you say are likely not true").

Every worker records itself here via the `pass_()` context manager, which
marks the pass RUNNING the instant it is entered (so a slow, in-flight pass
shows a live elapsed counter instead of only appearing once it finishes)
and moves it to the `recent` ring buffer on exit.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Iterator, Optional

from prism_service.services import wakeups

_LOCK = threading.Lock()
_MAX_RECENT = 500

# token -> running entry (dict). A dict, not a list, so an in-flight pass
# can be looked up and removed by its own token even if two passes of the
# same kind/project overlap.
_running: dict[str, dict] = {}
_recent: "deque[dict]" = deque(maxlen=_MAX_RECENT)


def _entry(id_: str, kind: str, project: str, detail: str,
           started_at: float) -> dict:
    return {
        "id": id_,
        "kind": kind,
        "project": project or "*",
        "detail": detail,
        "started_at": started_at,
    }


def record(kind: str, project: str, detail: str, started_at: float,
           elapsed_ms: float, ok: bool = True) -> dict:
    """Record one COMPLETED pass directly (used by callers that already
    know start/elapsed rather than wrapping the call in `pass_()`).
    Returns the stored entry."""
    entry = _entry(uuid.uuid4().hex[:12], kind, project, detail, started_at)
    entry["elapsed_ms"] = round(elapsed_ms, 1)
    entry["ok"] = bool(ok)
    with _LOCK:
        _recent.appendleft(entry)
    # Task fix/polling (SSE follow-up): the SPA's SystemActivityPanel moved
    # off its own 1Hz poll onto GET /sse/changes, which is driven by
    # wakeups signals -- so a completed pass must SIGNAL, or the panel
    # would never refetch and would show nothing after the very first
    # answer. Best-effort/never raises, same as every other wakeups.signal
    # call site.
    wakeups.signal("activity", project or "*")
    return entry


_IDLE_MIN_INTERVAL_S = 600.0  # collapse idle passes to at most 1 entry/10min
_last_idle_at: dict[tuple[str, str], float] = {}
_idle_skipped: dict[tuple[str, str], int] = {}


@contextlib.contextmanager
def pass_(kind: str, project: str = "*", detail: str = "") -> Iterator[dict]:
    """Wrap one worker tick/sweep. Records it as running immediately on
    entry, and moves it into `recent` on exit -- ok=True unless the body
    raised, in which case ok=False and the exception propagates untouched
    (never swallow a worker's real exception).

    Yields the mutable entry dict, which doubles as the `info` handle for
    two independent, composable opt-ins:

    - Set `entry["active"] = False` when the pass found NOTHING to do (a
      sweep with zero eligible tasks, no pending gates, nothing to
      reap, ...). A caller that never touches this keeps the old
      behaviour (every pass recorded). An inactive pass is never lost
      silently: it collapses into at most one "idle" entry per
      `kind`+`project` per `_IDLE_MIN_INTERVAL_S`, carrying how many quiet
      passes it stands in for, so the System Activity panel reads QUIET
      on an idle system instead of climbing on a clock tick that did
      nothing (owner 2026-09-13).
    - Set `entry["detail"] = "..."` once the body learns its real numbers
      partway through (e.g. drift's own "N candidates, M changed, K
      embedded" summary, known only after the reindex call returns) to
      have THAT string recorded at completion instead of the placeholder
      passed in at entry (task: livehang round 6).

    Both are optional and independent -- a pass can set either, both, or
    neither."""
    token = uuid.uuid4().hex[:12]
    started = time.time()
    entry = _entry(token, kind, project, detail, started)
    entry["active"] = True
    with _LOCK:
        _running[token] = entry
    # A pass starting to RUN is itself real activity worth pushing -- an
    # SSE-driven panel must not wait for completion to see it lit.
    wakeups.signal("activity", project or "*")
    ok = True
    try:
        yield entry
    except BaseException:
        ok = False
        raise
    finally:
        elapsed_ms = (time.time() - started) * 1000.0
        with _LOCK:
            _running.pop(token, None)
        final_detail = entry.get("detail", detail)
        active = bool(entry.get("active", True))
        if active or not ok:
            key = (kind, project or "*")
            with _LOCK:
                _last_idle_at.pop(key, None)
                _idle_skipped.pop(key, None)
            record(kind, project, final_detail, started, elapsed_ms, ok=ok)
        else:
            _record_idle(kind, project, final_detail, started, elapsed_ms)


def _record_idle(kind: str, project: str, detail: str, started: float,
                  elapsed_ms: float) -> None:
    key = (kind, project or "*")
    now = time.time()
    with _LOCK:
        last = _last_idle_at.get(key, 0.0)
        if now - last < _IDLE_MIN_INTERVAL_S:
            _idle_skipped[key] = _idle_skipped.get(key, 0) + 1
            return
        skipped = _idle_skipped.pop(key, 0)
        _last_idle_at[key] = now
    idle_detail = f"idle · {skipped} skipped" if skipped else "idle"
    record(kind, project, idle_detail or detail, started, elapsed_ms, ok=True)


def _matches(entry_project: str, project: Optional[str]) -> bool:
    if not project:
        return True
    return entry_project == project or entry_project == "*"


_QUIET_WINDOW_S = 60.0


def _local_snapshot(project: Optional[str] = None, limit: int = 20) -> dict:
    """Answer from THIS process's memory only -- no sqlite, no git, no
    locks held across I/O. `running` is every currently in-flight pass (own
    live elapsed computed at read time); `recent` is the most recent
    `limit` completed passes, newest first. `quiet` is True when nothing is
    running and no non-idle pass completed in the last minute -- the
    panel's QUIET header (owner 2026-09-13: an idle system should read as
    idle, not as churn)."""
    now = time.time()
    with _LOCK:
        running_snap = list(_running.values())
        recent_snap = list(_recent)
    running_out = []
    for r in running_snap:
        if not _matches(r["project"], project):
            continue
        d = dict(r)
        d["elapsed_ms"] = round((now - d["started_at"]) * 1000.0, 1)
        running_out.append(d)
    running_out.sort(key=lambda e: e["started_at"])
    recent_out = [r for r in recent_snap if _matches(r["project"], project)]
    quiet = not running_out and not any(
        (now - r["started_at"]) <= _QUIET_WINDOW_S
        and not str(r.get("detail", "")).startswith("idle")
        for r in recent_out
    )
    return {"running": running_out, "recent": recent_out[: max(0, limit)],
            "quiet": quiet}


# ---------------------------------------------------------------------------
# Cross-process export (task: worker-host process split, 2026-09-13).
#
# Once PRISM_WORKERS_PROCESS=1 moves the standing workers into a separate
# OS process from the API, every pass_()/record() call happens in THAT
# process -- so the API's own in-memory _running/_recent stay empty and the
# Live page's System Activity panel would look permanently QUIET even
# while workers are busy. The worker host periodically exports its local
# snapshot to a small JSON file under the data dir; snapshot() here merges
# it in transparently, so callers (the /api/system/activity route) need no
# changes at all -- same signature, same shape, same "keep its in-process
# API" contract.
# ---------------------------------------------------------------------------

_EXPORT_INTERVAL_S = 0.5
_EXPORT_MAX_AGE_S = 5.0  # a stale export (exporting process died) never
                          # haunts the panel as a ghost "running" pass


def _export_path() -> Optional[Path]:
    try:
        from prism_service.data_dir import resolve_data_dir
        return resolve_data_dir() / "system_activity.json"
    except Exception:
        return None


def _atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, path)


def export_once() -> None:
    """Write THIS process's local snapshot (unfiltered, full ring) to the
    cross-process export file, so another process's snapshot() can merge
    it in. Cheap: one in-memory read + one atomic file write. Safe to call
    from a process that never runs pass_()/record() at all (writes an
    empty-but-fresh export)."""
    path = _export_path()
    if path is None:
        return
    try:
        data = _local_snapshot(project=None, limit=_MAX_RECENT)
        _atomic_write_json(path, {"written_at": time.time(), **data})
    except Exception:
        pass


def start_activity_exporter(interval_s: float = _EXPORT_INTERVAL_S) -> None:
    """Own daemon thread: periodically export this process's activity feed.
    Called by the worker host once it starts running passes, so the API
    process (which runs none once PRISM_WORKERS_PROCESS=1) can still show
    them on the Live page."""
    def _loop() -> None:
        while True:
            export_once()
            time.sleep(interval_s)
    threading.Thread(target=_loop, daemon=True,
                      name="prism-activity-exporter").start()


def _read_export() -> Optional[dict]:
    path = _export_path()
    if path is None or not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if time.time() - float(raw.get("written_at", 0)) > _EXPORT_MAX_AGE_S:
        return None
    return raw


def snapshot(project: Optional[str] = None, limit: int = 20) -> dict:
    """`_local_snapshot()` merged with the newest cross-process export, if
    any (see module docstring above) -- an old/missing export changes
    nothing, so a process running PRISM_WORKERS_PROCESS=0 (workers still
    in-process) behaves exactly as before this merge existed."""
    local = _local_snapshot(project=project, limit=limit)
    external = _read_export()
    if not external:
        return local

    ext_running = [r for r in external.get("running", [])
                   if _matches(r.get("project", "*"), project)]
    ext_recent = [r for r in external.get("recent", [])
                  if _matches(r.get("project", "*"), project)]

    running_out = list(local["running"]) + ext_running
    running_out.sort(key=lambda e: e.get("started_at", 0))

    seen_ids = {r["id"] for r in local["recent"]}
    merged_recent = list(local["recent"]) + [
        r for r in ext_recent if r["id"] not in seen_ids
    ]
    merged_recent.sort(key=lambda e: e.get("started_at", 0), reverse=True)

    now = time.time()
    quiet = local["quiet"] and not ext_running and not any(
        (now - r.get("started_at", 0)) <= _QUIET_WINDOW_S
        and not str(r.get("detail", "")).startswith("idle")
        for r in ext_recent
    )
    return {"running": running_out, "recent": merged_recent[: max(0, limit)],
            "quiet": quiet}


def _reset_for_tests() -> None:
    """Test-only: clear all state between tests that share this module --
    including any cross-process export file, which otherwise outlives any
    one test (the suite pins PRISM_DATA_DIR to one throwaway dir for the
    whole session, not per-test)."""
    with _LOCK:
        _running.clear()
        _recent.clear()
        _last_idle_at.clear()
        _idle_skipped.clear()
    path = _export_path()
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
