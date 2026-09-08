"""The plan scorer measures a sequence diagram - task 98ef9d3d.

Two false measurements held root task 1bcb2b24 at design-packet certainty
0.62 and escalated a correct plan to a human.

  AC-1  mermaid_edges reads sequence-diagram arrows (->>, -->>, -x, -)).
  AC-2  mermaid_edges still reads the flowchart arrows it always read.
  AC-3  mermaid_edges invents no edge from ordinary prose.
  AC-4  a sequence-diagram plan scores diagram_quality 1.0, not 0.5.
  AC-5  _claimed_files ignores a git revision range (origin/main...HEAD).
  AC-6  _claimed_files ignores a path the plan cites as evidence.
  AC-7  _claimed_files still reports a path the plan says it changes.
  AC-8  the real 1bcb2b24 packet shape scores 1.00, above the threshold.
"""
from __future__ import annotations

import uuid

import pytest

from prism_service.services.arc_governance import mermaid_edges, mermaid_parses
from prism_service.services import design_packet as dp


# The diagram task 1bcb2b24's plan actually carries, trimmed to the shape
# that matters: participants, sequence messages, and a Note with markup.
SEQUENCE_DIAGRAM = """sequenceDiagram
    participant TR as task_runner seat
    participant CS as ClaimService
    participant SW as ship_worker seat

    TR->>CS: acquire(task, holder=prism-task-runner)
    CS-->>TR: claim_id
    Note over CS: The claim id matches the live claim.<br/>It takes no lease.
    SW->>CS: acquire(task, holder=conductor-shipper)
    CS--xSW: refused
    TR-)CS: release(claim_id)
"""


class _Task:
    """Minimal task stand-in carrying only the fields the scorer reads."""

    def __init__(self, id="t1", oracle="", likely_misfire="", plan_doc="",
                 plan_diagram="", tags=None, allowed_files=None,
                 stop_if=None, verify=None, workflow_step="plan_gate",
                 gate_state="pending", parent_id="", workflow="implement"):
        self.id = id
        self.oracle = oracle
        self.likely_misfire = likely_misfire
        self.plan_doc = plan_doc
        self.plan_diagram = plan_diagram
        self.tags = tags or []
        self.allowed_files = allowed_files or []
        self.stop_if = stop_if or []
        self.verify = verify or []
        self.workflow_step = workflow_step
        self.gate_state = gate_state
        self.parent_id = parent_id
        self.workflow = workflow


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    return "ps-" + uuid.uuid4().hex[:8]


def _edge_pairs(source):
    return {(e["from"], e["to"]) for e in mermaid_edges(source)}


def test_a_sequence_diagram_carries_edges():
    """AC-1: the arrows a sequenceDiagram uses are edges. Before this slice
    _EDGE_RE accepted `->` and `-->` only, so `->>` and `-->>` matched
    nothing and an 18-message diagram counted zero edges."""
    assert mermaid_parses(SEQUENCE_DIAGRAM) is True
    pairs = _edge_pairs(SEQUENCE_DIAGRAM)
    assert ("TR", "CS") in pairs, "solid arrow ->> is an edge"
    assert ("CS", "TR") in pairs, "dashed reply -->> is an edge"
    assert ("CS", "SW") in pairs, "a cross end --x is an edge"
    assert ("TR", "CS") in pairs, "an open end -) is an edge"
    assert len(pairs) >= 2


def test_the_flowchart_arrows_still_read():
    """AC-2: the shapes the counter always read keep working."""
    flow = ("flowchart TD\n"
            "    api --> services\n"
            "    services -.-> store\n"
            "    store ==> disk\n"
            "    cli-->api\n")
    pairs = _edge_pairs(flow)
    assert ("api", "services") in pairs
    assert ("services", "store") in pairs
    assert ("store", "disk") in pairs
    assert ("cli", "api") in pairs


def test_prose_is_not_an_edge():
    """AC-3 (the likely_misfire guard): widening the arrow set must not
    make ordinary plan prose look like a graph. A phantom edge would feed
    compute_violations and invent an architecture violation."""
    prose = ("flowchart TD\n"
             "    a --> b\n"
             "    The lease is held for 3600 s - a dead holder never wedges\n"
             "    it. See task_runner.py line 1178 and the note x-y below.\n")
    assert _edge_pairs(prose) == {("a", "b")}


# A plan that cites evidence the way the plan rubric asks for, and names
# exactly one file it changes. Padded past the 150-word completeness floor.
_EVIDENCE_PLAN = (
    "## Summary\n\n"
    "A search for `ClaimService` across `prism_service/` returns only "
    "`models/claim.py`, `services/dispatch.py` and "
    "`services/resume_actuator.py`. The MCP handler "
    "(`prism_service/mcp/tools.py:5208`) calls the flow module, so the "
    "lease belongs there and the verb inherits it.\n\n"
    "## Design\n\n"
    "This slice edits "
    "`services/prism-service/prism_service/services/claim_service.py` and "
    "adds a same-holder renew. The caller that already holds the live "
    "lease extends it. A stranger still receives None. The clamp keeps "
    "every lease under the maximum, so a dead holder never wedges the "
    "task for ever. " + ("The change is small and it is measured. " * 24) +
    "\n\n## Acceptance Criteria\n\n"
    "- AC-1: the same holder renews instead of losing the lease.\n"
    "  - oracle: `git diff --name-only origin/main...HEAD` prints no "
    "policy file.\n")

_ALLOWED = ["services/prism-service/prism_service/services/claim_service.py"]


def test_a_git_revision_range_is_not_a_file():
    """AC-5: `origin/main...HEAD` is a revision range. Reading it as a
    repo file zeroed scope_alignment on task 1bcb2b24."""
    claimed = dp._claimed_files(_EVIDENCE_PLAN)
    assert "origin/main...HEAD" not in claimed
    assert not any(".." in c for c in claimed)


def test_a_cited_path_is_not_a_scope_claim():
    """AC-6: the plan rubric rewards source research. A path the plan
    cites as evidence must not read as a claim to edit that path."""
    claimed = dp._claimed_files(_EVIDENCE_PLAN)
    for cited in ("models/claim.py", "services/dispatch.py",
                  "services/resume_actuator.py",
                  "prism_service/mcp/tools.py"):
        assert cited not in claimed, f"{cited} is evidence, not scope"


def test_a_stated_change_is_still_a_scope_claim():
    """AC-7 (the second likely_misfire guard): the tooth must keep its
    teeth. A plan that says it edits a path still reports that path."""
    claimed = dp._claimed_files(_EVIDENCE_PLAN)
    assert _ALLOWED[0] in claimed

    out_of_contract = (
        "## Design\n\nThis slice also modifies "
        "`services/prism-service/prism_service/services/"
        "conductor_service.py` to reach the seat.\n")
    assert ("services/prism-service/prism_service/services/"
            "conductor_service.py") in dp._claimed_files(out_of_contract)


def test_a_sequence_diagram_packet_clears_the_threshold(project):
    """AC-4 and AC-8: the shape task 1bcb2b24 actually carried. Before
    this slice it scored 0.62 - diagram_quality 0.5 because a sequence
    diagram counted no edge, and scope_alignment 0.0 because five cited
    paths and one revision range read as scope claims. No score above
    0.875 was reachable for any sequence-diagram plan, so the 0.90
    threshold refused every one of them."""
    task = _Task(
        id="1bcb2b24",
        plan_doc=_EVIDENCE_PLAN,
        plan_diagram=SEQUENCE_DIAGRAM,
        allowed_files=_ALLOWED,
        oracle=("Run `python -m pytest services/prism-service/tests/unit/"
                "test_one_driver_per_task_worktree.py -q`. It passes."),
        likely_misfire=("The lease never expires, so one crashed driver "
                        "blocks the task for ever. See `claim_service.py`."),
    )
    got = dp.plan_gate_certainty(project, task.id, task)
    assert got["signals"]["diagram_quality"] == 1.0, got["reasons"]
    assert got["signals"]["scope_alignment"] == 1.0, got["reasons"]
    assert got["score"] >= dp.certainty_threshold(), got
    assert got["reasons"] == []
