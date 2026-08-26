"""Every task write path returns its STE style block (task 6e611531).

TaskService.create/update already run the deterministic STE normaliser
(task 36283d72, services/ste.py) and set ``self.last_style`` to the
combined style_block for that call. This file pins that the SAME report
travels out on every write path a caller can use:

  * POST /api/tasks (create)
  * PATCH /api/tasks/{id} (update)
  * MCP task_create
  * MCP task_update
  * POST /api/signals/{id}/promote (creates through TaskService.create,
    so it inherits normalisation for free -- proven here, not edited)

Each response carries a top-level "style" key shaped by
``ste.style_block``: {"fixed": {field: [rule, ...]}, "findings": [...]}.
No test hard-codes an expected fixed string -- each computes the expected
result by calling ste.normalize/ste.check directly, the same way the
production code does.
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

from prism_service.services import ste  # noqa: E402

# A description that trips several safe-fix rules at once: a contraction,
# a filler phrase, a marketing word, and a semicolon.
_DESCRIPTION = "We don't do this in order to be robust; it's fine."

# One STRICT-mode sentence with no period, over the 20-word cap, so
# check() reports "sentence-length" for the oracle field.
_ORACLE_LONG = (
    "the user opens settings and toggles the new feature flag and sees "
    "the change reflected immediately without any need to refresh reload "
    "or restart the running app at all today right now"
)
assert len(_ORACLE_LONG.split()) >= 30


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so the API router, the signals router, and
    MCP handle_tool all resolve the SAME tmp-backed task_svc (mirrors
    test_task_channel_provenance.py / test_task_names_its_workflow.py)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-ste-write-paths"
    pc._contexts.clear()


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def _signals_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import signals as signals_api
    app = FastAPI()
    app.include_router(signals_api.router, prefix="/api/signals")
    return TestClient(app)


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


# ── (1) POST /api/tasks (create) ────────────────────────────────────────


def test_api_create_normalises_description_and_returns_style(project):
    client = _api_client()
    r = client.post(
        "/api/tasks", params={"project": project},
        json={"title": "ste create check", "description": _DESCRIPTION,
              "oracle": _ORACLE_LONG})
    assert r.status_code in (200, 201), r.text
    body = r.json()

    expected_desc, expected_rules = ste.normalize(_DESCRIPTION, "flavored")
    assert body["task"]["description"] == expected_desc

    style = body["style"]
    assert set(style["fixed"].get("description", [])) == set(expected_rules)
    oracle_findings = [f for f in style["findings"] if f["field"] == "oracle"]
    assert any(f["rule"] == "sentence-length" for f in oracle_findings), (
        style["findings"])


# ── (2) PATCH /api/tasks/{id} (update) ──────────────────────────────────


def test_api_update_normalises_description_and_returns_style(project):
    client = _api_client()
    create = client.post(
        "/api/tasks", params={"project": project},
        json={"title": "ste update check"})
    assert create.status_code in (200, 201), create.text
    tid = create.json()["task"]["id"]

    r = client.patch(
        f"/api/tasks/{tid}", params={"project": project},
        json={"description": _DESCRIPTION})
    assert r.status_code == 200, r.text
    body = r.json()

    expected_desc, expected_rules = ste.normalize(_DESCRIPTION, "flavored")
    assert body["task"]["description"] == expected_desc

    style = body["style"]
    assert set(style["fixed"].get("description", [])) == set(expected_rules)


# ── (3) MCP task_create / task_update ───────────────────────────────────


def test_mcp_task_create_returns_style(project):
    created = json.loads(_call(
        "task_create",
        {"title": "mcp ste create check", "description": _DESCRIPTION},
        project))

    expected_desc, expected_rules = ste.normalize(_DESCRIPTION, "flavored")
    assert created["description"] == expected_desc
    assert set(created["style"]["fixed"].get("description", [])) == set(
        expected_rules)


def test_mcp_task_update_returns_style(project):
    created = json.loads(_call(
        "task_create", {"title": "mcp ste update check"}, project))
    updated = json.loads(_call(
        "task_update",
        {"id": created["id"], "description": _DESCRIPTION},
        project))

    expected_desc, expected_rules = ste.normalize(_DESCRIPTION, "flavored")
    assert updated["description"] == expected_desc
    assert set(updated["style"]["fixed"].get("description", [])) == set(
        expected_rules)


# ── (4) POST /api/signals/{id}/promote ──────────────────────────────────
# promote_signal (api/signals.py) creates through TaskService.create with
# no code of its own touched by this task -- proven here, not edited.


def test_promote_normalises_the_description(project):
    signals_client = _signals_client()
    posted = signals_client.post(
        "/api/signals", params={"project": project},
        json={"channel": "slack", "channel_ref": "C1/2", "subject": "ping",
              "body": "please look at this", "sender": "alice"})
    assert posted.status_code == 200, posted.text
    signal = posted.json()["signal"]

    r = signals_client.post(
        f"/api/signals/{signal['id']}/promote",
        params={"project": project},
        json={"title": "go fix it", "description": _DESCRIPTION})
    assert r.status_code == 200, r.text
    task = r.json()["task"]

    expected_desc, _rules = ste.normalize(_DESCRIPTION, "flavored")
    assert task["description"] == expected_desc

    from prism_service.project_context import get_project
    stored = get_project(project).task_svc.get(task["id"])
    assert stored.description == expected_desc


# ── (5) fenced code block survives byte-identical ───────────────────────


def test_fenced_code_block_survives_byte_identical(project):
    client = _api_client()
    fence = (
        "```\n"
        "if not ok:\n"
        "    raise ValueError(\"don't; retry\")\n"
        "```"
    )
    description = f"Explain the fix below.\n\n{fence}\n\nThat is the whole change."

    r = client.post(
        "/api/tasks", params={"project": project},
        json={"title": "fence check", "description": description})
    assert r.status_code in (200, 201), r.text
    stored_desc = r.json()["task"]["description"]
    assert fence in stored_desc, stored_desc


# ── (6) plan_doc: checked, never rewritten ──────────────────────────────


def test_plan_doc_semicolon_inside_code_is_flagged_and_never_rewritten(project):
    client = _api_client()
    create = client.post(
        "/api/tasks", params={"project": project},
        json={"title": "plan doc check"})
    assert create.status_code in (200, 201), create.text
    tid = create.json()["task"]["id"]

    plan_doc = "See `run this; then that` for details."
    r = client.patch(
        f"/api/tasks/{tid}", params={"project": project},
        json={"plan_doc": plan_doc})
    assert r.status_code == 200, r.text
    body = r.json()

    # plan_doc is check-only: a human already approved it at plan_gate, so
    # the stored text must come back byte-identical to what was sent.
    assert body["task"]["plan_doc"] == plan_doc

    style = body["style"]
    plan_findings = [f for f in style["findings"] if f["field"] == "plan_doc"]
    assert any(f["rule"] == "semicolon" for f in plan_findings), (
        style["findings"])
