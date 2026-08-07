"""AC-6 of epic 0784729f: "everyone sees their own work".

The Inbox (tests/unit/test_inbox_surface.py) was built identical for every
caller. This pins the actual claim: the SAME endpoint, hit by TWO REAL
people through a REAL WorkspaceService + REAL TaskService (tmp_path, no
mocks, no injected collaborators), returns DIFFERENT results — because a
gate you produced is not yours to decide, and a workspace you don't belong
to shows you nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest


@dataclass
class TeamFixture:
    client: object
    task_svc: object
    alice_token: str
    bob_token: str
    mallory_token: str
    alice_id: str
    bob_id: str


@pytest.fixture
def team(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service import project_context
    from prism_service.api import inbox as inbox_api
    from prism_service.services.auth_service import AuthService
    from prism_service.services.workspace_service import (
        WorkspaceService,
        set_workspace_service,
    )

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    project_context._contexts.clear()

    ws = WorkspaceService(tmp_path / "workspace.db")
    set_workspace_service(ws)

    alice = ws.create_user("alice@example.test", display_name="Alice",
                            user_id="user-alice")
    bob = ws.create_user("bob@example.test", display_name="Bob",
                          user_id="user-bob")
    mallory = ws.create_user("mallory@example.test", display_name="Mallory",
                              user_id="user-mallory")
    workspace = ws.create_workspace("Shared workspace", alice.id,
                                     workspace_id="workspace-shared")
    ws.add_membership(workspace.id, bob.id, "member")
    ws.bind_project("default", workspace.id)

    auth = AuthService(ws, mode="team")
    alice_token = auth.issue_token(alice.id, "alice").secret
    bob_token = auth.issue_token(bob.id, "bob").secret
    mallory_token = auth.issue_token(mallory.id, "mallory").secret

    app = FastAPI()
    app.include_router(inbox_api.router, prefix="/api/inbox")

    task_svc = project_context.get_project("default").task_svc

    with TestClient(app) as client:
        yield TeamFixture(
            client=client, task_svc=task_svc,
            alice_token=alice_token, bob_token=bob_token,
            mallory_token=mallory_token,
            alice_id=alice.id, bob_id=bob.id,
        )

    set_workspace_service(None)
    project_context._contexts.clear()


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _inbox(team: TeamFixture, token: str) -> dict:
    r = team.client.get("/api/inbox?project=default", headers=_bearer(token))
    assert r.status_code == 200, r.text
    return r.json()


def test_the_same_endpoint_returns_different_needs_you_for_different_members(team):
    """The headline claim of AC-6: identical URL, identical task, two real
    accounts, two different needs_you. Alice produced the work parked at
    this gate (her user id is the most recent task_history actor) — she
    cannot sign her own gate, so it must stay off HER inbox. Bob is a
    distinct member of the same workspace and must still see it."""
    task = team.task_svc.create(title="a task alice drove to green_gate")
    team.task_svc.update(task.id, workflow_step="green_gate",
                          gate_state="pending", status="in_progress")
    team.task_svc.record_history(task.id, "advance_task", actor=team.alice_id)

    alice_view = _inbox(team, team.alice_token)
    bob_view = _inbox(team, team.bob_token)

    alice_ids = {i["task_id"] for i in alice_view["needs_you"]}
    bob_ids = {i["task_id"] for i in bob_view["needs_you"]}
    assert task.id not in alice_ids, "Alice must not see a gate she produced"
    assert task.id in bob_ids, "Bob is a distinct member and must see it"
    assert alice_view["viewer"]["display_name"] == "Alice"
    assert bob_view["viewer"]["display_name"] == "Bob"


def test_a_non_member_gets_an_empty_inbox_and_no_titles_leak(team):
    """Mallory belongs to no workspace bound to 'default'. The query-string
    project is a selector, never an ACL: she gets a clean empty inbox, not
    a 403 and not a single leaked task title."""
    task = team.task_svc.create(title="a title mallory must never see")
    team.task_svc.update(task.id, workflow_step="green_gate",
                          gate_state="pending", status="in_progress")

    body = _inbox(team, team.mallory_token)
    assert body["needs_you"] == []
    assert body["activity"] == []
    raw = team.client.get("/api/inbox?project=default",
                           headers=_bearer(team.mallory_token)).text
    assert "a title mallory must never see" not in raw


def test_a_subtask_gate_never_reaches_a_members_needs_you(team):
    """The existing invariant (test_inbox_surface.py) must survive the move
    to real per-member scoping: only ROOT tasks reach needs_you."""
    epic = team.task_svc.create(title="an epic bob watches")
    child = team.task_svc.create(title="a slice bob should never see",
                                  parent_id=epic.id)
    for t in (epic, child):
        team.task_svc.update(t.id, workflow_step="green_gate",
                              gate_state="pending", status="in_progress")

    body = _inbox(team, team.bob_token)
    ids = {i["task_id"] for i in body["needs_you"]}
    assert epic.id in ids
    assert child.id not in ids


def test_reading_the_inbox_never_mutates_task_state(team):
    """BEHAVIOURAL, real service: comparing full task rows before/after,
    the same invariant test_inbox_surface.py pins for the unscoped path
    must still hold once membership and distinct-actor filtering run."""
    task = team.task_svc.create(title="a task parked at a gate")
    team.task_svc.update(task.id, workflow_step="green_gate",
                          gate_state="pending", status="in_progress")
    team.task_svc.record_history(task.id, "advance_task", actor=team.alice_id)

    before = [(t.id, t.status, t.workflow_step, t.gate_state)
              for t in team.task_svc.list()]
    for token in (team.alice_token, team.bob_token, team.mallory_token):
        _inbox(team, token)
    after = [(t.id, t.status, t.workflow_step, t.gate_state)
             for t in team.task_svc.list()]
    assert after == before, "reading the inbox changed task state"
