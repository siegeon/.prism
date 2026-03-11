"""
conftest.py — per-phase skill/brain diagnostic report for customer skill lifecycle tests.

Uses pytest_terminal_summary to print a table showing skill matching metrics,
brain ranking results, and coverage gaps per workflow step after the test session.
"""
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
FIXTURES_DIR = Path(__file__).resolve().parent / "harness" / "fixtures"

# Workflow steps in display order: (step_id, agent, role_label)
_ALL_STEPS = [
    ("write_failing_tests",   "qa",  "QA"),
    ("implement_tasks",       "dev", "DEV"),
    ("draft_story",           "sm",  "SM"),
    ("verify_plan",           "sm",  "SM"),
    ("verify_green_state",    "qa",  "QA"),
    ("review_previous_notes", "sm",  "SM"),
    ("red_gate",              None,  None),
    ("green_gate",            None,  None),
]

# AC prefix → step_id (None = not tied to a single workflow step)
_AC_STEP_MAP = {
    "ac1":  "write_failing_tests",
    "ac2":  "implement_tasks",
    "ac3":  "draft_story",
    "ac4":  "verify_green_state",
    "ac5":  "review_previous_notes",
    "ac6":  "implement_tasks",
    "ac7":  "write_failing_tests",
    "ac8":  None,
    "ac9":  "review_previous_notes",
    "ac10": None,
    "ac11": "write_failing_tests",
    "ac12": None,   # gate steps — handled separately
    "ac13": None,
    "ac14": None,
    "ac15": None,
    "ac16": None,
    "ac17": None,
    "ac18": None,
    "ac19": None,
    "ac20": None,
}

_SKILL_CATEGORIES = {
    "BUILD":   {"api", "db", "domain", "query", "patterns", "scaffold", "migrate", "refactor", "inject"},
    "SHIP":    {"ci", "docs", "handoff"},
    "OPERATE": {"monitor", "alert", "rollback", "audit"},
    "Top":     {"task", "build", "verify", "ship", "operate", "test", "review", "checkin"},
}


def _load_skills():
    fixture = FIXTURES_DIR / "customer-skills.jsonl"
    skills = []
    with open(fixture, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                skills.append(json.loads(line))
    return skills


def _make_conductor_cold():
    sys.path.insert(0, str(HOOKS_DIR))
    from conductor_engine import Conductor
    c = object.__new__(Conductor)
    c._brain = None
    c._brain_available = False
    c.last_had_brain_context = 0
    c.last_prompt_id = ""
    return c


def _make_conductor_brain(usage_scores):
    sys.path.insert(0, str(HOOKS_DIR))
    from conductor_engine import Conductor
    c = object.__new__(Conductor)
    mock_brain = MagicMock()
    mock_brain.get_skill_scores.return_value = usage_scores
    c._brain = mock_brain
    c._brain_available = True
    c.last_had_brain_context = 0
    c.last_prompt_id = ""
    return c


def _count_tests_per_step(terminalreporter):
    """Extract test counts per step from reporter stats."""
    seen = set()
    counts = {s[0]: 0 for s in _ALL_STEPS}

    for reports in terminalreporter.stats.values():
        for rep in reports:
            if not hasattr(rep, "nodeid"):
                continue
            if "test_customer_skill_lifecycle" not in rep.nodeid:
                continue
            nid = rep.nodeid
            if nid in seen:
                continue
            seen.add(nid)

            m = re.search(r"\btest_(ac\d+)_", nid)
            if not m:
                continue
            ac = m.group(1)

            if ac == "ac12":
                if "red_gate" in nid:
                    counts["red_gate"] += 1
                else:
                    counts["green_gate"] += 1
            else:
                step = _AC_STEP_MAP.get(ac)
                if step and step in counts:
                    counts[step] += 1

    return counts


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Print per-phase skill/brain metrics table after the test session."""
    # Skip if no customer skill lifecycle tests were run
    lifecycle_seen = any(
        hasattr(rep, "nodeid") and "test_customer_skill_lifecycle" in rep.nodeid
        for reports in terminalreporter.stats.values()
        for rep in reports
    )
    if not lifecycle_seen:
        return

    try:
        skills = _load_skills()
        cold = _make_conductor_cold()
        usage = {s["name"]: 1 for s in skills}
        brain = _make_conductor_brain(usage)
        from conductor_engine import _STEP_SKILL_KEYWORDS
    except Exception:
        return

    n = len(skills)
    step_counts = _count_tests_per_step(terminalreporter)

    # Build per-step metric rows
    rows = []
    for step_id, agent, role in _ALL_STEPS:
        is_gate = step_id in ("red_gate", "green_gate")
        test_count = step_counts.get(step_id, 0)

        if is_gate:
            rows.append(dict(
                step_id=step_id, role=None, tests=test_count,
                cold_n=0, brain_n=0, gaps="-", coverage="(blocked)", is_gate=True,
            ))
            continue

        cold_result = cold.select_relevant_skills(step_id, agent, skills)
        brain_result = brain.select_relevant_skills(step_id, agent, skills)
        cold_n = len(cold_result)
        brain_n = len(brain_result)
        cold_names = [s["name"] for s in cold_result]

        keywords = {kw.lower() for kw in _STEP_SKILL_KEYWORDS.get(step_id, [])}
        gaps = sum(
            1 for s in skills
            if keywords and not any(
                kw in (s.get("name") or "").lower() or kw in (s.get("description") or "").lower()
                for kw in keywords
            )
        )

        rows.append(dict(
            step_id=step_id, role=role, tests=test_count,
            cold_n=cold_n, brain_n=brain_n, gaps=gaps,
            coverage=", ".join(cold_names), is_gate=False,
        ))

    # Global coverage gap analysis
    gap_names = []
    for s in skills:
        nl, dl = (s.get("name") or "").lower(), (s.get("description") or "").lower()
        matched = any(
            any(kw.lower() in nl or kw.lower() in dl for kw in kws)
            for sid, kws in _STEP_SKILL_KEYWORDS.items()
            if sid not in ("red_gate", "green_gate")
        )
        if not matched:
            gap_names.append(s["name"])

    covered_n = n - len(gap_names)
    total_tests = sum(r["tests"] for r in rows)
    SEP = "━" * 88

    tw = terminalreporter
    tw.write_sep("=", "Customer Skill Lifecycle Report")
    tw.write_line(SEP)
    tw.write_line(f"  {'Phase':<30} {'Tests':>6}  {'Skills Matched':>16}  {'Brain Ranked':>14}  {'Gaps':>5}  Coverage")
    tw.write_line(SEP)

    for r in rows:
        label = f"{r['step_id']} ({r['role']})" if r["role"] else r["step_id"]
        if r["is_gate"]:
            cold_s = f"0/{n} (0%)"
            brain_s = f"0/{n} (0%)"
            tw.write_line(f"  {label:<30} {r['tests']:>6}  {cold_s:>16}  {brain_s:>14}  {'  -':>5}  (blocked)")
        else:
            cold_pct = int(r["cold_n"] / n * 100) if n else 0
            brain_pct = int(r["brain_n"] / n * 100) if n else 0
            cold_s = f"{r['cold_n']}/{n} ({cold_pct}%)"
            brain_s = f"{r['brain_n']}/{n} ({brain_pct}%)"
            tw.write_line(f"  {label:<30} {r['tests']:>6}  {cold_s:>16}  {brain_s:>14}  {r['gaps']:>5}  {r['coverage']}")

    tw.write_line(SEP)
    cov_pct = int(covered_n / n * 100) if n else 0
    gap_pct = int(len(gap_names) / n * 100) if n else 0
    tw.write_line(
        f"  {'TOTALS':<30} {total_tests:>6}   "
        f"skills covered: {covered_n}/{n} ({cov_pct}%)  "
        f"gaps: {len(gap_names)}/{n} ({gap_pct}%)"
    )
    tw.write_line("")
    tw.write_line("  Coverage Gaps (skills with NO keyword match in any step):")
    for cat, cat_set in _SKILL_CATEGORIES.items():
        cat_gaps = sorted(g for g in gap_names if g in cat_set)
        if cat_gaps:
            tw.write_line(f"    {cat + ':':9} {', '.join(cat_gaps)}")
    tw.write_line("")
    tw.write_line("  Brain Override Summary:")
    tw.write_line("    With usage data: top-5 by frequency (ignores keywords)")
    tw.write_line("    Without data:    falls back to cold-start keyword matching")
    tw.write_line("    Tracking:        skill_calls (s), brain_queries (bq), tool_calls (tc) in step_history")
    tw.write_line("")
