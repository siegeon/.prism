"""Task d0b392b3: a browser-proof (proof_type=demo) task gets a red step it
can pass, instead of stalling on a pytest draft it can never make green.

THE LIVE DEFECT (tasks b490fabc, 8c3e0f37). `write-failing-tests-loop.json`'s
declared chain always drafts, writes, runs and commits a pytest file, no
matter what the task's derived OracleSpec adapter is. For a proof_type=demo
task whose oracle names a browser surface (no URL, no pytest material in
task.verify), oracle_spec.OracleSpec.from_task correctly derives
adapter="browser" -- but nothing downstream ever asks that question before
drafting a pytest file anyway. On b490fabc that draft imported a module that
did not exist; task_runner._handle_stall then consulted the codified
red-test-ids read, got the honest-but-terminal "adapter=browser, no pytest
node ids to name", and parked the task forever -- never reaching red_gate,
where conductor_service.adjudicate_demo_red_gate / _verify_gate ALREADY
auto-approve a demo ticket's red_gate from the demo rubric (that machinery
is untouched here: both files are pinned control-plane policy per
control_plane.POLICY_FILES, and stop_if #2 forbids routing a gate to a
human).

This suite pins the fix, confined to non-policy files:
  * a new codified node, `oracle-route-check` (api/workflows.py), declared
    as the FIRST step of write-failing-tests-loop.json (AC-1/AC-4);
  * a generic `stop_chain` early-exit in task_runner._dispatch_declared_steps
    so a declared node can branch without a bare, undeclared Python `if`
    (AC-1);
  * the red-test-ids node (workflow_step_red_test_ids) answering with the
    demo rubric's own evidence for a browser-adapter demo task, instead of
    a bare empty-list refusal (AC-2);
  * task_runner._handle_stall never blaming "no pytest node ids to name" for
    a demo/browser task, while a proof_type=test task's stall wording is
    untouched (AC-3, stop_if #3).
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest


def _repo_root() -> Path:
    # tests/unit/<this file> -> unit -> tests -> prism-service -> services -> repo root
    return Path(__file__).resolve().parent.parent.parent.parent.parent


def _behavior_path() -> Path:
    return (_repo_root() / ".prism" / "behaviors" / "conductor"
            / "write-failing-tests-loop.json")


class _FakeTask:
    def __init__(self, task_id="t-1", verify=None, oracle="",
                 likely_misfire="", proof_type="test", tags=None,
                 completion_proof=""):
        self.id = task_id
        self.verify = list(verify or [])
        self.oracle = oracle
        self.likely_misfire = likely_misfire
        self.proof_type = proof_type
        self.tags = list(tags or [])
        self.completion_proof = completion_proof
        self.priority = 10
        self.status = "in_progress"
        self.gate_state = "none"


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task
        self.history = []
        self.created = []
        self.updates = []
        self._children = []

    def get(self, tid):
        return self._task if (self._task and tid == self._task.id) else None

    def record_history(self, tid, **kw):
        self.history.append((tid, kw))

    def list(self, **kw):
        if "parent_id" in kw:
            return list(self._children)
        return []

    def create(self, **kw):
        child = _FakeTask(f"child-{len(self.created)}", verify=kw.get("verify"))
        self.created.append(kw)
        self._children.append(child)
        return child

    def update(self, tid, **kw):
        self.updates.append((tid, kw))


def _blocked_reason(svc):
    for _tid, kw in svc.updates:
        if kw.get("status") == "blocked":
            return kw.get("blocked_reason", "")
    return ""


# ---------------------------------------------------------------------------
# AC-1 / AC-4: the node file declares the branch as a real codified step.
# ---------------------------------------------------------------------------


def test_write_failing_tests_loop_declares_the_oracle_route_step():
    doc = json.loads(_behavior_path().read_text(encoding="utf-8"))
    steps = doc.get("steps") or []
    routes = [
        (s.get("url") or "").split("/steps/")[-1].split("?")[0]
        for s in steps
    ]
    assert "oracle-route-check" in routes, routes
    assert routes.index("oracle-route-check") < routes.index("write-test-file"), (
        "oracle-route-check must run BEFORE the pytest write/run/commit "
        f"chain, got order {routes!r}")


# ---------------------------------------------------------------------------
# AC-1: the new codified node itself.
# ---------------------------------------------------------------------------


def _wire(monkeypatch, workflows_api, task):
    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(task_svc=_FakeTaskSvc(task)))
    return workflows_api


def test_oracle_route_check_routes_pytest_backed_tasks_unchanged(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _FakeTask(
        verify=["services/prism-service/tests/unit/test_foo.py::test_bar"],
        proof_type="test")
    _wire(monkeypatch, workflows_api, task)

    resp = workflows_api.workflow_step_oracle_route_check(
        workflows_api.OracleRouteCheckRequest(task_id=task.id), project="prism")

    assert resp.route == "pytest"
    assert resp.stop_chain is False


def test_oracle_route_check_routes_browser_demo_task_to_the_demo_rubric(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _FakeTask(
        verify=[], oracle="the /workflows canvas shows the new node",
        proof_type="demo")
    svc = _FakeTaskSvc(task)
    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(task_svc=svc))

    resp = workflows_api.workflow_step_oracle_route_check(
        workflows_api.OracleRouteCheckRequest(task_id=task.id), project="prism")

    assert resp.route == "demo"
    assert resp.stop_chain is True
    assert "demo rubric" in resp.reason.lower()
    assert resp.report and "demo rubric" in resp.report.lower()
    # AC: the task history names the demo rubric as the red evidence.
    assert any("demo" in str(kw.get("details", "")).lower()
               for _tid, kw in svc.history), svc.history


def test_oracle_route_check_never_touches_a_non_demo_browser_oracle(monkeypatch):
    """stop_if #3 boundary check: a browser-adapter oracle that is NOT
    proof_type=demo is out of THIS ticket's scope -- it must still route to
    "pytest" (the pre-existing behaviour) rather than being silently
    swept into the new demo branch."""
    from prism_service.api import workflows as workflows_api

    task = _FakeTask(verify=[], oracle="check the health page renders",
                      proof_type="")
    _wire(monkeypatch, workflows_api, task)

    resp = workflows_api.workflow_step_oracle_route_check(
        workflows_api.OracleRouteCheckRequest(task_id=task.id), project="prism")

    assert resp.route == "pytest"
    assert resp.stop_chain is False


# ---------------------------------------------------------------------------
# AC-1: the dispatcher's generic early-exit primitive.
# ---------------------------------------------------------------------------


def test_dispatch_declared_steps_stops_after_a_stop_chain_result():
    from prism_service.services import task_runner as tr

    calls: list[str] = []

    def _route(_project, _body):
        return types.SimpleNamespace(route="demo", stop_chain=True,
                                     reason="demo rubric", report="report text")

    def _should_not_run(_project, _body):
        calls.append("write-test-file")
        raise AssertionError("must not run after stop_chain=True")

    plan = {"steps": [
        {"route": "oracle-route-check", "body": {}},
        {"route": "write-test-file", "body": {}},
        {"route": "run-pinned-suite", "body": {}},
        {"route": "commit-tests-only", "body": {}},
    ]}
    handlers = {"oracle-route-check": _route, "write-test-file": _should_not_run,
                "run-pinned-suite": _should_not_run,
                "commit-tests-only": _should_not_run}

    rows = tr._dispatch_declared_steps("prism", plan, handlers=handlers)

    assert calls == []
    assert [r["route"] for r in rows] == ["oracle-route-check"]


def test_dispatch_declared_steps_continues_when_no_stop_chain():
    from prism_service.services import task_runner as tr

    def _route(_project, _body):
        return types.SimpleNamespace(route="pytest", stop_chain=False)

    seen: list[str] = []

    def _mark(name):
        def _fn(_project, _body):
            seen.append(name)
            return {"ok": True}
        return _fn

    plan = {"steps": [
        {"route": "oracle-route-check", "body": {}},
        {"route": "write-test-file", "body": {}},
    ]}
    handlers = {"oracle-route-check": _route,
                "write-test-file": _mark("write-test-file")}

    rows = tr._dispatch_declared_steps("prism", plan, handlers=handlers)

    assert seen == ["write-test-file"]
    assert [r["route"] for r in rows] == ["oracle-route-check", "write-test-file"]


def test_result_from_dispatch_reports_the_demo_route_without_a_build_chain():
    from prism_service.services import task_runner as tr

    rows = [{"ok": True, "route": "oracle-route-check",
             "result": types.SimpleNamespace(
                 route="demo", stop_chain=True,
                 reason="demo-proof ticket: no test suite by design",
                 report="No pytest file drafted; demo rubric is the red "
                        "evidence for this task.")}]

    result = tr._result_from_dispatch(rows)

    assert result is not None
    assert "demo rubric" in result.final_text().lower()
    assert result.exit_code == 0
    assert result.usage is None


def test_result_from_dispatch_falls_through_to_the_pytest_chain():
    """A route="pytest" row must never be mistaken for the demo shape --
    _result_from_dispatch keeps falling through to the pre-existing
    build-chain / reason-loop readers."""
    from prism_service.services import task_runner as tr

    rows = [{"ok": True, "route": "oracle-route-check",
             "result": types.SimpleNamespace(route="pytest", stop_chain=False)}]

    assert tr._result_from_dispatch(rows) is None


# ---------------------------------------------------------------------------
# AC-2: the red-test-ids node answers with the demo rubric's own evidence.
# ---------------------------------------------------------------------------


def test_red_test_ids_reports_demo_rubric_evidence_for_a_browser_demo_task(
        monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _FakeTask(verify=[], oracle="the new node appears on the canvas",
                      proof_type="demo")

    class _FakeConductorSvc:
        def _red_step_sha(self, tid):
            return ""

    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(
            task_svc=_FakeTaskSvc(task), conductor_svc=_FakeConductorSvc()))

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id=task.id), project="prism")

    assert resp.red_test_ids == []
    assert "no pytest node ids to name" not in resp.reason
    assert resp.demo_rubric_evidence
    assert "demo rubric" in resp.demo_rubric_evidence.lower()


def test_red_test_ids_unaffected_for_a_pytest_backed_task(monkeypatch):
    """stop_if #3: a proof_type=test task's red-test-ids response must be
    byte-for-byte the same shape as before this ticket."""
    from prism_service.api import workflows as workflows_api
    from prism_service.services import oracle_spec as osp

    task = _FakeTask(
        verify=["services/prism-service/tests/unit/test_foo.py::test_bar"],
        proof_type="test")

    class _FakeConductorSvc:
        def _red_step_sha(self, tid):
            return ""

    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(
            task_svc=_FakeTaskSvc(task), conductor_svc=_FakeConductorSvc()))

    resp = workflows_api.workflow_step_red_test_ids(
        workflows_api.RedTestIdsRequest(task_id=task.id), project="prism")

    assert resp.red_test_ids == []
    assert resp.demo_rubric_evidence == ""
    assert "no red-step commit resolved yet" in resp.reason


# ---------------------------------------------------------------------------
# AC-3: _handle_stall never blames a missing pytest id for a settled demo
# task, and a proof_type=test task's stall wording is untouched.
# ---------------------------------------------------------------------------


@pytest.fixture
def runner(monkeypatch):
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _tid: False)
    monkeypatch.setattr(tr, "_last_outcome_was_a_kill", lambda *_a, **_k: False)
    return tr


def test_handle_stall_never_blames_missing_pytest_ids_for_a_demo_task(
        runner, monkeypatch):
    parent = _FakeTask(
        "demo-1", verify=[], proof_type="demo",
        oracle="the operator sees the change live",
        completion_proof="I looked at the screen and nothing changed.")
    svc = _FakeTaskSvc(parent)

    monkeypatch.setattr(
        runner, "_codified_red_test_ids",
        lambda project, task_id: (
            [], "task's derived oracle spec is not pytest-backed "
                "(adapter=browser) -- no pytest node ids to name"),
        raising=False)

    out = runner._handle_stall(svc, "demo-1", "implement_tasks", project="prism")

    reason = _blocked_reason(svc)
    assert "no pytest node ids to name" not in reason, reason
    assert "demo rubric" in reason.lower(), reason
    assert out["stalled"]["action"] == "blocked"


def test_handle_stall_test_proof_wording_is_unchanged(runner, monkeypatch):
    """stop_if #3 regression guard: a proof_type=test task with a genuinely
    empty codified read must keep the EXACT pre-existing wording."""
    parent = _FakeTask(
        "test-1",
        verify=["services/prism-service/tests/unit/test_foo.py::test_bar"],
        proof_type="test", completion_proof="no ids here")
    svc = _FakeTaskSvc(parent)

    monkeypatch.setattr(
        runner, "_codified_red_test_ids",
        lambda project, task_id: (
            [], "no fresh red receipt for the current red-step commit (abc123def456)"),
        raising=False)

    out = runner._handle_stall(svc, "test-1", "implement_tasks", project="prism")

    reason = _blocked_reason(svc)
    assert "no fresh red receipt" in reason, reason
    assert out["stalled"]["action"] == "blocked"
