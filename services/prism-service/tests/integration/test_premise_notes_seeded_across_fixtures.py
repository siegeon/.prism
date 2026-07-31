"""premise_grounded (task 3928b7ac / issue #222 continued) now binds
UNCONDITIONALLY on review_previous_notes via task.premise_notes
(prism_service/services/arc_governance.py:297-306). 15 tests/integration
files drive a fresh task through the SDLC for reasons unrelated to premise
content and never seeded premise_notes, so each one now stalls at
review_previous_notes ("gate=none", refused) instead of reaching its own
assertions. tests/unit already applied the fix (commit 50559d3e: seed a
compliant '## Premises\n- ... - UNVERIFIED' value right after each task is
created); this test is the regression guard that the same seed lands across
every equivalent tests/integration fixture.

AC-1  every one of the 15 listed files passes on its own (subprocess, real
      pytest, no mocking of the gate) once each is seeded - RED until the
      fixture edits land, GREEN and a standing guard afterward.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent

_STALLED_FIXTURE_FILES = [
    "tests/integration/test_conductor_lean_responses.py",
    "tests/integration/test_conductor_role_attribution.py",
    "tests/integration/test_conductor_work_honest_green.py",
    "tests/integration/test_conductor_work_terminal.py",
    "tests/integration/test_control_plane_pinned.py",
    "tests/integration/test_gate_decide_idempotent_rewind.py",
    "tests/integration/test_gate_evidence_wiring.py",
    "tests/integration/test_gate_role_enforcement.py",
    "tests/integration/test_green_gate_oracle_tooth.py",
    "tests/integration/test_green_gate_ui_artifact.py",
    "tests/integration/test_oracle_evidence_receipt.py",
    "tests/integration/test_proof_type_gates.py",
    "tests/integration/test_queue_transition_contract.py",
    "tests/integration/test_ui_first_conductor_gate_admin_surfaces.py",
    "tests/integration/test_worker_contract_enforced.py",
]


def test_no_fixture_stalls_at_review_previous_notes_for_premise_reasons():
    """AC-1: run the 15 previously-stalled files as a real subprocess pytest
    (no gate/rubric mocking) and require 0 failures. A returncode != 0 here
    means at least one fixture still never seeded task.premise_notes before
    driving past review_previous_notes."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *_STALLED_FIXTURE_FILES],
        cwd=str(_SERVICE_ROOT), capture_output=True, text=True, timeout=300)
    trace = result.stdout[-6000:] + "\n" + result.stderr[-2000:]
    assert result.returncode == 0, (
        "one or more of the 15 fixture files still stalls at "
        "review_previous_notes (premise_grounded refused an unseeded "
        "task.premise_notes) - trace:\n" + trace)
