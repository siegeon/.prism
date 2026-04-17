#!/usr/bin/env python3
"""
Multi-turn lifecycle harness: measure Brain learning effectiveness across features.

Simulates two feature builds (Feature A = cold start, Feature B = with learning)
and measures whether Brain usage history improves skill ranking for each step.

AC-2: select_relevant_skills returns different results after learning for ≥3 steps
AC-3: DEV implement_tasks ranks api/db/domain higher after Feature A
AC-4: QA write_failing_tests ranks test-design/e2e-test higher after Feature A
AC-5: score_threshold makes skill count variable (requires engine per-step support)
AC-7: cold-start results are stable (backward compat)
"""
import inspect
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
FIXTURES_DIR = Path(__file__).resolve().parent / "harness" / "fixtures"
sys.path.insert(0, str(HOOKS_DIR))


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------

def _load_customer_skills() -> list:
    fixture = FIXTURES_DIR / "customer-skills.jsonl"
    skills = []
    with open(fixture, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                skills.append(json.loads(line))
    return skills


CUSTOMER_SKILLS = _load_customer_skills()


# ---------------------------------------------------------------------------
# Workflow steps exercised in each feature turn
# ---------------------------------------------------------------------------

TURN_STEPS = [
    ("draft_story",         "sm"),
    ("implement_tasks",     "dev"),
    ("write_failing_tests", "qa"),
    ("verify_green_state",  "qa"),
]

# Simulated skill usage recorded during Feature A build
FEATURE_A_USAGE = {
    "implement_tasks":     {"api": 5, "db": 3, "domain": 2},
    "write_failing_tests": {"e2e-test": 4, "integration-test": 3, "test-design": 2},
    "verify_green_state":  {"validate": 3, "coverage": 2},
    "draft_story":         {"task": 2, "review": 1},
}


# ---------------------------------------------------------------------------
# Conductor factory helpers
# ---------------------------------------------------------------------------

def _make_conductor_no_brain():
    """Conductor with Brain disabled — cold-start keyword/phase matching only."""
    from conductor_engine import Conductor
    c = object.__new__(Conductor)
    c._brain = None
    c._brain_available = False
    c.last_had_brain_context = 0
    c.last_prompt_id = ""
    return c


def _make_conductor_with_step_scores(step_scores: dict):
    """Conductor with mock Brain where get_skill_scores(step_id=X) returns step_scores[X].

    Falls back to merged global scores when called without step_id (current engine
    behaviour before per-step support lands). This lets tests work against both
    the current engine and the updated one.
    """
    from conductor_engine import Conductor

    def mock_get_scores(step_id=""):
        if step_id and step_id in step_scores:
            return step_scores[step_id]
        # Merge all step scores into a global dict (current engine fallback)
        merged: dict = {}
        for scores in step_scores.values():
            for name, cnt in scores.items():
                merged[name] = merged.get(name, 0) + cnt
        return merged

    c = object.__new__(Conductor)
    mock_brain = MagicMock()
    mock_brain.get_skill_scores.side_effect = mock_get_scores
    c._brain = mock_brain
    c._brain_available = True
    c.last_had_brain_context = 0
    c.last_prompt_id = ""
    return c


# ---------------------------------------------------------------------------
# API capability detection for conditional xfail
# ---------------------------------------------------------------------------

def _conductor_has_score_threshold() -> bool:
    from conductor_engine import Conductor
    sig = inspect.signature(Conductor.select_relevant_skills)
    return "score_threshold" in sig.parameters


def _conductor_passes_step_id_to_brain() -> bool:
    """Detect whether conductor passes step_id to brain.get_skill_scores."""
    from conductor_engine import Conductor

    calls: list = []

    def _spy(step_id=""):
        calls.append(step_id)
        return {}

    c = object.__new__(Conductor)
    mock_brain = MagicMock()
    mock_brain.get_skill_scores.side_effect = _spy
    c._brain = mock_brain
    c._brain_available = True
    c.last_had_brain_context = 0
    c.last_prompt_id = ""
    c.select_relevant_skills("implement_tasks", "dev", CUSTOMER_SKILLS[:1])
    return bool(calls) and calls[0] == "implement_tasks"


# ---------------------------------------------------------------------------
# AC-2: Learning changes results for ≥3 steps
# ---------------------------------------------------------------------------

def test_ac2_learning_changes_results_for_at_least_3_steps():
    """Turn 2 (warm) top-5 differs from Turn 1 (cold) for at least 3 of 4 steps.

    'Different' means the ordered skill name list is not identical to cold.
    """
    cold = _make_conductor_no_brain()
    warm = _make_conductor_with_step_scores(FEATURE_A_USAGE)

    changed = 0
    details = []
    for step_id, agent in TURN_STEPS:
        cold_names = [s["name"] for s in cold.select_relevant_skills(step_id, agent, CUSTOMER_SKILLS)]
        warm_names = [s["name"] for s in warm.select_relevant_skills(step_id, agent, CUSTOMER_SKILLS)]
        if cold_names != warm_names:
            changed += 1
        details.append(f"  {step_id}: cold={cold_names} warm={warm_names}")

    assert changed >= 3, (
        f"Expected ≥3 steps to show different results after learning; got {changed}/4.\n"
        + "\n".join(details)
    )


# ---------------------------------------------------------------------------
# AC-3: DEV implement_tasks ranks api/db/domain higher after learning
# ---------------------------------------------------------------------------

def test_ac3_implement_tasks_ranks_learned_skills_higher():
    """api, db, domain should rank at least as high in warm as in cold at implement_tasks."""
    cold = _make_conductor_no_brain()
    warm = _make_conductor_with_step_scores(FEATURE_A_USAGE)

    cold_names = [s["name"] for s in cold.select_relevant_skills("implement_tasks", "dev", CUSTOMER_SKILLS)]
    warm_names = [s["name"] for s in warm.select_relevant_skills("implement_tasks", "dev", CUSTOMER_SKILLS)]

    target_skills = {"api", "db", "domain"}

    # At least 2 of 3 must appear in warm top-5
    warm_present = target_skills & set(warm_names)
    assert len(warm_present) >= 2, (
        f"Expected ≥2 of {{api,db,domain}} in warm implement_tasks top-5; "
        f"got {warm_present} in {warm_names}"
    )

    # Average position must improve (lower index = better rank)
    def _avg_pos(names: list, targets: set) -> float:
        positions = [names.index(n) for n in targets if n in names]
        return sum(positions) / len(positions) if positions else float("inf")

    cold_avg = _avg_pos(cold_names, target_skills)
    warm_avg = _avg_pos(warm_names, target_skills)
    assert warm_avg <= cold_avg, (
        f"implement_tasks: api/db/domain should rank at least as high in warm "
        f"(avg pos {warm_avg:.1f}) as in cold (avg pos {cold_avg:.1f}). "
        f"cold={cold_names} warm={warm_names}"
    )


# ---------------------------------------------------------------------------
# AC-4: QA write_failing_tests ranks test-design/e2e-test higher after learning
# ---------------------------------------------------------------------------

def test_ac4_write_failing_tests_ranks_learned_skills_higher():
    """test-design and e2e-test should appear and rank higher in warm write_failing_tests."""
    cold = _make_conductor_no_brain()
    warm = _make_conductor_with_step_scores(FEATURE_A_USAGE)

    cold_names = [s["name"] for s in cold.select_relevant_skills("write_failing_tests", "qa", CUSTOMER_SKILLS)]
    warm_names = [s["name"] for s in warm.select_relevant_skills("write_failing_tests", "qa", CUSTOMER_SKILLS)]

    target_skills = {"test-design", "e2e-test"}

    # At least 1 of the two must appear in warm top-5
    warm_present = target_skills & set(warm_names)
    assert len(warm_present) >= 1, (
        f"Expected ≥1 of {{test-design,e2e-test}} in warm write_failing_tests top-5; "
        f"got {warm_present} in {warm_names}"
    )

    # Warm average rank must be at least as good as cold
    def _avg_pos(names: list, targets: set) -> float:
        positions = [names.index(n) for n in targets if n in names]
        return sum(positions) / len(positions) if positions else float("inf")

    cold_avg = _avg_pos(cold_names, target_skills)
    warm_avg = _avg_pos(warm_names, target_skills)
    assert warm_avg <= cold_avg, (
        f"write_failing_tests: test-design/e2e-test should rank at least as high "
        f"in warm (avg {warm_avg:.1f}) as cold (avg {cold_avg:.1f}). "
        f"cold={cold_names} warm={warm_names}"
    )


# ---------------------------------------------------------------------------
# AC-5: score_threshold makes skill count dynamic
# These tests require conductor_engine score_threshold support (engine-changes-builder).
# Marked xfail until that branch merges.
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    not _conductor_has_score_threshold(),
    reason="requires conductor score_threshold parameter (engine-changes-builder)",
    strict=False,
)
def test_ac5_score_threshold_reduces_skill_count():
    """High score_threshold returns fewer than max_skills when few skills score above it."""
    # Only 2-3 skills per step get significant brain boost; others score low
    sparse_scores = {"implement_tasks": {"api": 10, "db": 8}}
    warm = _make_conductor_with_step_scores(sparse_scores)

    result = warm.select_relevant_skills(
        "implement_tasks", "dev", CUSTOMER_SKILLS, score_threshold=5.0
    )
    assert len(result) < 5, (
        f"Expected <5 skills with score_threshold=5.0 and only 2 boosted skills; "
        f"got {len(result)}: {[s['name'] for s in result]}"
    )


@pytest.mark.xfail(
    not _conductor_has_score_threshold(),
    reason="requires conductor score_threshold parameter (engine-changes-builder)",
    strict=False,
)
def test_ac5_score_threshold_zero_returns_max_skills():
    """score_threshold=0.0 (default) returns the standard max_skills count."""
    cold = _make_conductor_no_brain()
    result = cold.select_relevant_skills(
        "implement_tasks", "dev", CUSTOMER_SKILLS, score_threshold=0.0
    )
    assert len(result) == 5, (
        f"Expected 5 skills with threshold=0.0; got {len(result)}"
    )


@pytest.mark.xfail(
    not _conductor_has_score_threshold(),
    reason="requires conductor score_threshold parameter (engine-changes-builder)",
    strict=False,
)
def test_ac5_high_threshold_still_returns_at_least_1():
    """Even an extreme threshold returns at least 1 skill (top scorer always included)."""
    warm = _make_conductor_with_step_scores(FEATURE_A_USAGE)
    result = warm.select_relevant_skills(
        "implement_tasks", "dev", CUSTOMER_SKILLS, score_threshold=9999.0
    )
    assert len(result) >= 1, "select_relevant_skills must return at least 1 skill"


# ---------------------------------------------------------------------------
# AC-7: Cold-start results unchanged (backward compat)
# ---------------------------------------------------------------------------

def test_ac7_cold_start_results_unchanged():
    """Two independent cold conductors return identical results for every step."""
    c1 = _make_conductor_no_brain()
    c2 = _make_conductor_no_brain()

    for step_id, agent in TURN_STEPS:
        names1 = [s["name"] for s in c1.select_relevant_skills(step_id, agent, CUSTOMER_SKILLS)]
        names2 = [s["name"] for s in c2.select_relevant_skills(step_id, agent, CUSTOMER_SKILLS)]
        assert names1 == names2, (
            f"Cold start must be deterministic for {step_id}: {names1} != {names2}"
        )


# ---------------------------------------------------------------------------
# Per-step isolation: scores from one step must not contaminate another
# Requires conductor to pass step_id to brain.get_skill_scores (engine-changes-builder).
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    not _conductor_passes_step_id_to_brain(),
    reason="requires conductor to pass step_id to brain.get_skill_scores (engine-changes-builder)",
    strict=False,
)
def test_per_step_scores_dont_leak_across_steps():
    """api score of 100 at implement_tasks must not boost api at write_failing_tests."""
    # Only implement_tasks has any brain data; write_failing_tests has none
    step_scores = {"implement_tasks": {"api": 100}}
    warm = _make_conductor_with_step_scores(step_scores)
    cold = _make_conductor_no_brain()

    warm_wft = [s["name"] for s in warm.select_relevant_skills("write_failing_tests", "qa", CUSTOMER_SKILLS)]
    cold_wft = [s["name"] for s in cold.select_relevant_skills("write_failing_tests", "qa", CUSTOMER_SKILLS)]

    assert "api" not in warm_wft[:3], (
        f"api should NOT appear in top-3 for write_failing_tests when only boosted "
        f"at implement_tasks; got {warm_wft}"
    )
    assert warm_wft == cold_wft, (
        f"write_failing_tests results should be identical to cold when only "
        f"implement_tasks has brain data (no leakage). "
        f"warm={warm_wft} cold={cold_wft}"
    )


# ---------------------------------------------------------------------------
# Learning trajectory monotonicity: higher usage = higher rank
# ---------------------------------------------------------------------------

def test_learning_trajectory_monotonic():
    """At implement_tasks: api(usage=5) must rank above domain(usage=2)."""
    warm = _make_conductor_with_step_scores(FEATURE_A_USAGE)
    result = warm.select_relevant_skills("implement_tasks", "dev", CUSTOMER_SKILLS)
    names = [s["name"] for s in result]

    assert "api" in names, f"api must appear in warm implement_tasks results; got {names}"
    assert "domain" in names, f"domain must appear in warm implement_tasks results; got {names}"
    api_pos = names.index("api")
    domain_pos = names.index("domain")
    assert api_pos < domain_pos, (
        f"api (usage=5) must rank above domain (usage=2) at implement_tasks; "
        f"api pos={api_pos}, domain pos={domain_pos}, full={names}"
    )
