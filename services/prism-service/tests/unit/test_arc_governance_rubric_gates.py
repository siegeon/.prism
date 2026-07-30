"""RED scaffold — architecture governance rubric gates (task 8579d49e).

Pins b1/b2/b3/c1/c3/e2 of 'Architecture governance enforced through
conductor gates':

  * b1 — rubrics are YAML DATA (not code) carrying story_complete +
    plan_coverage entries, loadable via arc_governance.load_rubrics();
  * b2 — score_story_complete(evidence, rubric) is pure: a compliant
    story passes, a missing AC oracle fails citing the AC id;
  * b3/c1 — score_plan_coverage diffs AC-id coverage plan-vs-story and
    flags a plan_diagram edge violating a Brain-stored principle BEFORE
    code; empty principles must NOT score green (misfire guard);
  * b3 — forced-override retired: _VERIFIER_RULES no longer maps these
    kinds to None (conductor_service.py:1282-1283 today) and the MCP
    gate doc drops the manual-only contract; a WORKFLOW gate actually
    inherits each kind so the rubric is reachable end-to-end;
  * b2 e2e — gate_decide approves a compliant story WITHOUT override
    and refuses a non-compliant one with the rubric reason;
  * c3 — green_gate_conformance_note annotates, never blocks, beside
    the proof/misfire/outcome sibling notes;
  * e2 — plan_coverage rubric requires plan_diagram present + parsing.

FAILS today: prism_service.services.arc_governance does not exist and
no gate inherits story_complete/plan_coverage.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

COMPLIANT_STORY = """# Story: governance sample

## Summary
Sample story used by the governance rubric red tests.

## Requirements
- FR-1: rubric scoring is a pure function of (evidence, rubric)
- NFR-1: no override=True required on a compliant drive

## Acceptance Criteria
- AC-1: compliant story passes the rubric — oracle: pytest \
tests/unit/test_arc_governance_rubric_gates.py::test_compliant_story_scores_ok
- AC-2: non-compliant story fails citing the AC id — oracle: pytest \
tests/unit/test_arc_governance_rubric_gates.py::test_story_missing_oracle_fails_citing_ac_id
"""

# AC-2 has an id but NO oracle — the rubric must name AC-2 in its reason.
STORY_MISSING_ORACLE = COMPLIANT_STORY.replace(
    "- AC-2: non-compliant story fails citing the AC id — oracle: pytest \
tests/unit/test_arc_governance_rubric_gates.py::test_story_missing_oracle_fails_citing_ac_id",
    "- AC-2: non-compliant story fails citing the AC id",
)

PRINCIPLES = [{
    "id": "ARC-1", "kind": "layer_rule",
    "from": "domain", "must_not_depend_on": "infrastructure",
}]

PLAN_OK = {
    "story_md": COMPLIANT_STORY,
    "plan_doc": "## Plan\n- step 1 covers AC-1\n- step 2 covers AC-2",
    "plan_diagram": "flowchart TD\n  api --> domain",
}


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubrics():
    return _gov().load_rubrics()


# ── b1: rubrics are YAML data, not code ────────────────────────────────

def test_rubrics_live_as_yaml_data():
    import yaml
    hits = [p for p in (_SERVICE_ROOT / "prism_service").rglob("*.y*ml")
            if "rubric" in p.name.lower()]
    assert hits, "no rubrics YAML data file shipped under prism_service/"
    data = yaml.safe_load(hits[0].read_text(encoding="utf-8"))
    assert "story_complete" in data and "plan_coverage" in data


def test_load_rubrics_returns_both_kinds():
    rub = _rubrics()
    assert "story_complete" in rub and "plan_coverage" in rub


# ── b2: story_complete is a pure rubric function ───────────────────────

def test_compliant_story_scores_ok():
    g = _gov()
    res = g.score_story_complete(
        {"story_md": COMPLIANT_STORY}, _rubrics()["story_complete"])
    assert res["ok"] is True, res


def test_story_missing_oracle_fails_citing_ac_id():
    g = _gov()
    res = g.score_story_complete(
        {"story_md": STORY_MISSING_ORACLE}, _rubrics()["story_complete"])
    assert res["ok"] is False
    assert "AC-2" in res["reason"], res


def test_story_without_sections_fails():
    g = _gov()
    res = g.score_story_complete(
        {"story_md": "free prose, no sections, no ACs"},
        _rubrics()["story_complete"])
    assert res["ok"] is False


# ── b3/c1/e2: plan_coverage pure functions ─────────────────────────────

def test_plan_covering_all_acs_with_clean_diagram_passes():
    g = _gov()
    res = g.score_plan_coverage(
        PLAN_OK, _rubrics()["plan_coverage"], PRINCIPLES)
    assert res["ok"] is True, res


def test_plan_missing_ac_coverage_flagged():
    g = _gov()
    ev = dict(PLAN_OK, plan_doc="## Plan\n- step 1 covers AC-1 only")
    res = g.score_plan_coverage(ev, _rubrics()["plan_coverage"], PRINCIPLES)
    assert res["ok"] is False
    assert "AC-2" in res.get("missing_ac_ids", []), res


def test_plan_violating_principle_flagged_before_code():
    g = _gov()
    ev = dict(PLAN_OK, plan_diagram="flowchart TD\n  domain --> infrastructure")
    res = g.score_plan_coverage(ev, _rubrics()["plan_coverage"], PRINCIPLES)
    assert res["ok"] is False
    assert res.get("violations"), res
    blob = str(res)
    assert "ARC-1" in blob, "violation must cite the principle id"


def test_empty_principles_do_not_score_green():
    """Misfire guard: zero seeded principles must not silently pass
    conformance — the gate must say principles are missing."""
    g = _gov()
    res = g.score_plan_coverage(PLAN_OK, _rubrics()["plan_coverage"], [])
    assert res["ok"] is False
    assert "principle" in res["reason"].lower(), res


def test_plan_diagram_required_and_must_parse():
    g = _gov()
    rub = _rubrics()["plan_coverage"]
    missing = {k: v for k, v in PLAN_OK.items() if k != "plan_diagram"}
    res = g.score_plan_coverage(missing, rub, PRINCIPLES)
    assert res["ok"] is False and "plan_diagram" in res["reason"], res
    garbage = dict(PLAN_OK, plan_diagram="this is not mermaid at all")
    res2 = g.score_plan_coverage(garbage, rub, PRINCIPLES)
    assert res2["ok"] is False, "non-parsing plan_diagram must be flagged"


# ── b3: forced-override path retired ───────────────────────────────────

def test_verifier_rules_retire_manual_only():
    from prism_service.services.conductor_service import ConductorService
    rules = ConductorService._VERIFIER_RULES
    assert rules.get("story_complete") is not None, "story_complete is still manual-only"
    assert rules.get("plan_coverage") is not None, "plan_coverage is still manual-only"


def test_mcp_gate_doc_drops_manual_only_contract():
    src = (_SERVICE_ROOT / "prism_service" / "mcp" / "tools.py").read_text(
        encoding="utf-8")
    assert "validation kinds (story_complete, plan_coverage)" not in src, (
        "mcp/tools.py still documents story_complete/plan_coverage as "
        "manual-only override kinds (was tools.py:1212)"
    )


def _gate_for(kind: str):
    from prism_service.models.workflow import WORKFLOW_STEPS
    from prism_service.services.conductor_service import ConductorService
    for s in WORKFLOW_STEPS:
        if (s["type"] == "gate"
                and ConductorService._validation_for_gate(s["id"]) == kind):
            return s["id"]
    return None


def test_workflow_gate_inherits_story_complete():
    assert _gate_for("story_complete"), (
        "no WORKFLOW gate inherits validation=story_complete — the story "
        "rubric is unreachable through the conductor")


def test_workflow_gate_inherits_plan_coverage():
    assert _gate_for("plan_coverage"), (
        "no WORKFLOW gate inherits validation=plan_coverage")


# ── b2 end-to-end: gate_decide through the conductor, no override ──────

class StubVerifier:
    """Shell verifier stub that always says pass. The rubric — not this
    stub — must decide the story/plan gates; the non-compliant test
    below proves the rubric verdict wins."""

    def run(self, **kwargs: object) -> dict:
        return {"status": "pass", "tier0": "pass", "tier1": "not-run",
                "tier2": "skipped", "summary": "stub pass"}


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False,
        task_svc=task_svc, verifier_svc=StubVerifier())
    return task_svc, cond


def _walk_to_gate(cond, task_svc, task_id: str, gate_id: str) -> None:
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this unrelated rubric-gate walk can leave review_previous_notes.
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising rubric gates, not a real "
        "premise claim - UNVERIFIED\n"))
    for _ in range(30):
        t = task_svc.get(task_id)
        if t.workflow_step == gate_id and t.gate_state == "pending":
            return
        if t.gate_state == "pending":
            cond.gate_decide(
                task_id, action="approve", override=True,
                reason="walk intermediate; independent re-run: pytest -q",
                actor="walk-bot", session_id="walk-bot")
            continue
        cond.advance_task(task_id)
    raise AssertionError(f"never reached gate {gate_id}")


def test_compliant_story_clears_gate_without_override(tmp_path):
    gate_id = _gate_for("story_complete")
    assert gate_id, "no gate enforces story_complete"
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="governance: compliant story drive")
    task_svc.update(t.id, plan_doc=COMPLIANT_STORY)
    _walk_to_gate(cond, task_svc, t.id, gate_id)
    res = cond.gate_decide(
        t.id, action="approve",
        reason="story rubric evidence: sections + AC ids + oracles present")
    assert res["ok"] is True, res
    assert "override" not in str(res.get("reason", "")).lower(), res


def test_noncompliant_story_refused_with_rubric_reason(tmp_path):
    gate_id = _gate_for("story_complete")
    assert gate_id, "no gate enforces story_complete"
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="governance: non-compliant story drive")
    task_svc.update(t.id, plan_doc=STORY_MISSING_ORACLE)
    _walk_to_gate(cond, task_svc, t.id, gate_id)
    res = cond.gate_decide(
        t.id, action="approve", reason="attempting approve on a bad story")
    assert res["ok"] is False, "rubric must refuse a story whose AC lacks an oracle"
    assert "AC-2" in str(res.get("reason", "")), res


# ── c3: green_gate conformance note — annotate, never block ────────────

def test_green_gate_conformance_note_silent_when_clean():
    from prism_service.services.conductor_service import (
        green_gate_conformance_note)
    assert green_gate_conformance_note({"count": 0, "violations": []}) == ""


def test_green_gate_conformance_note_annotates_violations():
    from prism_service.services.conductor_service import (
        green_gate_conformance_note)
    note = green_gate_conformance_note({
        "count": 1,
        "violations": [{"from": "domain", "to": "infrastructure",
                        "principle": "ARC-1"}],
    })
    assert "⚠" in note, "advisory note must carry the warning marker"
    assert "conformance" in note.lower()


def test_conformance_note_wired_into_gate_decide():
    src = (_SERVICE_ROOT / "prism_service" / "services"
           / "conductor_service.py").read_text(encoding="utf-8")
    assert src.count("green_gate_conformance_note(") >= 2, (
        "green_gate_conformance_note must be CALLED on the green_gate "
        "approve path beside the proof/misfire/outcome notes, not just defined")
