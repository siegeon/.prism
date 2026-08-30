"""Task 404ef4ce: a CODIFIED conductor node that names the red test ids a
task's pinned `task.verify` targets demonstrate at the task's own red-anchor
commit -- derived entirely from data PRISM already persists, never from a
model.

THE STALL THIS FIXES. Task 8fbd5cf0-c131-4102-9c4a-60894d9fc830 held a
complete implementation in its worktree and the conductor blocked it anyway:
"step implement_tasks did not advance after 3 attempts. No red test id was
named in the last proof." That reason comes from task_runner.py's stall
handler greping the AGENTIC implement-tasks-loop step's model-authored prose
for a `X.py::name` pytest id (task_runner._TEST_ID_RE / red_test_ids) -- a
fact the model has to retype instead of read off the system that already has
it: task.verify names the pinned targets, ConductorService._red_step_sha
resolves the red anchor, and oracle_spec.fresh_red_receipt already records
whether a trusted run demonstrated red there.

Same pattern as workflow_step_red_gate_status (api/workflows.py): pure
reads, never a model, never pytest execution inside the request, never a
repo/worktree lock. When no anchor resolves, or no fresh red receipt is on
file, or the task isn't pytest-backed, the honest answer is an EMPTY
red_test_ids list with a specific reason -- never a guess.
"""
from __future__ import annotations

import json
import types
from pathlib import Path


def _repo_root() -> Path:
    # tests/unit/<this file> -> unit -> tests -> prism-service -> services -> repo root
    return Path(__file__).resolve().parent.parent.parent.parent.parent


class _FakeTask:
    def __init__(self, verify=None, oracle="", likely_misfire="", proof_type="test"):
        self.verify = verify or []
        self.oracle = oracle
        self.likely_misfire = likely_misfire
        self.proof_type = proof_type


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, tid):
        return self._task


def _wire(monkeypatch, workflows_api, task, red_sha=""):
    class _FakeConductorSvc:
        def _red_step_sha(self, tid):
            return red_sha

    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(
            task_svc=_FakeTaskSvc(task), conductor_svc=_FakeConductorSvc()))


def test_red_test_ids_reports_pinned_ids_from_a_fresh_red_receipt(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.services import oracle_spec as osp

    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    task_id = "t-red-ids-1"
    red_sha = "b" * 40
    pinned = "services/prism-service/tests/unit/test_foo.py::test_bar"

    receipt = osp.EvidenceReceipt(
        task_id=task_id, job_id="job-1", spec_hash="spec-fixed", tree_sha=red_sha,
        adapter=osp.ADAPTER_PYTEST, passed=False, status=osp.ST_RED,
        reason=f"red demonstrated at {red_sha[:12]}: pytest_ids: {pinned} -> "
               f"rc=1 (1 failed in 0.10s)",
    )
    osp.append_receipt("prism", receipt)

    task = _FakeTask(verify=[pinned], oracle="", proof_type="test")
    _wire(monkeypatch, workflows_api, task, red_sha=red_sha)
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.OracleSpec.spec_hash",
        lambda self: "spec-fixed")

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id=task_id), project="prism")

    assert resp.red_test_ids == [pinned]
    assert resp.anchor_sha == red_sha
    assert "red demonstrated" in resp.reason


def test_red_test_ids_reports_no_anchor_honestly(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _FakeTask(
        verify=["services/prism-service/tests/unit/test_foo.py::test_bar"],
        proof_type="test")
    _wire(monkeypatch, workflows_api, task, red_sha="")

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id="t-no-anchor"), project="prism")

    assert resp.red_test_ids == []
    assert "no red-step commit resolved yet" in resp.reason


def test_red_test_ids_reports_no_fresh_receipt_honestly(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    red_sha = "c" * 40
    task = _FakeTask(
        verify=["services/prism-service/tests/unit/test_foo.py::test_bar"],
        proof_type="test")
    _wire(monkeypatch, workflows_api, task, red_sha=red_sha)

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id="t-no-receipt"), project="prism")

    assert resp.red_test_ids == []
    assert resp.anchor_sha == red_sha
    assert "no fresh red receipt" in resp.reason


def test_red_test_ids_matches_the_real_stalled_tasks_shape(tmp_path, monkeypatch):
    """Reproduces task 8fbd5cf0-c131-4102-9c4a-60894d9fc830's ACTUAL live
    shape, verified 2026-08-30 via POST /api/workflows/steps/red-gate-status:
    red_sha resolves (0b059e8b84f6c96a17635de25432415d6e8cc2c0),
    has_fresh_red_receipt is false, task.verify pins a bare FILE with no
    '::'. The codified step must answer honestly -- empty ids, a real
    reason -- not crash on the bare-file target and not guess."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    red_sha = "0b059e8b84f6c96a17635de25432415d6e8cc2c0"
    task = _FakeTask(
        verify=["services/prism-service/tests/unit/"
                "test_conductor_run_is_recorded_and_live.py"],
        proof_type="test")
    _wire(monkeypatch, workflows_api, task, red_sha=red_sha)

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(
            task_id="8fbd5cf0-c131-4102-9c4a-60894d9fc830"), project="prism")

    assert resp.red_test_ids == []
    assert resp.anchor_sha == red_sha
    assert resp.reason and "no fresh red receipt" in resp.reason


def test_red_test_ids_returns_no_ids_for_a_non_pytest_oracle(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _FakeTask(verify=[], oracle="check /healthz returns 200",
                      proof_type="")
    _wire(monkeypatch, workflows_api, task, red_sha="")

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id="t-not-pytest"), project="prism")

    assert resp.red_test_ids == []
    assert "not pytest" in resp.reason.lower()


def test_red_test_ids_reports_no_such_task_honestly(monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(
            task_svc=_FakeTaskSvc(None), conductor_svc=None))

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id="t-missing"), project="prism")

    assert resp.red_test_ids == []
    assert "no such task" in resp.reason


def test_red_test_ids_never_calls_a_model(tmp_path, monkeypatch):
    """Structural guarantee: this codified step must never touch a model,
    no matter which branch it takes. Monkeypatch claude_cli.invoke to raise
    -- if the endpoint ever called it (directly or via reason-loop), any of
    the scenarios below would blow up instead of returning normally."""
    from prism_service.api import workflows as workflows_api
    from prism_service.services import oracle_spec as osp
    from prism_service.inference import claude_cli

    def _boom(*a, **kw):
        raise AssertionError("workflow_step_red_test_ids must never call a model")

    monkeypatch.setattr(claude_cli, "invoke", _boom)
    monkeypatch.setattr(
        "prism_service.config.project_data_dir", lambda project: tmp_path)

    red_sha = "d" * 40
    pinned = "services/prism-service/tests/unit/test_foo.py::test_bar"
    receipt = osp.EvidenceReceipt(
        task_id="t-model-guard", job_id="job-1", spec_hash="spec-fixed",
        tree_sha=red_sha, adapter=osp.ADAPTER_PYTEST, passed=False,
        status=osp.ST_RED, reason="red demonstrated")
    osp.append_receipt("prism", receipt)
    monkeypatch.setattr(
        "prism_service.services.oracle_spec.OracleSpec.spec_hash",
        lambda self: "spec-fixed")

    task = _FakeTask(verify=[pinned], proof_type="test")
    _wire(monkeypatch, workflows_api, task, red_sha=red_sha)

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id="t-model-guard"), project="prism")

    assert resp.red_test_ids == [pinned]


def test_red_test_ids_is_a_codified_http_callback_node_registered_in_the_pipeline():
    """Config-reading, not source-string-grepping: parses the two real JSON
    files the Workflows canvas loads. The node must be a real http-callback
    to the new deterministic route -- never reason-loop -- and must be
    listed in bot.json's pipeline FSM so it renders as a real node."""
    root = _repo_root()
    behavior_path = root / ".prism" / "behaviors" / "conductor" / "red-test-ids.json"
    bot_path = root / ".prism" / "behaviors" / "conductor" / "bot.json"

    behavior = json.loads(behavior_path.read_text(encoding="utf-8"))
    assert behavior["id"] == "red-test-ids"
    assert behavior["botId"] == "conductor"
    assert behavior["fsmId"] == "pipeline"
    steps = behavior["steps"]
    assert steps, "red-test-ids.json must declare at least one step"
    for step in steps:
        assert step["kind"] == "http-callback"
        assert "reason-loop" not in step["url"]
        assert "/api/workflows/steps/red-test-ids" in step["url"]

    bot = json.loads(bot_path.read_text(encoding="utf-8"))
    pipeline = next(f for f in bot["fsms"] if f["fsmId"] == "pipeline")
    assert "red-test-ids" in pipeline["behaviorIds"]
