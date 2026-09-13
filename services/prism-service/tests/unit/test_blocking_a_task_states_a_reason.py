"""A task may not enter `blocked` without saying why.

MEASURED ON THE LIVE BOARD, 2026-09-13: 19 of 22 live tasks were blocked,
and **6 of those 19 carried an EMPTY blocked_reason** -- b9772333, 8848089d,
d7947eb6, dc815149, 6f18c224, d0b392b3. A blocked row with no reason is a
dead end: no driver, machine or human, can self-diagnose it, and the board
shows a red task that states nothing to act on.

WHERE THEY CAME FROM. Every worker path that blocks a task already passes a
reason (dispatch_guard.py:359, resume_actuator.py:230 and :480,
ship_worker.py:1036, task_runner.py:1830). The hole is the generic status
PATCH: `api/tasks.py`'s update_task had transition guards for `in_progress`
(the session gate) and for `done` (the open-gate close guard) but none for
`blocked`, and the SPA's own button sends `{status}` alone
(TaskDetailPage.tsx setStatus, line ~1545). So one click produced exactly
these reasonless rows.

This is the same defect class the project already names elsewhere: a tooth
that refuses without recording WHY has only half-shipped, and "a parked gate
that states no real refusal is its own defect".

The guard is deliberately a REFUSAL, not an auto-filled placeholder. An
invented reason ("blocked by the UI") would satisfy the letter and destroy
the point -- the caller must say something a later reader can act on.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    from prism_service.main import app
    return TestClient(app)


def _project() -> str:
    return "blockreason-" + uuid.uuid4().hex[:8]


def _make_task(client, project) -> str:
    r = client.post(f"/api/tasks?project={project}",
                    json={"title": "a task that will be blocked"})
    assert r.status_code in (200, 201), r.text
    return r.json()["task"]["id"]


def test_blocking_without_a_reason_is_refused(client):
    """The whole point: one click used to mint an undiagnosable dead end."""
    project = _project()
    tid = _make_task(client, project)

    r = client.patch(f"/api/tasks/{tid}?project={project}",
                     json={"status": "blocked"})

    assert r.status_code == 422, (
        f"blocking with no reason must be refused, got {r.status_code}: "
        f"{r.text[:300]}")
    detail = str(r.json().get("detail", "")).lower()
    assert "reason" in detail, (
        f"the refusal must name what is missing, got: {detail}")

    got = client.get(f"/api/tasks/{tid}?project={project}").json()["task"]
    assert got["status"] != "blocked", (
        "a refused transition must not have been written")


def test_blocking_with_a_reason_is_accepted(client):
    """The guard must not make blocking impossible -- only wordless."""
    project = _project()
    tid = _make_task(client, project)

    r = client.patch(
        f"/api/tasks/{tid}?project={project}",
        json={"status": "blocked",
              "blocked_reason": "waiting on the upstream lexicon decision"})

    assert r.status_code == 200, r.text
    got = client.get(f"/api/tasks/{tid}?project={project}").json()["task"]
    assert got["status"] == "blocked", got["status"]
    assert "lexicon" in got["blocked_reason"], got["blocked_reason"]


def test_a_task_already_blocked_can_be_patched_without_resending_a_reason(
        client):
    """Grandfathering: the guard is on the TRANSITION, not on every write.

    Editing some other field of an already-blocked task must not demand the
    reason be retyped -- the same posture the in_progress session gate takes
    for rows already in that status.
    """
    project = _project()
    tid = _make_task(client, project)
    client.patch(f"/api/tasks/{tid}?project={project}",
                 json={"status": "blocked",
                       "blocked_reason": "upstream decision pending"})

    r = client.patch(f"/api/tasks/{tid}?project={project}",
                     json={"priority": 70})

    assert r.status_code == 200, r.text
    got = client.get(f"/api/tasks/{tid}?project={project}").json()["task"]
    assert got["status"] == "blocked" and got["priority"] == 70, got


def _setstatus_body() -> str:
    """The BODY of TaskDetailPage's setStatus, with comments stripped.

    The SPA has no JS test runner, so UI contracts are pinned by asserting
    the real TSX source. Comments are removed first because this change's
    OWN explanatory comment names `blocked_reason` and would otherwise
    satisfy every assertion below without a line of code backing it -- the
    trap recorded in CLAUDE.md's lessons.
    """
    import re
    from pathlib import Path
    src = (Path(__file__).parent.parent.parent / "prism_service" / "web"
           / "src" / "pages" / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    src = re.sub(r"//[^\n]*", "", src)
    start = src.index("const setStatus = async")
    end = src.index("const releasePark", start)
    return src[start:end]


def test_the_block_button_asks_for_a_reason_before_sending():
    body = _setstatus_body()
    assert "window.prompt" in body, (
        "the block button must ask for a reason rather than send a wordless "
        "block the server will refuse")
    assert "blocked_reason" in body, (
        "the answer must be sent as blocked_reason")


def test_a_cancelled_or_empty_answer_sends_nothing():
    """Cancel must abort, not send a block with an empty reason."""
    body = _setstatus_body()
    assert "=== null" in body or "== null" in body, (
        f"a cancelled prompt must return early; body was: {body[:400]}")
    assert "return" in body.split("window.prompt")[1][:400], (
        "the cancel branch must return before the fetch")


def test_leaving_blocked_still_needs_no_reason(client):
    """Unblocking is not a block -- the guard must not fire on the way out."""
    project = _project()
    tid = _make_task(client, project)
    client.patch(f"/api/tasks/{tid}?project={project}",
                 json={"status": "blocked", "blocked_reason": "upstream dep"})

    r = client.patch(f"/api/tasks/{tid}?project={project}",
                     json={"status": "pending"})

    assert r.status_code == 200, r.text
    got = client.get(f"/api/tasks/{tid}?project={project}").json()["task"]
    assert got["status"] == "pending", got["status"]
