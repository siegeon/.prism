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
import threading
import time
import uuid
from collections import deque
from typing import Iterator, Optional

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
    return entry


@contextlib.contextmanager
def pass_(kind: str, project: str = "*", detail: str = "") -> Iterator[None]:
    """Wrap one worker tick/sweep. Records it as running immediately on
    entry, and moves it into `recent` on exit -- ok=True unless the body
    raised, in which case ok=False and the exception propagates untouched
    (never swallow a worker's real exception)."""
    token = uuid.uuid4().hex[:12]
    started = time.time()
    with _LOCK:
        _running[token] = _entry(token, kind, project, detail, started)
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        elapsed_ms = (time.time() - started) * 1000.0
        with _LOCK:
            _running.pop(token, None)
        record(kind, project, detail, started, elapsed_ms, ok=ok)


def _matches(entry_project: str, project: Optional[str]) -> bool:
    if not project:
        return True
    return entry_project == project or entry_project == "*"


def snapshot(project: Optional[str] = None, limit: int = 20) -> dict:
    """Answer from memory only -- no sqlite, no git, no locks held across
    I/O. `running` is every currently in-flight pass (own live elapsed
    computed at read time); `recent` is the most recent `limit` completed
    passes, newest first."""
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
    return {"running": running_out, "recent": recent_out[: max(0, limit)]}


def _reset_for_tests() -> None:
    """Test-only: clear all state between tests that share this module."""
    with _LOCK:
        _running.clear()
        _recent.clear()
