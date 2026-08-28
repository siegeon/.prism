"""Task detail page stops hiding behind a poll loop (task 2d480b08).

Design pinned here matches this task's OWN plan_doc (D-1..D-6), already
recorded on the task before this step ran:

  D-1  TaskService.update() publishes {project, type:"task.changed",
       task_id, fields:{...changed scalars...}, updated_at} to
       prism_service.events.bus, ONLY when something real changed (after
       the :596 no-op early return) and NEVER the full row.
  D-2  The publish can never break a write — a bus/serialization failure
       must be swallowed, the task write still commits.
  D-3  A task_id-scoped SSE route, GET /sse/tasks?project=&task_id=,
       mirrors sse_sessions's existing project filter (routes/sse.py).
  D-4  The client PATCHES local state from the pushed event's `fields`;
       the onmessage handler never calls load()/api.get() — that would be
       the SessionsPage.tsx:80 `es.onmessage = () => load();`
       refetch-everything anti-pattern named in this task's own
       likely_misfire.
  D-6  Where a poll survives (lib/version.ts's /api/version fallback,
       LiveBar.tsx's /api/conductor/state poll) it must be GATED on SSE
       health, not fire unconditionally on a bare interval — dropping the
       poll outright would repeat the PR #257 mistake of losing a real
       safety property (a throttled tab whose SSE stalled) along with the
       code around it.
  D-7  `activity` ({state, task_motion_s, session_quiet_s, ...}) is
       computed server-side ONLY inside GET /api/tasks/{id}
       (ConductorService.activity_for) — it is not a DB column, so it can
       never ride `fields` above. A live task.changed publish must ALSO
       carry a freshly-computed `activity` key (via a late-bound
       ConductorService, TaskService.attach_conductor_service), and the
       client's SSE handler must merge it into task state the same way it
       merges `fields` — otherwise a watching browser tab can never see a
       fresh activity reading without a full refetch, and reads real,
       advancing work as frozen/stalled.

FAILS TODAY because:
  - TaskService.update() has no call into prism_service.events.bus at all
    (the task's own likely_misfire: events.py is published to only for
    session_outcome/skill_usage today).
  - routes/sse.py has no /tasks route.
  - TaskDetailPage.tsx still 5s-polls GET /api/tasks/<id>
    (`setInterval(load, 5000)`) and has no EventSource at all.
  - LiveBar.tsx still 5s-polls GET /api/conductor/state
    (`setInterval(tick, 5000)`) unconditionally.
  - lib/version.ts's fallback poll (`setInterval(poll, 15000)`) fires
    unconditionally at watchdog start, not gated on SSE health.

The SPA ships no JS test runner, so the TSX/TS-facing acceptance criteria
are pinned by asserting the ACTUAL source, brace-balanced from each
construct's own start (never a fixed character window or a comment),
matching the convention in test_heavy_polls_are_scoped.py.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


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


def _subscribe_callback_body(src: str) -> str:
    """Body of the callback passed to `subscribeStream(url, (…) => { … })`.

    Added 2026-08-18 (task b835f639). Deliberately NOT _brace_body: the url is
    a template literal whose `${project}` interpolation contains the first `{`
    after the marker, so a naive scan would walk the literal's braces instead
    of the callback's. Anchors on the arrow that follows the argument list.
    """
    i = src.index("subscribeStream(")
    arrow = src.index("=>", i)
    return _walk_braces(src, src.index("{", arrow))


def _fn_body_from_signature_line(src: str, marker: str) -> str:
    i = src.index(marker)
    eol = src.index("\n", i)
    return _walk_braces(src, src.rindex("{", i, eol))


# ---------------------------------------------------------------------------
# Backend seam: TaskService.update() publishes a LEAN, real bus event (D-1/D-2)
# ---------------------------------------------------------------------------

def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"), project="prism")


async def _drain_after(coro_body):
    """Subscribe to the REAL prism_service.events.bus (no injected double,
    per the plan's own 'seam test' note), run `coro_body(q)`, and return
    whatever landed on the queue."""
    from prism_service.events import bus

    q = bus.subscribe()
    try:
        await coro_body(q)
        await asyncio.sleep(0)  # let call_soon_threadsafe callbacks land
        out = []
        while not q.empty():
            out.append(q.get_nowait())
        return out
    finally:
        bus.unsubscribe(q)


def test_task_update_publishes_task_changed_on_the_real_bus(tmp_path):
    async def body(q):
        svc = _svc(tmp_path)
        task = svc.create(title="poll fix")
        while not q.empty():
            q.get_nowait()  # drop whatever create-time publishing exists today
        svc.update(task.id, status="in_progress")
        body.task = task

    events = asyncio.run(_drain_after(body))
    task = body.task

    assert events, (
        "TaskService.update() must publish a task.changed event to the "
        "REAL prism_service.events.bus so GET /sse/tasks has something to "
        "deliver — no event landed on the bus")
    evt = events[-1]
    assert evt.get("project") == "prism", f"event must carry project scope; got {evt!r}"
    assert evt.get("type") == "task.changed", f"event must be typed task.changed (D-1); got {evt!r}"
    assert evt.get("task_id") == task.id, f"event must name the task that changed; got {evt!r}"
    fields = evt.get("fields")
    assert isinstance(fields, dict) and fields.get("status") == "in_progress", (
        f"scoped event must carry changed scalars under `fields` (D-1); got {evt!r}")


def test_no_event_published_when_nothing_changed(tmp_path):
    async def body(q):
        svc = _svc(tmp_path)
        task = svc.create(title="poll fix")
        while not q.empty():
            q.get_nowait()
        svc.update(task.id, status=task.status)  # same value — update()'s own no-op guard

    events = asyncio.run(_drain_after(body))
    assert not events, (
        f"a no-op update() (nothing actually changed, per the :596 early "
        f"return) must not publish; got {events!r}")


def test_task_update_still_commits_when_the_bus_raises(tmp_path, monkeypatch):
    from prism_service import events as events_mod

    def _boom(_event):
        raise RuntimeError("bus is down")

    monkeypatch.setattr(events_mod.bus, "publish", _boom)

    svc = _svc(tmp_path)
    task = svc.create(title="poll fix")
    updated = svc.update(task.id, status="in_progress")

    assert updated is not None and updated.status == "in_progress", (
        "a raising bus.publish() must never break the task write (D-2) — "
        f"the write must still commit; got {updated!r}")
    refetched = svc.get(task.id)
    assert refetched is not None and refetched.status == "in_progress", (
        "the status change must be durably committed even though the "
        f"publish raised; got {refetched!r}")


# ---------------------------------------------------------------------------
# Backend: task.changed carries a FRESH `activity` block (D-7)
# ---------------------------------------------------------------------------

class _FakeConductor:
    """Stands in for ConductorService: only the two methods update() calls."""

    def __init__(self, activity_by_status):
        self._activity_by_status = activity_by_status
        self.calls = []

    def phase_progress(self, task_id):
        return {"session_quiet_s": None}

    def activity_for(self, task, phase_progress):
        self.calls.append((task.id, phase_progress))
        return self._activity_by_status[task.status]


def test_task_update_publishes_fresh_activity_when_conductor_attached(tmp_path):
    async def body(q):
        svc = _svc(tmp_path)
        cond = _FakeConductor({
            "pending": {"state": "pending"},
            "in_progress": {"state": "working"},
        })
        svc.attach_conductor_service(cond)
        task = svc.create(title="poll fix")
        while not q.empty():
            q.get_nowait()
        svc.update(task.id, status="in_progress")
        body.task = task
        body.cond = cond

    events = asyncio.run(_drain_after(body))
    task = body.task
    cond = body.cond

    assert events, "expected a task.changed event to be published"
    evt = events[-1]
    assert evt.get("activity") == {"state": "working"}, (
        "a task.changed event must carry a FRESHLY computed `activity` "
        "block (D-7), reusing ConductorService.activity_for at the moment "
        f"of the write — got {evt!r}")
    assert cond.calls and cond.calls[-1][0] == task.id, (
        "activity_for must be called with the just-updated task, not a "
        "stale snapshot")


def test_task_update_publishes_no_activity_key_error_when_no_conductor_attached(tmp_path):
    # A bare TaskService (no attach_conductor_service call — every existing
    # test/script that builds one directly) must keep working exactly as
    # before: no exception, `activity` simply reads None.
    async def body(q):
        svc = _svc(tmp_path)
        task = svc.create(title="poll fix")
        while not q.empty():
            q.get_nowait()
        svc.update(task.id, status="in_progress")
        body.task = task

    events = asyncio.run(_drain_after(body))
    assert events and events[-1].get("activity") is None, (
        f"with no ConductorService attached, activity must be None (never "
        f"raise, never fabricate a value); got {events[-1] if events else None!r}")


def test_activity_for_failure_never_breaks_the_publish(tmp_path):
    class _BoomConductor:
        def phase_progress(self, task_id):
            return {}

        def activity_for(self, task, phase_progress):
            raise RuntimeError("activity computation blew up")

    async def body(q):
        svc = _svc(tmp_path)
        svc.attach_conductor_service(_BoomConductor())
        task = svc.create(title="poll fix")
        while not q.empty():
            q.get_nowait()
        svc.update(task.id, status="in_progress")
        body.task = task

    events = asyncio.run(_drain_after(body))
    task = body.task
    assert events, "a raising activity_for must not swallow the whole publish"
    evt = events[-1]
    assert evt.get("type") == "task.changed" and evt.get("task_id") == task.id, (
        f"the task.changed event must still be published with the real "
        f"fields when activity_for raises; got {evt!r}")
    assert evt.get("activity") is None, (
        f"activity must fall back to None when activity_for raises, never "
        f"propagate the exception into the publish; got {evt!r}")


# ---------------------------------------------------------------------------
# Backend: GET /sse/tasks scopes to project + task_id (D-3)
# ---------------------------------------------------------------------------

class _FakeRequest:
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def test_sse_tasks_route_filters_by_task_id():
    from prism_service.events import bus
    from prism_service.routes import sse as sse_mod

    assert hasattr(sse_mod, "sse_tasks"), (
        "routes/sse.py must expose sse_tasks — a task_id-scoped SSE "
        "endpoint (GET /sse/tasks?project=&task_id=) mirroring "
        "sse_sessions's existing project filter (D-3)")

    async def _run():
        req = _FakeRequest()
        resp = await sse_mod.sse_tasks(req, project="prism", task_id="mine")
        it = resp.body_iterator
        first = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        assert b"connected" in first, f"expected an initial connect comment frame; got {first!r}"

        # A foreign task's event must never be delivered...
        bus.publish({"project": "prism", "type": "task.changed", "task_id": "other", "fields": {}})
        # ...only a matching task_id is.
        bus.publish({"project": "prism", "type": "task.changed", "task_id": "mine", "fields": {"status": "done"}})

        chunk = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        payload = json.loads(chunk.decode("utf-8").split("data: ", 1)[1].strip())
        assert payload.get("task_id") == "mine", (
            "GET /sse/tasks must filter to the requested task_id — a "
            f"foreign task's event must not be the one delivered; got {payload!r}")
        req.disconnected = True

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Frontend source contracts — no JS test runner, ACTUAL TSX/TS source parsed.
# ---------------------------------------------------------------------------

def test_task_detail_page_has_no_setinterval_poll():
    src = _read("pages/TaskDetailPage.tsx")
    assert "setInterval(load, 5000)" not in src, (
        "TaskDetailPage must not re-pull GET /api/tasks/<id> on a 5s "
        "setInterval at idle — replaced by the /sse/tasks push (D-3/D-4)")


def test_sse_handler_patches_state_and_never_refetches():
    # RE-ANCHORED 2026-08-18 (task b835f639): TaskDetailPage no longer holds
    # its own EventSource — lib/sharedStream.ts owns every stream in the SPA so
    # one socket is shared per URL and across tabs (three global streams per
    # tab were exhausting the browser's ~6-per-origin cap and starving new
    # navigations). The D-3/D-4 invariants below are unchanged: this page still
    # follows the task-scoped /sse/tasks stream, and its handler still PATCHES
    # from `fields` rather than refetching.
    src = _read("pages/TaskDetailPage.tsx")
    assert "new EventSource(" not in src, (
        "TaskDetailPage must not construct its own EventSource — it must "
        "subscribe through lib/sharedStream.ts (task b835f639)")
    assert "subscribeStream(" in src and "/sse/tasks" in src, (
        "TaskDetailPage must subscribe against "
        "/sse/tasks?project=&task_id= (D-3) — no task-scoped SSE "
        "subscription found")
    handler_body = _subscribe_callback_body(src)
    assert "load(" not in handler_body and "api.get(" not in handler_body, (
        "the onmessage handler must PATCH local task state from the "
        "pushed event's `fields` (D-4), never call load()/api.get() — "
        "that is the SessionsPage.tsx:80 `es.onmessage = () => load();` "
        "refetch-everything anti-pattern named in this task's own "
        f"likely_misfire; got handler body: {handler_body!r}")
    assert "fields" in handler_body, (
        "the handler must read the pushed event's `fields` payload to "
        f"patch state (D-1/D-4); got handler body: {handler_body!r}")


def test_sse_handler_also_merges_pushed_activity():
    # D-7: activity is computed server-side and never rides `fields` (it is
    # not a DB column) — the handler must read a SEPARATE `activity` key off
    # the same event and merge it into task state the same way `fields` is
    # merged, so a live tab's activity reading is never stuck on the
    # initial-load/poll snapshot.
    src = _read("pages/TaskDetailPage.tsx")
    handler_body = _subscribe_callback_body(src)
    assert "activity" in handler_body, (
        "the onmessage handler must also read the pushed event's "
        "`activity` key (D-7) and merge it into task state — otherwise a "
        "live tab can never see a fresh activity reading over SSE, only on "
        f"a full refetch; got handler body: {handler_body!r}")
    set_task_call = handler_body[handler_body.index("setTask("):]
    assert "activity" in set_task_call, (
        "the handler must actually MERGE `activity` into the setTask() "
        f"updater, not just read it off the payload; got: {set_task_call!r}")


# RETARGETED (task d9f082fe follow-up, owner live, 2026-08-24): LiveBar.tsx
# (the component these two guarded) is deleted outright — the live signal
# moved to Sidebar.tsx's own "/live" nav icon. Sidebar computes `isLive`
# straight from useConductorState(project)'s `managed` array with no
# component-local interval or stream of its own, so both invariants below
# re-anchor there.

def test_sidebar_does_not_poll_conductor_state_unconditionally():
    # Note: Sidebar.tsx has its OWN pre-existing setInterval(tick, 5000) for
    # unrelated staleness polling (useStaleness) — that's fine and not what
    # this guards. The invariant is that Sidebar never fetches
    # /api/conductor/state directly at all; it must reach the live signal
    # ONLY through useConductorState(project), same treatment as
    # lib/version.ts (D-6).
    src = _read("components/Sidebar.tsx")
    assert "/api/conductor/state" not in src, (
        "Sidebar must not fetch GET /api/conductor/state directly — the "
        "live icon reads it only through useConductorState(project)")


def test_sidebar_live_icon_subscribes_to_sse():
    src = _read("components/Sidebar.tsx")
    assert "new EventSource(" not in src, (
        "Sidebar must not open a stream of its own — it would duplicate the "
        "hook's /sse/sessions subscription and cost a connection in EVERY "
        "tab against the browser's ~6-per-origin cap (task b835f639)")
    assert re.search(r"useConductorState\(\s*project\s*\)", src), (
        "Sidebar's live icon must be push-refreshed through "
        "useConductorState(project), whose subscription reaches load() — "
        "that is what keeps D-6 honest, since Sidebar opens no stream itself")


def test_version_poll_is_gated_on_sse_health():
    # RE-ANCHORED 2026-08-18 (task b835f639): version.ts no longer owns an
    # EventSource, so it cannot read es.onerror itself. lib/sharedStream.ts
    # reports stream health back through subscribeStream's onHealth callback.
    # D-6's invariant is untouched — the 15s /api/version poll must still run
    # ONLY while the stream looks unhealthy, never always-on.
    src = _read("lib/version.ts")
    fn_body = _fn_body_from_signature_line(src, "function startLiveWatchdog(")
    assert ("onHealth" in fn_body) or ("es.onerror" in fn_body) or ("readyState" in fn_body), (
        "startLiveWatchdog must gate its fallback poll on stream health "
        "(subscribeStream's onHealth, or es.onerror / es.readyState if it "
        "still owned a stream) per D-6 — otherwise there is no honest "
        f"justification for keeping any poll at all; got: {fn_body!r}")
    marker = ("onHealth" if "onHealth" in fn_body
              else "es.onerror" if "es.onerror" in fn_body else "readyState")
    top_level = fn_body.split(marker, 1)[0]
    assert "setInterval(poll, 15000)" not in top_level, (
        "the /api/version fallback poll must not run unconditionally at "
        "watchdog start — /sse/live already pushes the version on connect "
        "and reconnect; an ALWAYS-ON poll defeats the payload-scope fix "
        f"(D-6). Top-level body before the health gate: {top_level!r}")
