"""CI must run every test that gates a release (task 7af6ab28, GH #430).

`prism selfcheck` refuses to mark a release PRISM-WORKABLE unless the six
files in `prism_cli.WORKABILITY_TESTS` pass. PR #235 went green through CI
and left 49 integration tests red on main because CI ran only tests/unit.
This guard parses `.github/workflows/pr-checks.yml` and asserts every
workability file is (a) collected by a pytest step, (b) not --ignore'd or
--deselect'ed, and (c) run by a step that is NOT `continue-on-error`.

The required set is DERIVED from `prism_cli.WORKABILITY_TESTS` (AC-2) so a
future edit to that constant is covered automatically; nothing here
hardcodes the six filenames.

Trace: AC-1 test exists / AC-2 derived set / AC-3 real workflow passes /
AC-4 --deselect is detected / AC-5 continue-on-error is detected /
AC-6 test_selfcheck_cli.py untouched (asserted by that suite itself).
"""
from __future__ import annotations

import copy
from pathlib import Path

import yaml

from prism_service.cli import prism_cli

REPO_ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pr-checks.yml"


def _load_workflow(path: Path = WORKFLOW) -> dict:
    assert path.is_file(), f"workflow not found at {path}"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _pytest_steps(workflow: dict) -> dict[str, dict]:
    """Map 'unit'/'integration' -> the step whose `run` invokes pytest on
    that tree. Matched on the `run` text, never the human `name`, so a step
    rename cannot fool the guard."""
    found: dict[str, dict] = {}
    for job in (workflow.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = " ".join(str(step.get("run", "")).split())
            for tree in ("unit", "integration"):
                if f"pytest tests/{tree}" in run:
                    found[tree] = step
    return found


def _uncovered_workability(workflow: dict) -> list[str]:
    """One reason string per WORKABILITY_TESTS entry CI would not gate on.
    Returns [] when every workability file is run, un-excluded, by a step
    that fails the job when it fails."""
    steps = _pytest_steps(workflow)
    uncovered: list[str] = []
    for entry in prism_cli.WORKABILITY_TESTS:
        tree = entry.split("/", 1)[0]
        rel = f"tests/{entry}"
        step = steps.get(tree)
        if step is None:
            uncovered.append(f"{entry}: no pytest step runs tests/{tree}")
            continue
        if step.get("continue-on-error"):
            uncovered.append(f"{entry}: its step is continue-on-error, so a "
                             "failure never fails the job")
            continue
        tokens = " ".join(str(step.get("run", "")).split()).replace('"', "").split()
        excluded = []
        for i, tok in enumerate(tokens):
            if tok.startswith(("--ignore=", "--deselect=")):
                excluded.append(tok.split("=", 1)[1])
            elif tok in ("--ignore", "--deselect") and i + 1 < len(tokens):
                excluded.append(tokens[i + 1])
        hits = [x for x in excluded if rel in x]
        if hits:
            uncovered.append(f"{entry}: excluded by {' '.join(hits)}")
    return uncovered


def test_ci_runs_every_workability_test():
    """AC-1 / AC-2 / AC-3: the real PR workflow gates on all six files."""
    uncovered = _uncovered_workability(_load_workflow())
    assert uncovered == [], (
        "CI would go green while `prism selfcheck` refuses the release; "
        f"uncovered workability tests: {uncovered}"
    )


def _mutated_copy(tmp_path: Path, mutate) -> dict:
    """Write a mutated copy of the REAL workflow to tmp_path and re-load it
    through the same load path. The real file is never written."""
    doc = copy.deepcopy(_load_workflow())
    mutate(_pytest_steps(doc)["integration"])
    scratch = tmp_path / "pr-checks.yml"
    scratch.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return _load_workflow(scratch)


def test_a_deselected_workability_file_is_reported(tmp_path):
    """AC-4: --deselect'ing a workability file must be caught."""
    target = "services/prism-service/tests/integration/test_gate_adjudicator_seat.py"

    def add_deselect(step: dict) -> None:
        step["run"] = f"{step['run']} --deselect {target}"

    uncovered = _uncovered_workability(_mutated_copy(tmp_path, add_deselect))
    assert any("test_gate_adjudicator_seat.py" in u for u in uncovered), uncovered
    assert "tests/integration/test_gate_adjudicator_seat.py" in " ".join(
        prism_cli.WORKABILITY_TESTS
    ).replace("integration/", "tests/integration/")


def test_a_continue_on_error_step_counts_as_not_run(tmp_path):
    """AC-5: a non-gating integration step is the same blindness with logs."""

    def soften(step: dict) -> None:
        step["continue-on-error"] = True

    uncovered = _uncovered_workability(_mutated_copy(tmp_path, soften))
    expected = [t for t in prism_cli.WORKABILITY_TESTS if t.startswith("integration/")]
    assert expected, "WORKABILITY_TESTS lost its integration entries"
    for entry in expected:
        assert any(entry in u for u in uncovered), (entry, uncovered)


def test_the_real_workflow_is_never_written(tmp_path):
    """AC-4/AC-5 mutate scratch copies only: the tracked file is untouched."""
    before = WORKFLOW.read_bytes()
    _mutated_copy(tmp_path, lambda step: step.__setitem__("continue-on-error", True))
    assert WORKFLOW.read_bytes() == before
