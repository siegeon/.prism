from types import SimpleNamespace
import pytest
from prism_service.models.planning_resolution import resolve_planning
from prism_service.services.task_runner import _dispatch_declared_steps, _result_from_dispatch
from prism_service.api import workflows as wf

@pytest.mark.parametrize('response', [{'ok': False}, {'validation': {'ok': False}}, {'stop_chain': True}, {'kind': 'workflow.step_result', 'outcome': 'unknown'}])
def test_refusal_stops_dependent_action(response):
    effects = []
    rows = _dispatch_declared_steps('fixture', {'steps': [{'route': 'first'}, {'route': 'effect'}]}, handlers={'first': lambda *_: response, 'effect': lambda *_: effects.append(True)})
    assert not effects and not rows[0]['ok']
    assert _result_from_dispatch(rows).exit_code == 1

def task(**changes):
    values = dict(id='t', title='Add the requested field', description='Add the field to the existing response.', oracle='The response includes the field', verify=['tests/test_response.py'], allowed_files=['api/response.py'], stop_if=['The response changes another field'], tags=['planning-template:bounded-change-v1'])
    values.update(changes)
    return SimpleNamespace(**values)

def test_known_unknown_missing_context_are_distinct():
    assert resolve_planning(task(), 'verify_plan').state == 'complete'
    assert resolve_planning(task(tags=[]), 'verify_plan').state == 'unknown'
    assert resolve_planning(task(oracle=''), 'verify_plan').unresolved == ['oracle']
    assert resolve_planning(None, 'verify_plan').state == 'error'

def test_complete_template_uses_no_model(monkeypatch):
    t = task()
    monkeypatch.setattr(wf, 'get_project', lambda _: SimpleNamespace(task_svc=SimpleNamespace(get=lambda _: t)))
    monkeypatch.setattr(wf, '_score_rubric', lambda *a, **k: {'ok': True, 'reason': 'fixture control passed'})
    monkeypatch.setattr(wf, 'ContextBuilder', lambda **k: pytest.fail('Entered inference context'))
    resolution = resolve_planning(t, 'verify_plan')
    result = wf.workflow_step_reason_loop(wf.ReasonLoopRequest(task_id='t', prompt='fill', json_schema={}, rubric='plan_structured', resolution_json=resolution.model_dump_json()), project='fixture')
    assert result.reason['inference_used'] is False and result.reason['cost_usd'] == 0 and not result.stop_chain

def test_stale_binding_cannot_execute(monkeypatch):
    t = task(); resolution = resolve_planning(t, 'verify_plan'); t.oracle = 'Changed intent'
    monkeypatch.setattr(wf, 'get_project', lambda _: SimpleNamespace(task_svc=SimpleNamespace(get=lambda _: t)))
    result = wf.workflow_step_reason_loop(wf.ReasonLoopRequest(task_id='t', prompt='fill', json_schema={}, rubric='plan_structured', resolution_json=resolution.model_dump_json()), project='fixture')
    assert result.stop_chain

def test_context_failure_is_not_an_empty_frame(monkeypatch):
    def unavailable(_): raise RuntimeError('context store unavailable')
    monkeypatch.setattr(wf, 'get_project', unavailable)
    result = wf.workflow_step_plan_compose(wf.PlanComposeRequest(task_id='t'), project='fixture')
    assert not result.ok and result.stop_chain and 'context store unavailable' in result.reason


def test_canonical_step_exports_bound_values():
    from prism_service.models.step_result import StepResult
    from prism_service.services.task_runner import _exported_variables
    assert _exported_variables(StepResult(outcome="passed", outputs={"plan_doc": "bound"}))["planDoc"] == "bound"

def test_template_keeps_all_verification_targets_and_refuses_blank_bindings():
    resolved = resolve_planning(task(verify=["tests/test_one.py", "tests/test_two.py"]), "verify_plan")
    assert [ac["pytest_id"] for ac in resolved.fields["acs"]] == ["tests/test_one.py", "tests/test_two.py"]
    assert resolve_planning(task(verify=[" "]), "verify_plan").state == "unknown"


def test_production_dispatch_uses_magic_and_preserves_typed_result(monkeypatch):
    import prism_service.services.task_runner as runner
    monkeypatch.setenv("MAGIC_WORKFLOWS_URL", "http://magic")
    calls = []
    def engine(path, method="GET", body=None):
        calls.append((path, method, body))
        if method == "POST":
            return {"instanceId": "run-1"}
        return {"status": "Complete", "data": {
            "definition": {"steps": [{"id": "loop", "url": "/api/workflows/steps/reason-loop"}]},
            "results": [{"step": "loop", "exitCode": 0, "output": '{"reason":{"ok":true,"fields":{"plan_doc":"bound"}}}'}]}}
    monkeypatch.setattr(wf, "_workflow_engine_json", engine)
    monkeypatch.setattr(runner, "_step_handlers", lambda: pytest.fail("Used a second execution engine"))
    rows = runner._dispatch_declared_steps("fixture", {"behavior": "verify-plan-loop", "steps": [{}]}, request_key="attempt-1")
    assert calls[0][0] == "/workflows/bots/fixture/conductor/pipeline/verify-plan-loop"
    assert calls[0][2]["requestKey"] == "attempt-1"
    assert runner._result_from_dispatch(rows).final_text() == "bound"


def test_magic_unavailable_does_not_fall_back(monkeypatch):
    import prism_service.services.task_runner as runner
    monkeypatch.setenv("MAGIC_WORKFLOWS_URL", "http://magic")
    monkeypatch.setattr(wf, "_workflow_engine_json", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline")))
    monkeypatch.setattr(runner, "_step_handlers", lambda: pytest.fail("Used a second execution engine"))
    rows = runner._dispatch_declared_steps("fixture", {"behavior": "verify-plan-loop", "steps": []})
    assert runner._result_from_dispatch(rows).exit_code == 1


def test_story_template_is_executed_as_a_declared_chain():
    from prism_service.services.task_runner import _runs_as_declared_steps
    assert _runs_as_declared_steps("draft_story", {"steps": [{"route": "planning-resolve"}, {"route": "reason-loop"}], "json_schema": {"type": "object"}})
