"""task_update can set tags over MCP (task a1f2244d).

MCP task_update has never exposed `tags`, even though task_create has always
had it and PATCH /api/tasks/{id} accepts it (2026-07-16). control_plane
authorises a slice to touch POLICY_FILES only when its task carries the
policy-change / control-plane tag, and GitHub-imported tasks are created with
tags fixed at ["github", "external"] - so an agent driving purely through MCP
could not self-authorise an imported policy ticket (issue #222, 2026-07-28).

Pins, RED-first:
  * AC-4: the task_update tool schema advertises "tags".
  * AC-1: MCP task_update(tags=[...]) durably sets tags (read back through a
    SEPARATE project-context lookup, not the echoed response).
  * AC-2: the negative - an update call that OMITS tags leaves the existing
    tags untouched (never blanked to []).
  * AC-3: a github-tagged task, once tagged policy-change via MCP, is
    accepted by control_plane.policy_change_authorized.
  * AC-5: the task_update handler routes through task_svc.update directly -
    no parallel/raw HTTP path to drift from the REST route.

These FAIL before "tags" exists in the schema + update_kwargs allowlist.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext so handle_tool resolves a tmp-backed task_svc
    (mirrors test_task_title_rename / test_full_outcome_complete)."""
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-tags-over-mcp"
    pc._contexts.clear()


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


# ---- AC-4: schema advertises tags --------------------------------------

def test_task_update_schema_advertises_tags():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    assert "tags" in by_name["task_update"].inputSchema["properties"]


# ---- AC-1: tags set through MCP are durable ----------------------------

def test_mcp_task_update_sets_tags(project):
    from prism_service.project_context import get_project

    created = json.loads(_call(
        "task_create",
        {"title": "via mcp original", "tags": ["github", "external"]},
        project,
    ))
    tid = created["id"]

    json.loads(_call(
        "task_update",
        {"id": tid, "tags": ["policy-change", "control-plane"]},
        project,
    ))

    # Durable, not echo: read through a SEPARATE project-context lookup.
    fresh = get_project(project).task_svc.get(tid)
    assert fresh.tags == ["policy-change", "control-plane"]


# ---- AC-2: the negative - omitted tags leaves tags untouched -----------

def test_mcp_task_update_omitted_tags_leaves_existing_tags_untouched(project):
    from prism_service.project_context import get_project

    created = json.loads(_call(
        "task_create",
        {"title": "github import", "tags": ["github", "external"]},
        project,
    ))
    tid = created["id"]

    # An update for a COMPLETELY unrelated reason, no "tags" key at all.
    json.loads(_call(
        "task_update", {"id": tid, "title": "renamed, tags untouched"}, project,
    ))

    fresh = get_project(project).task_svc.get(tid)
    assert fresh.tags == ["github", "external"], (
        "omitting tags must mean UNTOUCHED, never blanked to []"
    )


# ---- AC-3: a github-tagged task can self-authorise a policy change -----

def test_mcp_tagged_task_authorises_policy_change(project):
    from prism_service.project_context import get_project
    from prism_service.services import control_plane

    created = json.loads(_call(
        "task_create",
        {"title": "imported policy ticket", "tags": ["github", "external"]},
        project,
    ))
    tid = created["id"]

    before = get_project(project).task_svc.get(tid)
    assert not control_plane.policy_change_authorized(before)

    json.loads(_call(
        "task_update",
        {"id": tid, "tags": ["github", "external", "policy-change"]},
        project,
    ))

    after = get_project(project).task_svc.get(tid)
    assert control_plane.policy_change_authorized(after)


# ---- AC-5: no drift - the handler calls task_svc.update directly ------

def test_mcp_task_update_handler_has_no_parallel_http_path():
    import prism_service.mcp.tools as tools_mod

    src = inspect.getsource(tools_mod)
    start = src.index('if name == "task_update":')
    end = src.index('if name == "task_link_session":', start)
    branch = src[start:end]

    assert "task_svc.update(" in branch
    assert "httpx" not in branch
    assert "requests." not in branch
    assert "urlopen" not in branch
