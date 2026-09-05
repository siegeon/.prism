"""Task 0c396de2: the Workflows page shows tier-0 Bots with their agentic
workflows underneath (owner 2026-08-27, mx-7df790).

Each WORKFLOWS entry (models/workflow.py) is a tier-0 deterministic FSM Bot.
The conductor's behaviours are its tier-1 agentic children. A step is
agentic from WHAT IT DOES (type == "agent"; an http step into reason-loop),
never from its name. Persona cards are roles, not Bots.

Trace: AC-1..AC-8 of the task plan_doc map one-to-one onto the tests below.
UI ACs pin the ACTUAL TSX source (the PRISM SPA has no JS test runner).
"""

from __future__ import annotations

import re
import types
from pathlib import Path

import pytest

from prism_service.api import workflows as workflows_api
from prism_service.models.workflow import WORKFLOWS

TSX = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src" / "pages" / "WorkflowsPage.tsx"
BOT_HEADER = "BOT · TIER 0 · deterministic FSM"
FSM_TO_ENTRY = {"implement": "conductor", "triage": "triage",
                "align_language": "align_language", "promote_to_law": "promote_to_law"}


class _Svc:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)

    def list(self, **_kw):
        return list(self.tasks)


def _behaviour(step_ids_kinds_urls):
    return {"id": "story-gate-check", "steps": [
        {"id": sid, "kind": kind, "url": url, "command": "" if kind == "http" else "echo ok"}
        for sid, kind, url in step_ids_kinds_urls]}


@pytest.fixture
def body(monkeypatch):
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc()))
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [
                            {"id": "story-gate-check", "name": "Story gate check",
                             "description": "", "steps": [], "bots": [], "occupancy": {}},
                            {"id": "ci-local-dev", "name": "CI to local dev",
                             "description": "", "steps": [], "bots": [], "occupancy": {}}])
    return workflows_api.get_workflows("prism")


def _by_id(body):
    return {w["id"]: w for w in body["workflows"]}


# AC-1
def test_every_workflows_entry_is_a_tier0_bot(body):
    entries = _by_id(body)
    for fsm_id, entry_id in FSM_TO_ENTRY.items():
        entry = entries[entry_id]
        assert entry["tier"] == 0, entry_id
        assert entry["fsm_id"] == fsm_id, entry_id
        assert entry["bot_header"] == BOT_HEADER, entry_id
    tier0 = {w["id"] for w in body["workflows"] if w.get("tier") == 0}
    assert tier0 == {FSM_TO_ENTRY[k] for k in WORKFLOWS}, (
        "tier 0 must follow from the entry's steps coming from WORKFLOWS, "
        "not from an id list")


# AC-2
def test_conductor_behaviours_are_tier1_children(body):
    children = [w for w in body["workflows"] if w.get("parent_id") == "conductor"]
    assert children
    assert {w["id"] for w in children} >= {"validation", "story-gate-check"}
    assert all(w["tier"] == 1 for w in children), [(w["id"], w.get("tier")) for w in children]


# AC-3
def test_step_agentic_flag_derives_from_step_kind(body, monkeypatch):
    steps = {s["id"]: s for s in _by_id(body)["conductor"]["steps"]}
    assert steps["draft_story"]["agentic"] is True
    assert steps["story_gate"]["agentic"] is False
    triage = {s["id"]: s for s in _by_id(body)["triage"]["steps"]}
    assert triage["intake"]["agentic"] is False
    for w in body["workflows"]:
        for s in w["steps"]:
            assert isinstance(s.get("agentic"), bool), (w["id"], s["id"])

    def engine(path, **_kw):
        if "/behaviors/" in path:
            return _behaviour([("run-check", "shell", ""),
                               ("call-llm", "http", "http://x/workflow-steps/reason-loop"),
                               ("run-tests", "http", "http://x/workflow-steps/reason-loop")])
        return {"fsms": [{"fsmId": "conductor", "behaviorIds": ["story-gate-check"]}]}

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", engine)
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc(), root=Path("/tmp")),
                        raising=False)
    entries = workflows_api._conductor_behavior_workflows("prism")
    flags = {s["id"]: s["agentic"] for s in entries[0]["steps"]}
    assert flags["run-check"] is False
    assert flags["call-llm"] is True
    assert flags["run-tests"] is True, "the name does not decide; the url does"


# AC-4
def test_persona_cards_moved_under_roles(body):
    assert {r["id"] for r in body["roles"]} == {"sm", "qa", "dev"}
    for w in body["workflows"]:
        assert not any("card" in b for b in w.get("bots", [])), w["id"]


# AC-8
def test_live_reads_and_ci_stay_outside_the_bot_tree(body):
    entries = _by_id(body)
    assert "tier" not in entries["knowledge_health"]
    assert "parent_id" not in entries["knowledge_health"]
    assert "parent_id" not in entries["ci-local-dev"]


def _src():
    return re.sub(r"\{/\*.*?\*/\}|//[^\n]*", "", TSX.read_text(encoding="utf-8"), flags=re.S)


# AC-5
def test_workflows_page_renders_bot_header_and_nests_children():
    src = _src()
    m = re.search(r"workflow\.tier === 0 &&[^;]*?\{workflow\.bot_header\}", src, re.S)
    assert m, "no rendered branch on workflow.tier === 0 emitting workflow.bot_header"
    assert "candidate.parent_id === workflow.id" in src


# AC-6
def test_step_marker_reads_agentic_field():
    src = _src()
    assert 'step.agentic ? "agentic" : "deterministic"' in src
    assert not re.search(r"(draft_story|write_failing_tests)\s*:\s*[\"']?(agentic|deterministic)", src)


# AC-7
def test_roles_heading_holds_persona_cards():
    src = _src()
    assert re.search(r"<h[1-6][^>]*>\s*Roles\s*</h[1-6]>", src), "no Roles heading"
    assert re.search(r"data\.roles\b[^;]*?\.map\(", src, re.S), "Roles section must map data.roles"
