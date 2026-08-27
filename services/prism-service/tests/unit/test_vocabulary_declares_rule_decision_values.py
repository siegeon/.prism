"""Task afb47c33: the vocabulary declares the values rule decisions write.

Red until models/signal.py SIGNAL_STATES holds resolved and promoted,
models/task.py CHANNELS holds ontology and PROOF_TYPES holds pr, and
rule_decisions.py no longer says those values sit outside the tuples.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from prism_service.models.signal import SIGNAL_STATES, Signal
from prism_service.models.task import (
    CHANNELS, PROOF_TYPES, validate_channel, validate_proof_type,
)
from prism_service.project_context import get_project
from prism_service.services import ontology_terms
from prism_service.services.signal_store import SignalStore

SRC = Path(__file__).resolve().parents[2] / "prism_service" / "services" / "rule_decisions.py"


@pytest.fixture
def project():
    return f"vocab-test-{uuid.uuid4().hex[:8]}"


def test_signal_states_declare_resolved_and_promoted():  # AC-1
    assert SIGNAL_STATES == ("open", "became_task", "dropped", "resolved", "promoted")


def test_channels_declare_ontology():  # AC-2
    assert "ontology" in CHANNELS
    assert validate_channel("ontology") == "ontology"


def test_proof_types_declare_pr():  # AC-3
    assert "pr" in PROOF_TYPES
    assert validate_proof_type("pr") == "pr"


def test_terms_holds_nothing_back_for_rule_decision_values(project):  # AC-4, AC-7
    store = SignalStore(project)
    try:
        for state in ("open", "became_task", "dropped", "resolved", "promoted"):
            store.create(Signal(project=project, channel="mcp", subject=state, state=state))
        store.create(Signal(project=project, channel="ontology", subject="rule fired"))
        store.create(Signal(project=project, channel="mcp", subject="bogus", state="bogus"))
    finally:
        store.close()
    get_project(project).task_svc.create(title="ship a pull request", proof_type="pr")

    out = ontology_terms.terms(project)
    assert out["held_back"] == [
        {"vocabulary": "signal_state", "value": "bogus", "count": 1}
    ]
    by_name = {v["name"]: {t["value"]: t for t in v["terms"]} for v in out["vocabularies"]}
    assert by_name["channel"]["ontology"]["in_use"] is True
    assert by_name["signal_state"]["resolved"]["in_use"] is True
    assert by_name["signal_state"]["promoted"]["in_use"] is True
    assert by_name["proof_type"]["pr"]["in_use"] is True


def test_rule_decisions_no_longer_disclaims_the_tuples():  # AC-6
    text = SRC.read_text(encoding="utf-8")
    assert "out of allowed_files" not in text
    assert "out of this task's allowed_files" not in text
