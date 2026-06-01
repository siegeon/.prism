"""Anti-busywork audit (goalbuddy Judge doctrine) — green_gate proof note.

Pins green_gate_proof_note(): "lots of files is not completion." A green_gate
close with code churn but no completion_proof is BUSYWORK RISK; with no churn
and no proof it's an ORACLE gap; with a real proof it's clean. Advisory only.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_churn_without_proof_is_busywork():
    from prism_service.services.conductor_service import green_gate_proof_note
    note = green_gate_proof_note(7, "")
    assert "busywork risk" in note
    assert "7 file-change" in note


def test_no_churn_no_proof_is_oracle_gap():
    from prism_service.services.conductor_service import green_gate_proof_note
    note = green_gate_proof_note(0, "tbd")
    assert "oracle: no completion_proof" in note
    assert "busywork" not in note


def test_real_proof_is_clean_regardless_of_churn():
    from prism_service.services.conductor_service import green_gate_proof_note
    assert green_gate_proof_note(42, "532 tests pass + demo recorded") == ""
    assert green_gate_proof_note(0, "demo at /x.mp4") == ""


def test_placeholder_proof_still_flagged_as_busywork_when_churned():
    from prism_service.services.conductor_service import green_gate_proof_note
    note = green_gate_proof_note(3, "<fill in evidence>")
    assert "busywork risk" in note
