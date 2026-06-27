"""RED scaffold — seedable principles + epic green_gate roll-up (issue #171).

The conductor's governance gates are UNSATISFIABLE on a fresh customer
project today:

  * plan_coverage is fail-closed on require_principles (empty principles
    never score green), and the only seeder (seed_prism_principles) seeds
    PRISM's OWN models/services rules — useless on a customer repo. 6.7
    retired the override path, so a fresh project HARD-STOPS at plan_gate.
  * an epic's green_gate demands its own red->green even though its
    children already carry passing completion_proof — no child->parent
    roll-up — so agents fall back to task_update(status=done).

Pins, RED-first (these FAIL before seed_default_principles / principles_seed
/ the CLI handler / epic_rollup_verdict exist):

  * AC-1 — seed_default_principles seeds GENERIC defaults (not ARC-PRISM-*)
    into a fresh store, loadable via load_principles; the principles_seed
    MCP tool dispatches end-to-end through the real handler.
  * AC-2 — score_plan_coverage with the seeded defaults scores a compliant
    plan green; with [] principles it still fails (fail-closed preserved).
  * AC-3 — the `prism principles seed` CLI handler seeds + reports a count.
  * AC-4 — epic_rollup_verdict: all children done+strong-proof -> pass;
    one incomplete or weak-proof child -> fail with a concrete reason.
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


def _mem(tmp_path):
    from prism_service.services.memory_service import MemoryService
    return MemoryService(str(tmp_path / "mulch"))


# ── AC-1: generic default principles seed + load ───────────────────────

def test_seed_default_principles_seeds_generic_defaults(tmp_path):
    from prism_service.services.arc_governance import (
        load_principles, seed_default_principles)
    svc = _mem(tmp_path)
    stored = seed_default_principles(svc)
    assert stored, "seed_default_principles must store at least one principle"
    rules = load_principles(svc)
    assert rules, "defaults must be loadable on a fresh store"
    layer_rules = [r for r in rules if r.get("kind") == "layer_rule"]
    assert layer_rules, "at least one machine-checkable layer rule expected"
    ids = {r.get("id") for r in rules}
    assert not any(str(i).startswith("ARC-PRISM-") for i in ids), (
        "defaults must be GENERIC, not PRISM's own models/services rules")
    for r in layer_rules:
        assert r.get("from") and r.get("must_not_depend_on"), r


def test_seed_default_principles_is_idempotent(tmp_path):
    from prism_service.services.arc_governance import (
        load_principles, seed_default_principles)
    svc = _mem(tmp_path)
    seed_default_principles(svc)
    n1 = len(load_principles(svc))
    seed_default_principles(svc)
    n2 = len(load_principles(svc))
    assert n1 == n2, "re-seeding must not duplicate (same-name supersedes)"


def test_seed_default_principles_accepts_custom_rules(tmp_path):
    from prism_service.services.arc_governance import (
        load_principles, seed_default_principles)
    svc = _mem(tmp_path)
    custom = [{"id": "ARC-CUSTOM-1", "kind": "layer_rule",
               "from": "ui", "must_not_depend_on": "data"}]
    seed_default_principles(svc, rules=custom)
    ids = {r.get("id") for r in load_principles(svc)}
    assert "ARC-CUSTOM-1" in ids


@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-171"
    pc._contexts.clear()


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


def test_principles_seed_mcp_tool_registered_interactive():
    from prism_service.mcp.tools import TOOLS, INTERACTIVE_TOOL_NAMES
    names = {t.name for t in TOOLS}
    assert "principles_seed" in names, "principles_seed MCP tool not registered"
    assert "principles_seed" in INTERACTIVE_TOOL_NAMES


def test_principles_seed_mcp_dispatch_seeds_fresh_project(project):
    from prism_service.project_context import get_project
    from prism_service.services.arc_governance import load_principles

    out = json.loads(_call("principles_seed", {}, project))
    assert out.get("seeded", 0) > 0, out
    # Durable: a SEPARATE read through the project context sees the seed.
    rules = load_principles(get_project(project).memory_svc)
    assert len(rules) > 0


# ── AC-2: plan_coverage green with seeded defaults, fail-closed on [] ───

_STORY = """# Story: 171 sample
## Summary
Seedable governance sample.
## Requirements
- FR-1: principles seed on a fresh project
## Acceptance Criteria
- AC-1: defaults seed green — oracle: pytest tests/unit/test_seedable_gates.py
"""

_PLAN = {
    "story_md": _STORY,
    "plan_doc": "## Plan\n- step covers AC-1",
    "plan_diagram": "flowchart TD\n  interface --> application",
}


def test_plan_coverage_green_with_seeded_defaults(tmp_path):
    from prism_service.services import arc_governance as g
    svc = _mem(tmp_path)
    g.seed_default_principles(svc)
    principles = g.load_principles(svc)
    res = g.score_plan_coverage(
        _PLAN, g.load_rubrics()["plan_coverage"], principles)
    assert res["ok"] is True, res


def test_plan_coverage_still_fails_closed_on_empty_principles(tmp_path):
    from prism_service.services import arc_governance as g
    res = g.score_plan_coverage(_PLAN, g.load_rubrics()["plan_coverage"], [])
    assert res["ok"] is False
    assert "principle" in res["reason"].lower(), res


# ── AC-3: CLI `principles seed` handler ────────────────────────────────

def test_cli_principles_seed_handler_seeds_and_reports(tmp_path, monkeypatch,
                                                       capsys):
    import argparse
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    from prism_service.cli.prism_cli import cmd_principles_seed
    from prism_service.services.arc_governance import load_principles

    rc = cmd_principles_seed(argparse.Namespace(project="cli-171"))
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "seed" in out and any(ch.isdigit() for ch in out)
    assert load_principles(pc.get_project("cli-171").memory_svc)
    pc._contexts.clear()


# ── AC-4: epic green_gate child roll-up verdict ────────────────────────

class _Kid:
    def __init__(self, status, proof, parent_id="p", id="kid"):
        self.status = status
        self.completion_proof = proof
        self.parent_id = parent_id
        self.id = id


def test_epic_rollup_passes_when_all_children_done_strong():
    from prism_service.services.conductor_service import epic_rollup_verdict
    ok, reason = epic_rollup_verdict([
        _Kid("done", "pytest -q: 9 passed; screenshot :8888/a.png", id="a"),
        _Kid("done", "pytest -q: 3 passed", id="b"),
    ])
    assert ok is True, reason


def test_epic_rollup_fails_on_incomplete_child():
    from prism_service.services.conductor_service import epic_rollup_verdict
    ok, reason = epic_rollup_verdict([
        _Kid("done", "pytest -q: 9 passed", id="a"),
        _Kid("in_progress", "pytest -q: 9 passed", id="b"),
    ])
    assert ok is False
    assert "child" in reason.lower()


def test_epic_rollup_fails_on_weak_proof_child():
    from prism_service.services.conductor_service import epic_rollup_verdict
    ok, reason = epic_rollup_verdict([
        _Kid("done", "pytest -q: 9 passed", id="a"),
        _Kid("done", "", id="bbbb1234"),
    ])
    assert ok is False
    assert "proof" in reason.lower()


def test_epic_rollup_ignores_cancelled_children():
    from prism_service.services.conductor_service import epic_rollup_verdict
    ok, _ = epic_rollup_verdict([
        _Kid("done", "pytest -q: 9 passed", id="a"),
        _Kid("cancelled", "", id="c"),
    ])
    assert ok is True


def test_epic_rollup_no_children_is_not_an_epic():
    from prism_service.services.conductor_service import epic_rollup_verdict
    ok, reason = epic_rollup_verdict([])
    assert ok is False
    assert reason


# ── AC-4 integration: roll-up rides the real green_gate (no override) ───

def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)
    return task_svc, cond


def _drive_to_green_gate(task_svc, cond, t):
    guard = 0
    cleared = 0
    while task_svc.get(t.id).workflow_step != "green_gate" and guard < 30:
        if task_svc.get(t.id).gate_state == "pending":
            cleared += 1
            cond.gate_decide(
                t.id, "approve",
                reason="walk; independent re-run: pytest -> 1 failed",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
        else:
            cond.advance_task(t.id, validation="step")
        guard += 1
    assert task_svc.get(t.id).workflow_step == "green_gate"


def test_epic_green_gate_passes_by_child_rollup_no_override(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic parent")
    for i in range(2):
        c = task_svc.create(title=f"child {i}", parent_id=parent.id)
        task_svc.update(
            c.id, status="done",
            completion_proof=f"pytest -q: 5 passed; screenshot :8888/c{i}.png")
    _drive_to_green_gate(task_svc, cond, parent)
    # Parent carries NO artifact of its own — roll-up of the children's
    # proofs satisfies the gate WITHOUT override.
    res = cond.gate_decide(
        parent.id, "approve", reason="epic complete via child roll-up")
    assert res["ok"] is True, res
    assert task_svc.get(parent.id).full_outcome_complete is True


def test_epic_green_gate_fails_on_incomplete_child(tmp_path):
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic parent weak")
    good = task_svc.create(title="good child", parent_id=parent.id)
    task_svc.update(good.id, status="done",
                    completion_proof="pytest -q: 9 passed; :8888/g.png")
    task_svc.create(title="incomplete child", parent_id=parent.id)  # pending
    _drive_to_green_gate(task_svc, cond, parent)
    res = cond.gate_decide(parent.id, "approve", reason="try anyway")
    assert res["ok"] is False, res
    assert "child" in str(res.get("reason", "")).lower()
