"""Adapter registry survives ANY test order (task c23e2e7b).

THE LEAK: running tests/integration/test_external_work_sync.py then
tests/integration/test_pull_issues_into_tasks.py in ONE pytest process left
api.integrations._adapters["github"] cleared for the second file. The
`team` fixture in test_external_work_sync.py calls reset_adapters() at BOTH
setup and teardown to get a clean slate for its own scripted adapters, but
the teardown call permanently emptied the registry instead of putting back
whatever was there before the fixture ran (the real production github
adapter registered at import time by register_builtin_adapters()). Any later
test in the same process that expects the production adapter
(test_pull_issues_into_tasks.py:95-97) inherited the emptied registry.

Latent, not broken today: the default `tests/unit` collection order never
puts these two files adjacent, so it detonates only when ordering changes -
a new file landing between them, -k, a sharded/random run, or someone
running the two files directly to debug.

This file pins the MECHANISM (the registry is restored after any test that
mutates it), not just the SYMPTOM (the two named suites happen to pass in
some order today) - a fix that only reorders/renames files, or only makes
the reported pair pass, would leave the underlying global mutable and let a
third suite (test_github_zero_setup_connect.py also reaches into
_adapters) reopen the exact same hole unnoticed.
"""

from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_EXTERNAL = "tests/integration/test_external_work_sync.py"
_PULL = "tests/integration/test_pull_issues_into_tasks.py"
_ZERO_SETUP = "tests/integration/test_github_zero_setup_connect.py"


def _run_together(*rel_paths: str) -> subprocess.CompletedProcess:
    """Run the given test files in ONE pytest process, from the service
    root, exactly the way a sharded/filtered/reordered CI run would."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *rel_paths],
        cwd=str(_SERVICE_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
    )


def _assert_clean_pass(result: subprocess.CompletedProcess, label: str) -> None:
    tail = result.stdout[-4000:] if result.stdout else ""
    assert result.returncode == 0, (
        f"{label} must pass in one process regardless of order; got rc="
        f"{result.returncode}\nstdout:\n{tail}\nstderr:\n{result.stderr[-2000:]}")
    assert " failed" not in tail and " error" not in tail.lower(), (
        f"{label} exited 0 but reported failures/errors:\n{tail}")


# ── AC-1 / AC-2: the reported pair, both orders ─────────────────────────

def test_external_then_pull_leaves_no_leak():
    """THE reported order. Before the fix: rc=1, 7 errors -
    'AssertionError: production must register a github adapter' - because
    test_external_work_sync.py's `team` fixture teardown clears the
    registry and never restores it."""
    _assert_clean_pass(_run_together(_EXTERNAL, _PULL), "external -> pull")


def test_pull_then_external_leaves_no_leak():
    """The reverse order. Passes even before the fix today (the second file
    happens not to touch the registry) - kept as a regression pin so a
    future change to either fixture cannot quietly break this direction
    while "fixing" the other."""
    _assert_clean_pass(_run_together(_PULL, _EXTERNAL), "pull -> external")


# ── AC-3: a third registry-touching suite, randomized order ────────────

def test_a_random_order_of_three_registry_suites_all_pass():
    """Recorded second misfire: fixing only the reported pair could leave a
    third suite that also reaches into _adapters
    (test_github_zero_setup_connect.py) able to reintroduce the same leak
    unnoticed. Fixed-seed shuffle so the run is deterministic across CI
    machines while still exercising an order nobody hand-picked."""
    files = [_EXTERNAL, _PULL, _ZERO_SETUP]
    order = list(files)
    random.Random(20260730).shuffle(order)
    _assert_clean_pass(_run_together(*order), f"randomized order {order}")


# ── AC-4: pin the mechanism itself, not just these three files ──────────

class _FakeAdapter:
    provider = "fake-c23e2e7b"


def test_registry_mutation_is_restored_after_this_test():
    """Mutate the registry the way a scripted suite does. The NEXT test in
    this file (below) checks that the mutation left no trace - proving the
    autouse restore fixture did the work, not incidental file ordering."""
    from prism_service.api import integrations

    integrations.reset_adapters()
    assert integrations._adapters == {}
    integrations.register_adapter(_FakeAdapter())
    assert "fake-c23e2e7b" in integrations._adapters


def test_previous_tests_mutation_did_not_leak_into_this_test():
    """If this fails, the registry restore is not actually wired up (or was
    reordered/renamed away) even though the file-level tests above pass -
    the exact 'fixed the symptom, not the mechanism' misfire this task
    warns against."""
    from prism_service.api import integrations

    assert "fake-c23e2e7b" not in integrations._adapters, (
        "the previous test's reset_adapters()/register_adapter() call "
        "leaked into this test; the registry must be restored to its "
        "pre-test state after every test, not just after the two suites "
        "named in this task")
