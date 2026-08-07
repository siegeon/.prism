"""The team work hub's identity + collaboration spine (epic 0784729f).

RED FIRST, deliberately. Two things this epic cannot be built on top of
exist nowhere yet:

1. IDENTITY. `Task.assigned_agent` (models/task.py:20) and
   `TaskHistory.actor` (models/task.py:108) are free strings that never
   join to `User` (models/workspace.py:40), so the distinct-actor rule
   compares TEXT (services/conductor_service.py:531-536). There is no
   Actor type at all.
2. COLLABORATION. Nothing in the service reaches a person who is not at
   this machine. There is no port, no adapter, and nothing registered.

The load-bearing assertion in here is REGISTRATION BY PRODUCTION CODE.
This epic already shipped seven green children whose feature could not
run because every test injected its collaborator and nothing ever
constructed one (task f4dd3687, the reason
api/integrations.py:63 register_builtin_adapters exists). So the
collaboration test below reads the registry after a CLEAN import in a
subprocess, which an injecting test cannot fake.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ----------------------------------------------------------------------
# Identity: an Actor is a real thing, not a string
# ----------------------------------------------------------------------

def test_actor_covers_human_agent_machine_and_unknown():
    """AC-2. One type unifies the three kinds PRISM actually has, plus the
    placeholder every legacy string must land on."""
    from prism_service.models.actor import Actor, ActorKind

    human = Actor(id="user:u1", kind=ActorKind.HUMAN,
                  display_name="Ada", user_id="u1")
    agent = Actor(id="agent:drive-1", kind=ActorKind.AGENT,
                  display_name="drive-1", user_id="u1")
    machine = Actor(id="machine:conductor-adjudicator",
                    kind=ActorKind.MACHINE,
                    display_name="conductor-adjudicator")
    unknown = Actor(id="unknown:whatever", kind=ActorKind.UNKNOWN,
                    display_name="whatever")

    assert [a.kind for a in (human, agent, machine, unknown)] == [
        "human", "agent", "machine", "unknown"]
    # An agent belongs to a person; that ownership is what makes it
    # revocable independently of the human.
    assert agent.user_id == "u1"
    assert machine.user_id == ""


def test_the_models_layer_stays_dependency_free():
    """ARC-PRISM-1: models must not depend on services. Parsed with ast, so
    a mention inside a docstring or comment cannot satisfy it."""
    src = (_SERVICE_ROOT / "prism_service" / "models" / "actor.py").read_text(
        encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
        elif isinstance(node, ast.Import):
            imported.extend(a.name for a in node.names)
    offenders = [m for m in imported if "prism_service.services" in m
                 or "prism_service.api" in m]
    assert offenders == [], f"models/actor.py reaches into {offenders}"


# ----------------------------------------------------------------------
# Resolution: every string that exists today must land somewhere
# ----------------------------------------------------------------------

@pytest.fixture
def actors(tmp_path):
    """An ActorService backed by a REAL user store, not a stub."""
    from prism_service.services.actor_service import ActorService
    from prism_service.services.workspace_service import WorkspaceService

    ws = WorkspaceService(tmp_path / "workspace.db")
    return ActorService(workspaces=ws), ws


def test_the_machine_seat_resolves_to_a_machine_actor(actors):
    """AC-2. The conductor's own seat is a bare string constant today. It
    must keep resolving, and it must resolve as a MACHINE - a seat is not a
    person. Imported from the constant rather than retyped, so a rename
    cannot silently desync the resolver from the conductor."""
    from prism_service.services.conductor_service import ADJUDICATOR_SEAT

    svc, _ = actors
    for seat in (ADJUDICATOR_SEAT, "conductor-autoclear", "gate-card-rerun"):
        actor = svc.resolve(seat)
        assert actor.kind == "machine", f"{seat} resolved as {actor.kind}"
        assert actor.display_name == seat


def test_a_real_user_resolves_to_a_human_actor(actors):
    """AC-2. Resolution joins to the EXISTING user store - by id and by
    email - rather than inventing a second table."""
    svc, ws = actors
    user = ws.create_user(email="ada@example.com", display_name="Ada L")

    by_id = svc.resolve(user.id)
    assert by_id.kind == "human"
    assert by_id.user_id == user.id
    # The name comes off the ROW, not off the raw string handed in.
    assert by_id.display_name == "Ada L"

    by_email = svc.resolve("ada@example.com")
    assert by_email.kind == "human"
    assert by_email.user_id == user.id


@pytest.mark.parametrize("raw", ["some-legacy-session-abc123", "", None])
def test_resolution_is_total_and_never_raises(actors, raw):
    """AC-2. Old history rows carry strings nobody can account for. A
    resolver that raised on those would turn an audit trail into a 500 on
    page load, so unknown input lands on a placeholder that PRESERVES the
    original text."""
    svc, _ = actors
    actor = svc.resolve(raw)
    assert actor.kind == "unknown"
    if raw:
        assert raw in actor.display_name


# ----------------------------------------------------------------------
# Collaboration: something production CONSTRUCTS reaches off this machine
# ----------------------------------------------------------------------

_CLEAN_IMPORT = (
    "import json;"
    "from prism_service.api import collaboration as c;"
    "print(json.dumps(sorted(c.collaboration_surfaces())))"
)


def test_a_collaboration_surface_is_registered_by_production_code():
    """AC-1. THE failure this epic already shipped once: seven green
    children whose feature could not run because every test injected its
    collaborator and nothing in production ever constructed one (task
    f4dd3687, the reason api/integrations.py:63 exists).

    So this reads the registry out of a CLEAN interpreter - no fixtures, no
    injection, nothing imported but the module itself. A test that registers
    an adapter for itself cannot satisfy this, which is the point.
    """
    proc = subprocess.run([sys.executable, "-c", _CLEAN_IMPORT],
                          cwd=str(_SERVICE_ROOT), capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 0, (
        f"clean import failed:\n{proc.stdout}\n{proc.stderr}")
    surfaces = json.loads(proc.stdout.strip().splitlines()[-1])
    assert surfaces, (
        "no collaboration surface is registered at import time - the "
        "adapter exists only if a test injects it, which is exactly the "
        "0784729f failure")


def test_a_gate_decision_carries_a_resolved_identity(tmp_path, monkeypatch):
    """AC-2 reaching the surface a person reads. The task detail response
    must expose WHO each history row was, resolved, alongside the raw
    string it already carries - additive, so nothing that reads the
    existing shape breaks."""
    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    svc = get_project("default").task_svc
    task = svc.create(title="a task with a decision on it")
    svc.record_history(task.id, actor="conductor-adjudicator",
                       action="gate_decide", details="approved")

    api = FastAPI()
    api.include_router(tasks_api.router, prefix="/api/tasks")
    with TestClient(api) as client:
        body = client.get(f"/api/tasks/{task.id}?project=default").json()

    rows = body["history"]
    assert rows, "no history recorded"
    row = rows[-1]
    # The raw string stays exactly where it was.
    assert row["actor"] == "conductor-adjudicator"
    # ...and now resolves to a real identity.
    assert row["actor_identity"]["kind"] == "machine"
