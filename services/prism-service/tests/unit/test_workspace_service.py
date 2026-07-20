"""Red tests for the secure team workspace boundary (task ba8abec4).

The store is deliberately global rather than per-project: project ids are
owned by workspaces, and membership is the authority used before a per-project
service can be selected.
"""

from __future__ import annotations

import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

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


def _identity_row_counts(service) -> dict[str, int]:
    return {
        table: service._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("users", "workspaces", "memberships", "auth_tokens")
    }


def test_bootstrap_owner_rolls_back_the_entire_identity_graph(tmp_path):
    """A late token failure must not leave a user that bricks bootstrap."""
    service = _new_service(tmp_path)
    service._db.executescript(
        """
        CREATE TRIGGER force_bootstrap_token_failure
        BEFORE INSERT ON auth_tokens
        BEGIN
            SELECT RAISE(ABORT, 'forced token failure');
        END;
        """
    )

    with pytest.raises(sqlite3.IntegrityError, match="forced token failure"):
        service.bootstrap_owner(
            email="owner@example.test",
            display_name="Owner",
            workspace_name="Platform",
            user_id="bootstrap-user",
            workspace_id="bootstrap-workspace",
            token_id="bootstrap-token",
            token_hash="bootstrap-token-hash",
            token_label="bootstrap",
        )

    assert _identity_row_counts(service) == {
        "users": 0,
        "workspaces": 0,
        "memberships": 0,
        "auth_tokens": 0,
    }


def test_concurrent_bootstrap_creates_exactly_one_complete_owner(tmp_path):
    from prism_service.services.workspace_service import BootstrapAlreadyCompleted

    service = _new_service(tmp_path)

    def attempt(index: int) -> str:
        try:
            service.bootstrap_owner(
                email=f"owner-{index}@example.test",
                display_name=f"Owner {index}",
                workspace_name=f"Workspace {index}",
                user_id=f"user-{index}",
                workspace_id=f"workspace-{index}",
                token_id=f"token-{index}",
                token_hash=f"hash-{index}",
                token_label="bootstrap",
            )
            return "created"
        except BootstrapAlreadyCompleted:
            return "already-created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, range(2)))

    assert sorted(outcomes) == ["already-created", "created"]
    assert _identity_row_counts(service) == {
        "users": 1,
        "workspaces": 1,
        "memberships": 1,
        "auth_tokens": 1,
    }


def test_project_reservation_is_canonical_and_has_one_cross_workspace_winner(tmp_path):
    from prism_service.services.workspace_service import ProjectOwnershipConflict

    service = _new_service(tmp_path)
    owner = service.create_user("owner@example.test", user_id="owner")
    first = service.create_workspace("First", owner.id, workspace_id="first")
    second = service.create_workspace("Second", owner.id, workspace_id="second")

    for hostile in (
        ".",
        "..",
        "../victim",
        "folder/victim",
        r"folder\victim",
        r"C:\victim",
        "project.",
        "Project-A",
    ):
        with pytest.raises(ValueError):
            service.reserve_project(hostile, first.id)

    def reserve(workspace_id: str) -> tuple[str, bool]:
        try:
            ownership, created = service.reserve_project("shared-project", workspace_id)
            return ownership.workspace_id, created
        except ProjectOwnershipConflict:
            return "conflict", False

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(reserve, (first.id, second.id)))

    winners = [workspace_id for workspace_id, created in outcomes if created]
    assert len(winners) == 1
    assert outcomes.count(("conflict", False)) == 1
    assert service.project_workspace("shared-project").id == winners[0]

