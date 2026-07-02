"""In-daemon Tier0 must exclude ``daemon_exclusive`` tests (task 91ac81e8).

VerifierService runs the full pytest suite as a subprocess of the LIVE
daemon at green_gate. A handful of process-lifecycle / pidfile /
learning-loop tests contend with the running daemon's pid/process space
and flake ONLY in-daemon (they pass standalone), so a genuinely 0-failed
tree could never clear green_gate mechanically — every green needed a
distinct-actor override, the exact gate-theater the reinforcement
doctrine targets.

Fix: mark the contending tests ``@pytest.mark.daemon_exclusive`` and have
the in-daemon Tier0 pytest run add ``-m "not daemon_exclusive"``. The
marked tests STILL run standalone / in CI (no ``-m`` filter there); the
verifier is NOT weakened — a real failure in a NON-excluded test still
fails Tier0.

Contract encoded here:
  * AC-1 — a FAILING daemon_exclusive test is DESELECTED by the in-daemon
    Tier0 run, so a green tree stays green (tooling.pytest claim=pass and
    the excluded nodeid never appears in the run output).
  * AC-2 — a FAILING non-excluded test still fails Tier0 with exit 1 (the
    ``-m`` filter must not swallow real regressions).
  * AC-3 — the 5 named process-lifecycle tests carry the marker so the
    exclusion actually targets them (guards against silent un-marking).
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.verifier_service import run_tier0   # noqa: E402


# ----------------------------------------------------------------------
# Temp-repo builders — a git repo whose tests land in Tier0's diff scope
# ----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def _init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "README.md")
    _git(ws, "commit", "-m", "seed")


def _write_tree(root: Path, *, daemon_exclusive_failing: bool = False,
                plain_failing: bool = False) -> None:
    """A minimal python project: pyproject + a passing normal test, plus
    optionally a FAILING daemon_exclusive test and/or a FAILING plain
    test. The passing test guarantees the suite is non-empty so a fully
    deselected run never collapses to pytest's exit-5 (no tests ran)."""
    # Register the marker exactly as the real suite does (root pyproject),
    # so the isolated run emits no unknown-mark warning — otherwise the
    # warnings summary would print the deselected test's path and defeat the
    # "was it deselected?" assertion below.
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'svc'\nversion = '0'\n\n"
        "[tool.pytest.ini_options]\n"
        "markers = [\n"
        "    \"daemon_exclusive: contends with the live daemon; excluded "
        "in-daemon\",\n"
        "]\n",
        encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_normal_pass.py").write_text(
        "def test_normal_pass():\n    assert True\n", encoding="utf-8")
    if daemon_exclusive_failing:
        (tests / "test_daemon_only.py").write_text(
            "import pytest\n\n\n"
            "@pytest.mark.daemon_exclusive\n"
            "def test_daemon_only_failure():\n"
            "    assert False, 'contends with the live daemon'\n",
            encoding="utf-8")
    if plain_failing:
        (tests / "test_plain_failure.py").write_text(
            "def test_plain_failure():\n"
            "    assert False, 'a real regression'\n", encoding="utf-8")


def _pytest_claim(claims):
    hits = [c for c in claims if c.kind == "tooling.pytest"]
    assert hits, ("Tier0 emitted no tooling.pytest claim — the full-suite "
                  "run never fired")
    return hits[0]


# ----------------------------------------------------------------------
# AC-1 — daemon_exclusive failure is deselected in-daemon
# ----------------------------------------------------------------------


def test_daemon_exclusive_failure_is_excluded_from_tier0(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    _write_tree(ws, daemon_exclusive_failing=True)

    claim = _pytest_claim(run_tier0(ws))
    assert claim.status == "pass", (
        "a FAILING daemon_exclusive test was NOT deselected by the "
        f"in-daemon Tier0 run: {claim.feedback} / "
        f"{claim.evidence.get('stdout', '')[:500]}")
    # Proof it was DESELECTED, not silently collected+passed: a -q run
    # prints a failing test's nodeid; a deselected one never appears.
    assert "test_daemon_only" not in claim.evidence.get("stdout", ""), (
        "the daemon_exclusive test ran in-daemon; it must be deselected")


# ----------------------------------------------------------------------
# AC-2 — verifier NOT weakened: a non-excluded failure still fails
# ----------------------------------------------------------------------


def test_non_excluded_failure_still_fails_tier0(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    _write_tree(ws, plain_failing=True)

    claim = _pytest_claim(run_tier0(ws))
    assert claim.status == "fail", (
        "a real failing (non-excluded) test must still fail Tier0 — the "
        f"-m filter swallowed a regression (got {claim.status})")
    assert claim.evidence.get("exit_code") == 1, (
        f"expected a real test failure (exit 1), got "
        f"exit={claim.evidence.get('exit_code')}")
    assert "test_plain_failure" in claim.evidence.get("stdout", ""), (
        "the planted non-excluded failure never ran")


# ----------------------------------------------------------------------
# AC-3 — the named process-lifecycle tests carry the marker
# ----------------------------------------------------------------------

_MARKED = [
    ("tests.unit.test_pidfile_lifecycle",
     "test_cmd_status_reports_unhealthy_and_self_heals_split_brain"),
    ("tests.unit.test_pidfile_lifecycle",
     "test_cmd_stop_reaps_split_brain_orphan"),
    ("tests.integration.test_event_handlers_phase3",
     "test_session_imported_burst_coalesces_to_one_pass"),
    ("tests.integration.test_event_handlers_phase3",
     "test_session_imported_reinforces_instead_of_duplicating"),
    ("tests.integration.test_learning_loop_self_sustaining",
     "test_completed_reflection_writes_rollup_and_feeds_outcome"),
]


def test_named_process_lifecycle_tests_are_marked_daemon_exclusive():
    for mod_name, fn_name in _MARKED:
        mod = importlib.import_module(mod_name)
        fn = getattr(mod, fn_name)
        marks = {m.name for m in getattr(fn, "pytestmark", [])}
        assert "daemon_exclusive" in marks, (
            f"{mod_name}::{fn_name} lost its @pytest.mark.daemon_exclusive "
            "marker — the in-daemon exclusion no longer targets it")
