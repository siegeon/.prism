"""Authoring-time oracle validation — "the oracle like a compiler"
(task b78a193c).

OracleSpec.from_task() (oracle_spec.py:139) already derives a structured
spec — but only lazily, at gate time. This pins the move to AUTHORING
time: task_create/task_update (MCP) and POST /api/tasks (REST) reject the
one hard, unrepairable-later contradiction — proof_type=test with a
non-empty oracle but zero runnable pytest ids in verify[] — instead of
letting it surface deep in the SDLC at green_gate. Oracle-less tasks and
every manual-evidence proof_type keep today's honest fallback.

Covers AC-1..AC-4 of task b78a193c's story.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PYTEST_ID = "services/prism-service/tests/unit/test_x.py::test_y"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so the MCP dispatcher and the API router
    resolve the SAME tmp-backed task_svc (mirrors test_task_misfire.py)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-oracle-authoring"
    pc._contexts.clear()


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def _api_client(project_id):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app), project_id


# ---- AC-1: MCP task_create rejects the hard contradiction --------------

def test_mcp_task_create_rejects_test_proof_type_without_pytest_ids(project):
    from prism_service.project_context import get_project
    before = len(get_project(project).task_svc.list())

    result = json.loads(_call(
        "task_create",
        {
            "title": "oracle without a runnable test",
            "oracle": "the new pytest passes",
            "proof_type": "test",
            "verify": [],
        },
        project,
    ))
    assert result.get("error") == "oracle_validation_failed", result
    domain_errors = " ".join(result.get("domain_errors", []))
    assert "pytest" in domain_errors.lower()
    assert "verify" in domain_errors.lower()

    after = get_project(project).task_svc.list()
    assert len(after) == before, "hard-contradiction create must not insert a row"


# ---- AC-2: MCP task_create accepts a valid test oracle + spec summary --

def test_mcp_task_create_accepts_valid_test_oracle_and_returns_spec_summary(project):
    result = json.loads(_call(
        "task_create",
        {
            "title": "oracle with a runnable test",
            "oracle": "the new pytest passes",
            "proof_type": "test",
            "verify": [_PYTEST_ID],
        },
        project,
    ))
    assert "error" not in result, result
    assert result["id"]
    spec = result.get("oracle_spec")
    assert spec is not None, result
    assert spec["adapter"] == "pytest_ids"
    assert spec["spec_hash"].startswith("sha256:")


# ---- AC-3: REST POST /api/tasks parity (reject + accept) ---------------

def test_rest_create_task_parity_reject_and_accept(project):
    client, pid = _api_client(project)

    rejected = client.post(
        "/api/tasks", params={"project": pid},
        json={
            "title": "rest oracle without a runnable test",
            "oracle": "the new pytest passes",
            "proof_type": "test",
            "verify": [],
        },
    )
    assert rejected.status_code == 422, rejected.text
    body = rejected.json().get("detail", "")
    assert "pytest" in body.lower()
    assert "verify" in body.lower()

    accepted = client.post(
        "/api/tasks", params={"project": pid},
        json={
            "title": "rest oracle with a runnable test",
            "oracle": "the new pytest passes",
            "proof_type": "test",
            "verify": [_PYTEST_ID],
        },
    )
    assert accepted.status_code in (200, 201), accepted.text
    payload = accepted.json()
    assert payload["task"]["id"]
    assert payload["oracle_spec"]["adapter"] == "pytest_ids"


# ---- AC-4: oracle-less + demo tasks keep the honest fallback -----------

def test_oracle_less_and_demo_tasks_still_create_fine(project):
    client, pid = _api_client(project)

    mcp_no_oracle = json.loads(_call(
        "task_create", {"title": "no oracle at all via mcp"}, project))
    assert "error" not in mcp_no_oracle, mcp_no_oracle
    assert mcp_no_oracle.get("oracle_spec") is None

    mcp_demo = json.loads(_call(
        "task_create",
        {
            "title": "demo proof via mcp",
            "oracle": "the screenshot shows the new card",
            "proof_type": "demo",
        },
        project,
    ))
    assert "error" not in mcp_demo, mcp_demo

    rest_no_oracle = client.post(
        "/api/tasks", params={"project": pid},
        json={"title": "no oracle at all via rest"})
    assert rest_no_oracle.status_code in (200, 201), rest_no_oracle.text

    rest_demo = client.post(
        "/api/tasks", params={"project": pid},
        json={
            "title": "demo proof via rest",
            "oracle": "the screenshot shows the new card",
            "proof_type": "demo",
        },
    )
    assert rest_demo.status_code in (200, 201), rest_demo.text


# ---- R7: task_update only validates fields it actually touches ---------

def test_mcp_task_update_validates_only_touched_oracle_fields(project):
    created = json.loads(_call(
        "task_create", {"title": "task_update touch scope"}, project))
    tid = created["id"]

    # Untouched oracle/proof_type/verify -> unrelated field update passes
    # even though the task has no oracle at all (nothing to contradict).
    untouched = json.loads(_call(
        "task_update", {"id": tid, "priority": 5}, project))
    assert "error" not in untouched, untouched

    # Now touch verify+proof_type+oracle together with a hard contradiction
    # -> rejected, and the update must not have applied.
    rejected = json.loads(_call(
        "task_update",
        {"id": tid, "oracle": "the new pytest passes",
         "proof_type": "test", "verify": []},
        project,
    ))
    assert rejected.get("error") == "oracle_validation_failed", rejected

    # Fix it -> accepted, spec summary attached.
    accepted = json.loads(_call(
        "task_update",
        {"id": tid, "oracle": "the new pytest passes",
         "proof_type": "test", "verify": [_PYTEST_ID]},
        project,
    ))
    assert "error" not in accepted, accepted
    assert accepted["oracle_spec"]["adapter"] == "pytest_ids"
