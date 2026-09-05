"""The stall splitter reads the CODIFIED red test ids before it parks a task.

THE GAP THIS CLOSES. Task 404ef4ce shipped a codified conductor node,
`workflow_step_red_test_ids` (api/workflows.py), whose whole purpose is to
name a task's red test ids from data PRISM already persists -- task.verify
for the pinned targets, ConductorService._red_step_sha for the anchor, and
oracle_spec.fresh_red_receipt for the demonstration -- instead of asking a
model to retype them. That node went green and was never wired into the one
path that needed it.

task_runner._handle_stall still greps ONLY the agentic step's model-authored
prose (`parent.completion_proof` via red_test_ids/_TEST_ID_RE). When the
model does not happen to write a `X.py::name` id into its report, the task
parks for a human with "no red test id was named in the last proof" -- even
though the pinned ids and a fresh red receipt are sitting in the database.
Observed live 2026-09-05 on tasks 6a7105f9 and 0b5dd37c, both blocked with
that exact reason.

A stall must consult the deterministic source before it parks a human. When
the codified read yields nothing either, the honest empty answer carries the
node's OWN reason (no anchor / no fresh receipt / not pytest-backed) rather
than blaming a proof that was never the authority.
"""
from __future__ import annotations

import types

import pytest

PINNED = "services/prism-service/tests/unit/test_foo.py::test_bar"


class _Task:
    def __init__(self, task_id, verify=None, completion_proof="", proof_type="test"):
        self.id = task_id
        self.verify = list(verify or [])
        self.completion_proof = completion_proof
        self.proof_type = proof_type
        self.oracle = ""
        self.likely_misfire = ""
        self.priority = 10
        self.tags = []
        self.status = "in_progress"
        self.gate_state = "none"


class _TaskSvc:
    """Enough TaskService for the stall path: get/list/create/update/history."""

    def __init__(self, parent):
        self._parent = parent
        self.created = []
        self.updates = []
        self.history = []
        self._children = []

    def get(self, tid):
        return self._parent if tid == self._parent.id else None

    def list(self, **kw):
        if "parent_id" in kw:
            return list(self._children)
        return []

    def create(self, **kw):
        child = _Task(f"child-{len(self.created)}", verify=kw.get("verify"))
        child.title = kw.get("title", "")
        self.created.append(kw)
        self._children.append(child)
        return child

    def update(self, tid, **kw):
        self.updates.append((tid, kw))

    def record_history(self, tid, **kw):
        self.history.append((tid, kw))


@pytest.fixture
def runner(monkeypatch):
    from prism_service.services import task_runner as tr

    # This task is NOT shipped and its step is a plain agent step, so the
    # stall path reaches the splitter rather than the shipped/gate branches.
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _tid: False)
    monkeypatch.setattr(tr, "_last_outcome_was_a_kill",
                        lambda *_a, **_k: False)
    return tr


def _blocked_reason(svc):
    for _tid, kw in svc.updates:
        if kw.get("status") == "blocked":
            return kw.get("blocked_reason", "")
    return ""


def test_a_stall_splits_on_the_codified_ids_when_the_proof_names_none(
        runner, monkeypatch):
    """AC-1 (the live 6a7105f9 / 0b5dd37c shape). The model's report names no
    pytest id, but the task pins one and a fresh red receipt demonstrates it.
    The splitter must use the codified answer and decompose, never park."""
    parent = _Task("t-1", verify=[PINNED],
                   completion_proof="I refactored the helper and it looks good.")
    svc = _TaskSvc(parent)

    monkeypatch.setattr(
        runner, "_codified_red_test_ids",
        lambda project, task_id: ([PINNED], "red demonstrated at abc123def456"),
        raising=False)

    out = runner._handle_stall(svc, "t-1", "implement_tasks", project="prism")

    assert out["stalled"]["action"] == "decomposed", _blocked_reason(svc)
    assert [c["verify"] for c in svc.created] == [[PINNED]]
    assert "no red test id was named" not in _blocked_reason(svc)


def test_the_proof_still_wins_when_it_does_name_ids(runner, monkeypatch):
    """AC-2. The prose path is not retired -- when the report DOES name an id
    the behaviour is unchanged, and the codified read is not consulted."""
    parent = _Task("t-2", verify=[PINNED], completion_proof=f"red: {PINNED}")
    svc = _TaskSvc(parent)

    def _boom(*_a, **_k):
        raise AssertionError("codified read must not run when the proof names ids")

    monkeypatch.setattr(runner, "_codified_red_test_ids", _boom, raising=False)

    out = runner._handle_stall(svc, "t-2", "implement_tasks", project="prism")

    assert out["stalled"]["action"] == "decomposed"
    assert [c["verify"] for c in svc.created] == [[PINNED]]


def test_an_empty_codified_read_parks_with_the_nodes_own_reason(
        runner, monkeypatch):
    """AC-3. When neither source names an id the task still parks -- but the
    reason must carry WHY the deterministic read was empty, instead of
    blaming a model proof that was never the authority."""
    parent = _Task("t-3", verify=[PINNED], completion_proof="no ids here")
    svc = _TaskSvc(parent)

    monkeypatch.setattr(
        runner, "_codified_red_test_ids",
        lambda project, task_id: (
            [], "no fresh red receipt for the current red-step commit (abc123def456)"),
        raising=False)

    out = runner._handle_stall(svc, "t-3", "implement_tasks", project="prism")

    assert out["stalled"]["action"] == "blocked"
    reason = _blocked_reason(svc)
    assert "no fresh red receipt" in reason, reason


def test_the_codified_read_is_real_and_pure(monkeypatch, tmp_path):
    """AC-4. _codified_red_test_ids is not a stub: against a real
    EvidenceReceipt it returns the task's pinned ids and the anchor's reason,
    reading the same functions the codified node reads. It never runs pytest
    and never invokes a model."""
    from prism_service.services import task_runner as tr
    from prism_service.services import oracle_spec as osp

    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    task_id, red_sha = "t-real", "d" * 40
    osp.append_receipt("prism", osp.EvidenceReceipt(
        task_id=task_id, job_id="job-9", spec_hash="spec-fixed",
        tree_sha=red_sha, adapter=osp.ADAPTER_PYTEST, passed=False,
        status=osp.ST_RED,
        reason=f"pytest_ids: {PINNED} -> rc=1 (1 failed in 0.10s)"))
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.OracleSpec.spec_hash",
        lambda self: "spec-fixed")

    parent = _Task(task_id, verify=[PINNED])
    monkeypatch.setattr(
        tr, "get_project",
        lambda p: types.SimpleNamespace(
            task_svc=_TaskSvc(parent),
            conductor_svc=types.SimpleNamespace(
                _red_step_sha=lambda _t: red_sha)),
        raising=False)

    ids, reason = tr._codified_red_test_ids("prism", task_id)

    assert ids == [PINNED]
    assert red_sha[:12] in reason
