"""RED scaffold — the task detail page updates itself without polling
(walking-skeleton child 792ebf6f of epic 2d480b08).

Pins: TaskService.update publishes exactly one "task_updated" event onto
prism_service.events.bus after a REAL (non-no-op) change commits, and
publishes NOTHING on a no-op update (task_service.py:595-596's existing
short-circuit). A conductor-style call (workflow_step/gate_state kwargs,
no HTTP layer) publishes too, because the publish lives at the single
chokepoint every mutation path already funnels through.

TaskDetailPage.tsx no longer polls via setInterval, and its EventSource
onmessage handler filters on BOTH the event type and the route's task id
before calling load() — the SPA ships no JS test runner, so this is
pinned by reading the ACTUAL TSX source, brace-balanced from the
handler's own start (never a fixed window or a comment), matching the
convention in test_heavy_polls_are_scoped.py.

FAILS TODAY because:
  - TaskService.update never imports or calls prism_service.events.bus at
    all (services/prism-service/prism_service/services/task_service.py:571-650).
  - TaskDetailPage.tsx:959 is still
    `useEffect(() => { load(); const t = setInterval(load, 5000); ...})`.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _task_svc(tmp_path, name="tasks.db", project="prism"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name), project=project)


def _drain(q):
    """Non-blocking drain of an asyncio.Queue's current contents."""
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _subscribe():
    """Subscribe to the real process-wide bus from a running event loop,
    the same way the /sse/sessions endpoint does (routes/sse.py:28)."""
    from prism_service.events import bus
    return bus, bus.subscribe()


# ---------------------------------------------------------------------------
# Backend: TaskService.update publishes onto the real process-wide bus
# (AC-1, AC-2, AC-3).
# ---------------------------------------------------------------------------

def test_update_publishes_task_updated_event(tmp_path):
    """A REAL (non-no-op) update through TaskService.update must publish
    exactly one task_updated event carrying task_id/project/fields."""
    svc = _task_svc(tmp_path)
    t = svc.create("probe task")

    async def _run():
        bus, q = _subscribe()
        try:
            svc.update(t.id, status="blocked")
            await asyncio.sleep(0.05)
            return _drain(q)
        finally:
            bus.unsubscribe(q)

    events = asyncio.run(_run())
    assert len(events) == 1, (
        f"expected exactly one task_updated event, got {events!r}")
    ev = events[0]
    assert ev.get("type") == "task_updated", ev
    assert ev.get("task_id") == t.id, ev
    assert ev.get("project") == "prism", ev
    assert "status" in (ev.get("fields") or []), ev


def test_noop_update_publishes_nothing(tmp_path):
    """A no-op update (kwargs equal to current values) must publish
    nothing — exercises the existing task_service.py:595-596 short-circuit."""
    svc = _task_svc(tmp_path)
    t = svc.create("probe task", priority=5)

    async def _run():
        bus, q = _subscribe()
        try:
            svc.update(t.id, priority=5)
            await asyncio.sleep(0.05)
            return _drain(q)
        finally:
            bus.unsubscribe(q)

    events = asyncio.run(_run())
    assert events == [], f"no-op update must publish nothing, got {events!r}"


def test_conductor_style_update_also_publishes(tmp_path):
    """A conductor-shaped call (workflow_step/gate_state kwargs, no HTTP
    layer) must also publish — the fix lives at the chokepoint, not the
    REST route."""
    svc = _task_svc(tmp_path)
    t = svc.create("probe task")

    async def _run():
        bus, q = _subscribe()
        try:
            svc.update(t.id, workflow_step="plan", gate_state="pending")
            await asyncio.sleep(0.05)
            return _drain(q)
        finally:
            bus.unsubscribe(q)

    events = asyncio.run(_run())
    assert len(events) == 1, (
        f"conductor-style update must publish one event, got {events!r}")
    assert events[0].get("type") == "task_updated", events[0]


# ---------------------------------------------------------------------------
# Frontend source contracts (AC-4, AC-5) — no JS test runner, so the ACTUAL
# TSX source is parsed, brace-balanced from each construct's own start,
# matching the convention in test_heavy_polls_are_scoped.py.
# ---------------------------------------------------------------------------

def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _walk_braces(src: str, brace: int) -> str:
    depth = 0
    j = brace
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces scanning from index {brace}")


def _brace_body(src: str, marker: str) -> str:
    i = src.index(marker)
    return _walk_braces(src, src.index("{", i))


def test_task_detail_page_has_no_poll_interval():
    """TaskDetailPage.tsx must no longer poll via setInterval — the SSE
    subscription replaces the 5s re-pull entirely (AC-4)."""
    src = _read("pages/TaskDetailPage.tsx")
    assert "setInterval" not in src, (
        "TaskDetailPage.tsx must no longer contain setInterval — expected "
        "an EventSource-driven refresh instead of a fixed-interval poll")


def test_task_detail_page_opens_event_source():
    """The page must actually open an EventSource — a naive fix that
    deletes the poll without adding a live subscription would leave the
    page dead (AC-5 prerequisite)."""
    src = _read("pages/TaskDetailPage.tsx")
    assert "new EventSource(" in src, (
        "TaskDetailPage.tsx must open a new EventSource to receive push "
        "updates in place of the removed poll")


def test_task_detail_page_onmessage_filters_type_and_task_id():
    """The onmessage handler body (brace-balanced from its own start) must
    reference both the event type ("task_updated") and the route's own
    `id` BEFORE calling load() — never the SessionsPage.tsx:79-82 blanket
    `es.onmessage = () => load()` anti-pattern named as this task's
    likely_misfire (AC-5)."""
    src = _read("pages/TaskDetailPage.tsx")
    assert ".onmessage" in src, (
        "TaskDetailPage.tsx must attach an onmessage handler to the "
        "EventSource it opens")
    body = _brace_body(src, ".onmessage")
    assert "task_updated" in body, (
        f"onmessage handler must check for the task_updated event type "
        f"before refetching; got body: {body!r}")
    assert re.search(r"\bid\b", body), (
        "onmessage handler must compare the event's task id against this "
        f"route's own `id` (from useParams) before refetching; got body: "
        f"{body!r}")
    assert "load(" in body, (
        f"onmessage handler must call load() when the event matches; got "
        f"body: {body!r}")
    type_idx = body.index("task_updated")
    load_idx = body.index("load(")
    assert type_idx < load_idx, (
        "the type/task_id check must GATE the load() call (occur before "
        f"it in source), not follow an unconditional refetch; got body: "
        f"{body!r}")
