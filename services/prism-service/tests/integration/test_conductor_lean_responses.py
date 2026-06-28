"""RED scaffold — rubric-on-advance + scoped task_list + lean conductor
responses (task 0e071d68), driven THROUGH the real MCP dispatcher
(handle_tool) — the way the MCP server invokes these verbs — not the
service methods in isolation (that gap has shipped false-greens).

  AC-4 — conductor_advance stepping INTO draft_story / verify_plan returns
         result['rubric'] carrying the active rubric schema (ac_id_pattern
         'AC-\\d+'; draft_story also carries oracle_marker 'oracle:').
  AC-5 — task_list(parent_id=epic) returns ONLY that epic's children;
         unrelated tasks are absent (FR-6 — schema + handler + service).
  AC-6 — conductor_advance / conductor_gate with a `fields` projection
         return ONLY the requested keys and OMIT the full task object
         (no plan_doc/plan_diagram leak) (FR-7).

ALL fail today: advance_task returns no 'rubric' key, task_list ignores
parent_id (returns every task), and both conductor handlers always attach
result['task'] = the full task row (tools.py:3874/3896).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _isolated_project(tmp_path, monkeypatch, pid="test-lean-resp"):
    from prism_service import config as cfg
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc
    pc._contexts.clear()
    return pid


def _call(name, arguments, pid):
    from prism_service.mcp.tools import handle_tool
    out = asyncio.run(handle_tool(name, arguments, project_id=pid))
    assert len(out) == 1
    return json.loads(out[0].text)


def _task_svc(pid):
    from prism_service.project_context import get_project
    return get_project(pid).task_svc


# ── AC-4 / FR-5: the active rubric is surfaced on the authoring advance ──

def test_advance_into_draft_story_returns_rubric(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    tid = _call("task_create", {"title": "feature"}, pid)["id"]
    _call("conductor_advance", {"id": tid, "session_id": "test-sid"}, pid)
    res = _call("conductor_advance", {"id": tid, "session_id": "test-sid"}, pid)
    assert res["to_step"] == "draft_story"
    rubric = res.get("rubric")
    assert isinstance(rubric, dict), (
        "conductor_advance into draft_story returned no result['rubric'] — the "
        "active rubric schema must be surfaced on the authoring step (FR-5)")
    assert rubric.get("ac_id_pattern") == r"AC-\d+"
    assert "oracle:" in str(rubric.get("oracle_marker", "")), (
        "the story_complete rubric must carry oracle_marker 'oracle:'")


def test_advance_into_verify_plan_returns_rubric(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    tid = _call("task_create", {"title": "feature"}, pid)["id"]
    # Park at a CLEARED story_gate so the next advance steps into verify_plan.
    _task_svc(pid).update(tid, workflow_step="story_gate", gate_state="passed")
    res = _call("conductor_advance", {"id": tid, "session_id": "test-sid"}, pid)
    assert res["to_step"] == "verify_plan"
    rubric = res.get("rubric")
    assert isinstance(rubric, dict), (
        "conductor_advance into verify_plan returned no result['rubric']")
    assert rubric.get("ac_id_pattern") == r"AC-\d+", (
        "the plan_coverage rubric must carry the AC id pattern")


# ── AC-5 / FR-6: task_list(parent_id) scopes to one epic's children ──────

def test_task_list_parent_id_scopes_to_children(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    epic = _call("task_create", {"title": "epic"}, pid)["id"]
    c1 = _call("task_create", {"title": "child 1", "parent_id": epic}, pid)["id"]
    c2 = _call("task_create", {"title": "child 2", "parent_id": epic}, pid)["id"]
    other = _call("task_create", {"title": "unrelated root"}, pid)["id"]

    scoped = _call("task_list", {"parent_id": epic}, pid)
    ids = {t["id"] for t in scoped}
    assert ids == {c1, c2}, (
        "task_list(parent_id=epic) did not scope to the epic's children — got "
        f"{ids}; the parent_id filter is ignored (epic={epic} other={other})")
    assert all(t.get("parent_id") == epic for t in scoped)


def test_task_list_schema_advertises_parent_id():
    from prism_service.mcp.tools import TOOLS
    props = {t.name: t for t in TOOLS}["task_list"].inputSchema["properties"]
    assert "parent_id" in props, (
        "task_list inputSchema must advertise the parent_id filter (FR-6)")


# ── AC-6 / FR-7: a fields projection returns ONLY requested keys ─────────

def test_conductor_advance_fields_projection(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    tid = _call("task_create", {"title": "feature"}, pid)["id"]
    res = _call("conductor_advance", {
        "id": tid, "session_id": "test-sid",
        "fields": ["from_step", "to_step", "gate_state"]}, pid)
    assert "task" not in res, (
        "conductor_advance with a fields projection still attached the full "
        "task object — the lean response must omit it (FR-7)")
    assert set(res.keys()) == {"from_step", "to_step", "gate_state"}, (
        "conductor_advance fields projection must return ONLY the requested "
        f"keys — got {sorted(res.keys())}")


def test_conductor_gate_fields_projection(tmp_path, monkeypatch):
    pid = _isolated_project(tmp_path, monkeypatch)
    tid = _call("task_create", {"title": "feature"}, pid)["id"]
    # Park on a pending gate; override-clear it (distinct actor, no prior
    # work sessions) so only the projection shape is under test.
    _task_svc(pid).update(tid, workflow_step="story_gate", gate_state="pending")
    res = _call("conductor_gate", {
        "id": tid, "action": "approve", "reason": "manual review ok",
        "override": True, "actor": "verifier-bot", "session_id": "verifier-bot",
        "fields": ["gate_step", "gate_state", "to_step"]}, pid)
    assert "task" not in res, (
        "conductor_gate with a fields projection still attached the full task "
        "object — the lean response must omit it (FR-7)")
    assert set(res.keys()) == {"gate_step", "gate_state", "to_step"}, (
        f"conductor_gate fields projection returned extra keys: {sorted(res.keys())}")


def test_conductor_schemas_advertise_fields():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    for tool in ("conductor_advance", "conductor_gate", "task_list"):
        props = by_name[tool].inputSchema["properties"]
        assert "fields" in props, (
            f"{tool} inputSchema must advertise the fields projection (FR-7)")
