"""Task detail page stops hiding behind a poll loop (task 2d480b08).

Today the open /tasks/:id tab is a polling machine:
  - TaskDetailPage.tsx:959 `setInterval(load, 5000)` re-pulls GET
    /api/tasks/<id> (+ the child-task GET riding the same load()) forever,
    whether or not anything changed.
  - LiveBar.tsx:102 `setInterval(tick, 5000)` re-pulls GET
    /api/conductor/state (~29KB with ONE managed task, per task c38ef597's
    own docstring) on every tab that mounts the app shell, /tasks/:id
    included.
  - lib/version.ts:59 `setInterval(poll, 15000)` re-pulls GET /api/version
    unconditionally, even though /sse/live already pushes the same fact.

FAILS TODAY because:
  - none of the three intervals above have been replaced by a push
    subscription: TaskDetailPage.tsx has no EventSource at all, LiveBar.tsx
    has no EventSource at all, and version.ts's fallback poll fires on a
    bare interval rather than only as an SSE-failure fallback.
  - prism_service/services/task_service.py's TaskService.update() has no
    call into prism_service.events.bus — no task-lifecycle event exists to
    subscribe to (the task's own likely_misfire: events.py is published to
    only for session_outcome/skill_usage today).

The SPA ships no JS test runner, so the TSX/TS-facing acceptance criteria
are pinned by asserting the ACTUAL source, brace-balanced from each
construct's own start (never a fixed character window or a comment),
matching the convention in test_heavy_polls_are_scoped.py and
test_conductor_page_animated_cleanup_ui.py.

Scoped push, not the SessionsPage.tsx:80 anti-pattern: that page's
`es.onmessage = () => load();` refetches its FULL list on literally any
bus event, project-matched or not otherwise filtered. The fix here filters
BEFORE refetching (TaskDetailPage checks the event names ITS OWN task id;
LiveBar checks the event is actually a task lifecycle event) and the
published event itself stays lean — board-live fields only, never the
heavy long-text columns (description/plan_doc/completion_proof/
premise_notes) — so a keystroke-sized field edit never blasts a big
string to every open tab.
"""

from __future__ import annotations

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


def _fn_body_from_signature_line(src: str, marker: str) -> str:
    i = src.index(marker)
    eol = src.index("\n", i)
    return _walk_braces(src, src.rindex("{", i, eol))


# ---------------------------------------------------------------------------
# Backend: TaskService.update() must publish a LEAN task-lifecycle event
# ---------------------------------------------------------------------------

def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"), project="prism")


def test_task_update_publishes_scoped_task_event(tmp_path, monkeypatch):
    from prism_service import events as events_mod

    captured = []
    monkeypatch.setattr(events_mod.bus, "publish", lambda e: captured.append(e))

    svc = _svc(tmp_path)
    task = svc.create(title="poll fix", description="d1")
    captured.clear()  # ignore whatever create-time publishing exists today

    long_description = "a much longer description body " * 20
    svc.update(task.id, status="in_progress", description=long_description)

    assert captured, (
        "TaskService.update() must publish a task-lifecycle event to "
        "prism_service.events.bus so /sse/sessions has something for the "
        "open task page to subscribe to — no event fired")
    evt = captured[-1]
    assert evt.get("project") == "prism", f"event must carry project scope; got {evt!r}"
    assert evt.get("type") == "task_updated", f"event must be typed task_updated; got {evt!r}"
    assert evt.get("task_id") == task.id, f"event must name the task that changed; got {evt!r}"
    assert evt.get("status") == "in_progress", (
        f"scoped event must carry live-board fields (status); got {evt!r}")
    assert "description" not in evt, (
        "the task_updated event must stay LEAN (board-live fields only) — "
        "shipping the full changed description over the bus defeats the "
        f"payload-scope fix and blasts a big string to every open tab; got {evt!r}")


def test_task_update_with_no_real_change_does_not_publish(tmp_path, monkeypatch):
    from prism_service import events as events_mod

    captured = []
    monkeypatch.setattr(events_mod.bus, "publish", lambda e: captured.append(e))

    svc = _svc(tmp_path)
    task = svc.create(title="poll fix", description="d1")
    captured.clear()

    # Re-sending the SAME status is a no-op per update()'s own
    # `old_value == value` skip — must not fire a phantom event.
    svc.update(task.id, status=task.status)

    assert not captured, (
        f"a no-op update() (no field actually changed) must not publish; got {captured!r}")


# ---------------------------------------------------------------------------
# Frontend source contracts — no JS test runner, ACTUAL TSX/TS source parsed.
# ---------------------------------------------------------------------------

def test_task_detail_page_no_longer_polls_on_interval():
    src = _read("pages/TaskDetailPage.tsx")
    assert "setInterval(load, 5000)" not in src, (
        "TaskDetailPage must not re-pull GET /api/tasks/<id> on a 5s "
        "setInterval at idle — replace with a push subscription")


def test_task_detail_page_subscribes_to_scoped_task_events():
    src = _read("pages/TaskDetailPage.tsx")
    assert "new EventSource(" in src and "/sse/sessions" in src, (
        "TaskDetailPage must open an EventSource against /sse/sessions to "
        "receive task-lifecycle push updates")
    assert "es.onmessage = () => load();" not in src, (
        "the onmessage handler must not be the SessionsPage.tsx:80 "
        "refetch-everything-on-every-event anti-pattern (named in this "
        "task's own likely_misfire) — it must filter to THIS task's id "
        "before refetching anything")
    assert "task_id" in src, (
        "the onmessage handler must check the pushed event's task_id "
        "against THIS page's id before refetching — got no task_id "
        "reference in TaskDetailPage.tsx at all")


def test_livebar_no_longer_polls_conductor_state_on_interval():
    src = _read("components/LiveBar.tsx")
    assert "setInterval(tick, 5000)" not in src, (
        "LiveBar must not re-pull GET /api/conductor/state on a 5s "
        "setInterval at idle (it ran on every activity-context tab, "
        "including /tasks/:id) — replace with a push subscription")


def test_livebar_subscribes_to_scoped_task_events():
    src = _read("components/LiveBar.tsx")
    assert "new EventSource(" in src and "/sse/sessions" in src, (
        "LiveBar must open an EventSource against /sse/sessions to receive "
        "task-lifecycle push updates instead of polling the whole board")
    assert "es.onmessage = () => load();" not in src, (
        "LiveBar's onmessage handler must not be the "
        "refetch-everything-on-every-event anti-pattern — it must check "
        "the event actually names a task-lifecycle change before "
        "re-fetching /api/conductor/state")
    assert "task_updated" in src, (
        "LiveBar's onmessage handler must filter on the task_updated "
        "event type before refetching")


def test_version_watchdog_poll_is_sse_failure_fallback_only():
    src = _read("lib/version.ts")
    fn_body = _fn_body_from_signature_line(src, "function startLiveWatchdog(")
    assert "es.onerror" in fn_body, (
        "startLiveWatchdog must react to SSE failure (es.onerror) — "
        "otherwise there is no honest justification left for keeping any "
        f"fallback poll at all; got body: {fn_body!r}")
    onerror_body = _brace_body(fn_body, "es.onerror")
    top_level = fn_body.split("es.onerror", 1)[0]
    assert "setInterval(poll, 15000)" not in top_level, (
        "the /api/version fallback poll must not run unconditionally at "
        "module-watchdog-start — /sse/live already pushes the version on "
        "connect and on reconnect; a poll that ALSO always runs defeats "
        f"the payload-scope fix. Top-level body: {top_level!r}")
    assert "setInterval(poll" in onerror_body or "poll(" in onerror_body, (
        "the fallback poll must be reachable from the es.onerror handler "
        f"(SSE-failure path only); got onerror body: {onerror_body!r}")
