"""Red tests for the secure team workspace boundary (task ba8abec4).

The store is deliberately global rather than per-project: project ids are
owned by workspaces, and membership is the authority used before a per-project
service can be selected.
"""

from __future__ import annotations

import hashlib

import pytest


def _new_service(tmp_path):
    from prism_service.services.workspace_service import WorkspaceService

    return WorkspaceService(tmp_path / "workspace.db")


def test_workspace_membership_and_project_ownership_round_trip(tmp_path):
    from prism_service.services.workspace_service import AuthorizationDenied

    service = _new_service(tmp_path)
    alice = service.create_user(
        "alice@example.test", display_name="Alice", user_id="user-alice"
    )
    bob = service.create_user(
        "bob@example.test", display_name="Bob", user_id="user-bob"
    )
    workspace = service.create_workspace(
        "Platform", owner_user_id=alice.id, workspace_id="workspace-a"
    )
    service.add_membership(workspace.id, bob.id, "viewer")
    ownership = service.bind_project("project-a", workspace.id)

    assert ownership.project_id == "project-a"
    assert ownership.workspace_id == workspace.id
    assert service.membership_for(workspace.id, alice.id).role == "owner"
    assert service.membership_for(workspace.id, bob.id).role == "viewer"
    assert service.require_project_role(bob.id, "project-a", "viewer").role == "viewer"
    with pytest.raises(AuthorizationDenied):
        service.require_project_role(bob.id, "project-a", "member")

    reopened = type(service)(tmp_path / "workspace.db")
    assert reopened.get_user(alice.id).email == "alice@example.test"
    assert reopened.project_workspace("project-a").id == workspace.id
    assert reopened.list_projects_for_user(bob.id) == ["project-a"]


@pytest.mark.parametrize(
    ("role", "minimum", "allowed"),
    [
        ("viewer", "viewer", True),
        ("viewer", "member", False),
        ("member", "member", True),
        ("member", "admin", False),
        ("admin", "member", True),
        ("admin", "admin", True),
        ("owner", "admin", True),
        ("owner", "owner", True),
    ],
)
def test_role_hierarchy_is_explicit(role, minimum, allowed):
    from prism_service.models.workspace import role_allows

    assert role_allows(role, minimum) is allowed


def test_team_tokens_are_hashed_at_rest_and_resolve_a_stable_principal(tmp_path):
    from prism_service.services.auth_service import AuthService
    from prism_service.services import sqlite_db

    service = _new_service(tmp_path)
    user = service.create_user(
        "owner@example.test", display_name="Owner", user_id="stable-user-id"
    )
    auth = AuthService(service, mode="team")

    issued = auth.issue_token(user.id, label="test client")
    principal = auth.resolve_principal(f"Bearer {issued.secret}")

    assert principal.user_id == "stable-user-id"
    assert principal.email == "owner@example.test"
    assert principal.mode == "team"
    with sqlite_db.connect(tmp_path / "workspace.db") as conn:
        row = conn.execute(
            "SELECT token_hash FROM auth_tokens WHERE id = ?", (issued.id,)
        ).fetchone()
        dump = "\n".join(conn.iterdump())
    assert row["token_hash"] == hashlib.sha256(issued.secret.encode()).hexdigest()
    assert issued.secret not in dump


def test_local_mode_is_explicit_and_team_mode_fails_closed(tmp_path, monkeypatch):
    from prism_service.services.auth_service import (
        AuthenticationRequired,
        AuthService,
    )

    service = _new_service(tmp_path)
    local = AuthService(service, mode="local").resolve_principal(None)
    assert local.mode == "local"
    assert local.user_id == "local-user"
    assert local.role == "owner"

    with pytest.raises(AuthenticationRequired):
        AuthService(service, mode="team").resolve_principal(None)
    with pytest.raises(AuthenticationRequired):
        AuthService(service, mode="team").resolve_principal("Bearer not-a-token")

    monkeypatch.setenv("PRISM_AUTH_MODE", "team")
    with pytest.raises(AuthenticationRequired):
        AuthService(service).resolve_principal(None)


def test_project_binding_cannot_be_silently_moved_between_workspaces(tmp_path):
    from prism_service.services.workspace_service import ProjectOwnershipConflict

    service = _new_service(tmp_path)
    owner = service.create_user("owner@example.test", user_id="owner")
    first = service.create_workspace("First", owner.id, workspace_id="first")
    second = service.create_workspace("Second", owner.id, workspace_id="second")
    service.bind_project("shared-name", first.id)

    with pytest.raises(ProjectOwnershipConflict):
        service.bind_project("shared-name", second.id)

