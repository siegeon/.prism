#!/usr/bin/env python3
"""
Tests for Conductor.select_relevant_skills() with a realistic customer skill set.

Models a TalentSyncPro brownfield project with 8 top-level lifecycle skills
(/task, /build, /verify, /ship, /operate, /test, /review, /checkin) and
25 sub-skills across BUILD(9), VERIFY(9), SHIP(3), OPERATE(4) phases.

Acceptance criteria:
- AC-1: write_failing_tests → test/verify-related skills (blackbox, validate, test-design, etc.)
- AC-2: implement_tasks → build-related skills (api, db, domain, query, patterns, etc.)
- AC-3: draft_story → task/planning skills
- AC-4: verify_green_state → verify/test skills
- AC-5: At least 3 customer skills match each non-gate step
"""
import json
import sys
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
FIXTURES_DIR = Path(__file__).resolve().parent / "harness" / "fixtures"
sys.path.insert(0, str(HOOKS_DIR))


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

def _load_customer_skills() -> list:
    """Load skills from customer-skills.jsonl fixture."""
    fixture = FIXTURES_DIR / "customer-skills.jsonl"
    skills = []
    with open(fixture, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                skills.append(json.loads(line))
    return skills


def _make_conductor_no_brain():
    """Create a Conductor with Brain disabled (no DB required)."""
    from conductor_engine import Conductor
    c = object.__new__(Conductor)
    c._brain = None
    c._brain_available = False
    c.last_had_brain_context = 0
    c.last_prompt_id = ""
    return c


CUSTOMER_SKILLS = _load_customer_skills()

_TEST_SKILL_NAMES = {
    "blackbox", "validate", "test-design", "integration-test", "smoke-test",
    "contract-test", "e2e-test", "regression", "coverage", "test", "verify",
}
_BUILD_SKILL_NAMES = {
    "api", "db", "domain", "query", "patterns", "scaffold", "migrate",
    "refactor", "inject", "build",
}
_TASK_SKILL_NAMES = {
    "task",
}


# ---------------------------------------------------------------------------
# AC-1: write_failing_tests → test/verify-related skills
# ---------------------------------------------------------------------------

def test_ac1_write_failing_tests_returns_test_related_skills():
    """select_relevant_skills('write_failing_tests') returns test/verify-related customer skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("write_failing_tests", "qa", CUSTOMER_SKILLS)
    names = {s["name"] for s in result}
    matched_test = names & _TEST_SKILL_NAMES
    assert matched_test, (
        f"write_failing_tests should return at least one test/verify skill; got {names}"
    )


def test_ac1_write_failing_tests_result_bounded():
    """select_relevant_skills('write_failing_tests') returns at most 5 skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("write_failing_tests", "qa", CUSTOMER_SKILLS)
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# AC-2: implement_tasks → build-related skills
# ---------------------------------------------------------------------------

def test_ac2_implement_tasks_returns_build_related_skills():
    """select_relevant_skills('implement_tasks') returns build-related customer skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("implement_tasks", "dev", CUSTOMER_SKILLS)
    names = {s["name"] for s in result}
    matched_build = names & _BUILD_SKILL_NAMES
    assert matched_build, (
        f"implement_tasks should return at least one build skill; got {names}"
    )


def test_ac2_implement_tasks_result_bounded():
    """select_relevant_skills('implement_tasks') returns at most 5 skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("implement_tasks", "dev", CUSTOMER_SKILLS)
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# AC-3: draft_story → task/planning skills
# ---------------------------------------------------------------------------

def test_ac3_draft_story_returns_task_or_planning_skills():
    """select_relevant_skills('draft_story') returns task/planning-related customer skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("draft_story", "sm", CUSTOMER_SKILLS)
    assert len(result) > 0, "draft_story should return at least one skill"
    names = {s["name"] for s in result}
    # task skill is top-level for planning; story/plan/design keywords also match
    task_or_planning = names & {"task", "design", "plan", "review"}
    assert task_or_planning or len(result) >= 3, (
        f"draft_story should match task/planning skills or return fallback; got {names}"
    )


def test_ac3_draft_story_result_bounded():
    """select_relevant_skills('draft_story') returns at most 5 skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("draft_story", "sm", CUSTOMER_SKILLS)
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# AC-4: verify_green_state → verify/test skills
# ---------------------------------------------------------------------------

def test_ac4_verify_green_state_returns_verify_test_skills():
    """select_relevant_skills('verify_green_state') returns verify/test customer skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("verify_green_state", "qa", CUSTOMER_SKILLS)
    names = {s["name"] for s in result}
    matched_verify = names & _TEST_SKILL_NAMES
    assert matched_verify, (
        f"verify_green_state should return at least one verify/test skill; got {names}"
    )


def test_ac4_verify_green_state_result_bounded():
    """select_relevant_skills('verify_green_state') returns at most 5 skills."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills("verify_green_state", "qa", CUSTOMER_SKILLS)
    assert len(result) <= 5


# ---------------------------------------------------------------------------
# AC-5: At least 3 customer skills match each non-gate step
# ---------------------------------------------------------------------------

NON_GATE_STEPS = [
    ("write_failing_tests", "qa"),
    ("implement_tasks", "dev"),
    ("draft_story", "sm"),
    ("verify_green_state", "qa"),
    ("review_previous_notes", "sm"),
    ("verify_plan", "sm"),
]


@pytest.mark.parametrize("step_id,agent", NON_GATE_STEPS)
def test_ac5_at_least_3_customer_skills_match_each_non_gate_step(step_id, agent):
    """Each non-gate step returns at least 3 customer skills from the 33-skill set."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills(step_id, agent, CUSTOMER_SKILLS)
    assert len(result) >= 3, (
        f"step '{step_id}' should return >= 3 customer skills; got {len(result)}: "
        f"{[s['name'] for s in result]}"
    )


# ---------------------------------------------------------------------------
# Gate steps return empty list
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate_step", ["red_gate", "green_gate"])
def test_gate_steps_return_empty_with_customer_skills(gate_step):
    """Gate steps return no customer skills regardless of skill set size."""
    c = _make_conductor_no_brain()
    result = c.select_relevant_skills(gate_step, "sm", CUSTOMER_SKILLS)
    assert result == [], f"Gate step '{gate_step}' must return [] with customer skills"


# ---------------------------------------------------------------------------
# Fixture integrity checks
# ---------------------------------------------------------------------------

def test_customer_skills_fixture_has_33_skills():
    """customer-skills.jsonl contains 8 top-level + 25 sub-skills = 33 total."""
    assert len(CUSTOMER_SKILLS) == 33, (
        f"Expected 33 customer skills, got {len(CUSTOMER_SKILLS)}"
    )


def test_customer_skills_fixture_all_have_required_fields():
    """Every skill in customer-skills.jsonl has name, description, and priority fields."""
    for skill in CUSTOMER_SKILLS:
        assert "name" in skill, f"Skill missing 'name': {skill}"
        assert "description" in skill, f"Skill missing 'description': {skill!r}"
        assert "priority" in skill, f"Skill missing 'priority': {skill!r}"
        assert skill["name"], f"Skill name must not be empty: {skill!r}"


def test_customer_skills_fixture_names_are_unique():
    """No duplicate skill names in customer-skills.jsonl."""
    names = [s["name"] for s in CUSTOMER_SKILLS]
    assert len(names) == len(set(names)), (
        f"Duplicate skill names found: {[n for n in names if names.count(n) > 1]}"
    )
