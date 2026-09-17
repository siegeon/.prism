from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
import json
import pytest
from pydantic import ValidationError
from prism_service.models.task_intake import TaskContract
from prism_service.services import task_intake as intake

@pytest.fixture
def environment(tmp_path, monkeypatch):
    source = tmp_path / "api.py"
    source.write_text("def existing_endpoint(): return {}")
    task = SimpleNamespace(id=str(uuid4()), title="Expose the saved task criteria", description="Show criteria in the task API response.",
        oracle="", proof_type="", allowed_files=[], verify=[], stop_if=[], likely_misfire="", plan_doc="", status="pending", workflow_step="", workflow="implement")
    events=[]
    def update(task_id, **fields):
        for key, value in fields.items(): setattr(task, key, value)
        return task
    ctx=SimpleNamespace(_data_dir=tmp_path / "data", task_svc=SimpleNamespace(get=lambda _: task, update=update,
        record_history=lambda *a, **k: events.append(k)),
        brain_svc=SimpleNamespace(search=lambda *a, **k: [{"source_file":"api.py","line_start":1}]),
        memory_svc=SimpleNamespace(recall=lambda *a, **k: [SimpleNamespace(id="mx-rule",name="use-existing-api",description="Reuse the task API.")]))
    from prism_service.services import claude_transcripts, ontology_rules, oracle_authoring
    monkeypatch.setattr(claude_transcripts, "_project_source_path", lambda _: str(tmp_path))
    monkeypatch.setattr(ontology_rules, "rule_catalog", lambda _: [{"name":"decision-has-evidence","description":"Cite evidence"}])
    monkeypatch.setattr(oracle_authoring, "validate_for_authoring", lambda **k: ({}, []))
    return ctx, task, events

def candidate():
    return {"oracle":"The /api/tasks response contains each saved acceptance criterion.","proof_type":"test",
        "acceptance_criteria":[{"id":"AC-1","given":"A task has saved criteria", "when":"A client reads /api/tasks", "then":"The response includes those criteria", "verification":"tests/test_api.py::test_criteria", "evidence_refs":["request","code:api.py","mx-rule"]}],
        "allowed_files":["api.py","tests/test_api.py"],"verify":["tests/test_api.py::test_criteria"],"stop_if":["The change needs files outside the declared scope"],"likely_misfire":"A mocked response passes while the real endpoint omits the criteria."}

def gathered(environment):
    ctx, task, _=environment
    return intake.gather(ctx,"fixture",task.id,lambda path: {"id":"conductor","fsms":[]})

def test_all_sources_reach_architect_and_contract_is_applied(environment):
    ctx, task, events=environment
    packet=gathered(environment)
    assert {s["kind"] for s in packet["sources"]} == {"request","brain","concept_memory","ontology","magic"}
    def infer(prompt, schema):
        for ref in ["code:api.py", "mx-rule", "ontology:rules", "magic:conductor"]: assert ref in prompt
        return {"contract":candidate(),"unresolved_questions":[]}
    intake.propose(ctx,"fixture",task.id,infer)
    assert task.oracle == ""
    intake.check(ctx,task.id)
    intake.apply(ctx,task.id)
    assert task.oracle == candidate()["oracle"]
    assert "AC-1" in task.plan_doc and "Verification:" in task.plan_doc
    assert task.status == "pending" and not task.workflow_step
    assert len([e for e in events if e["action"] == "intake_apply"]) == 1
    intake.apply(ctx,task.id)
    assert len([e for e in events if e["action"] == "intake_apply"]) == 1

@pytest.mark.parametrize("change",["stale_task","stale_source","unknown_citation","escape","missing_check","owner_constraint"])
def test_bad_contract_cannot_mutate_task(environment,change):
    ctx, task, _=environment
    gathered(environment)
    value=candidate()
    if change=="unknown_citation": value["acceptance_criteria"][0]["evidence_refs"]=["made-up"]
    if change=="escape": value["allowed_files"]=["../outside.py"]
    if change=="missing_check": value["verify"]=["tests/test_other.py"]
    if change=="owner_constraint":
        task.oracle="Keep the existing public API unchanged"
        gathered(environment)
    if change=="missing_check":
        with pytest.raises(ValidationError): intake.propose(ctx,"fixture",task.id,lambda *a:{"contract":value})
        return
    intake.propose(ctx,"fixture",task.id,lambda *a:{"contract":value})
    if change=="stale_task": task.description="Different intent"
    if change=="stale_source": (Path(intake.load(ctx,task.id)["root"])/"api.py").write_text("Changed source")
    with pytest.raises(ValueError): intake.check(ctx,task.id)
    with pytest.raises(ValueError): intake.apply(ctx,task.id)
    assert not task.plan_doc


def test_unknown_intent_remains_an_explicit_question(environment):
    ctx,task,_=environment
    gathered(environment)
    packet=intake.propose(ctx,"fixture",task.id,lambda *a:{"contract":None,"unresolved_questions":["Which result must the task create?"]})
    assert packet["state"]=="unresolved"
    with pytest.raises(ValueError,match="Which result"):
        intake.check(ctx,task.id)
    assert not task.oracle


def test_unchanged_validated_template_needs_no_inference(environment):
    ctx,task,_=environment
    gathered(environment)
    intake.propose(ctx,"fixture",task.id,lambda *a:{"contract":candidate()})
    intake.check(ctx,task.id)
    gathered(environment)
    packet=intake.propose(ctx,"fixture",task.id,lambda *a:pytest.fail("Unnecessary inference"))
    assert packet["inference_used"] is False


def test_in_progress_task_cannot_be_rewritten(environment):
    ctx,task,_=environment
    task.status="in_progress"
    with pytest.raises(ValueError,match="pending"):
        gathered(environment)


def test_http_refusal_is_a_typed_stop(environment,monkeypatch):
    from prism_service.api import workflows
    ctx,task,_=environment
    monkeypatch.setattr(workflows,"get_project",lambda _:ctx)
    result=workflows.workflow_step_task_intake_apply(workflows.TaskIntakeRequest(task_id=task.id),project="fixture")
    assert result.outcome=="error" and result.stop_chain and not task.oracle


def test_architect_definition_is_a_shared_magic_chain():
    doc=json.loads((Path(__file__).resolve().parents[4]/".prism/behaviors/architect/create-task.json").read_text())
    assert [s["id"] for s in doc["steps"]]==["gather","propose","validate","apply"]
    assert [s["id"] for s in doc["steps"] if s["agentic"]]==["propose"]
    assert all(s["kind"]=="http-callback" for s in doc["steps"])


def test_rest_creation_queues_intake_before_conductor(tmp_path, monkeypatch):
    from prism_service.api import tasks
    from prism_service.services.task_service import TaskService
    svc=TaskService(str(tmp_path/"tasks.db"))
    ctx=SimpleNamespace(task_svc=svc, conductor_svc=SimpleNamespace(advance_task=lambda *a,**k:pytest.fail("Started implementation before intake")))
    monkeypatch.setattr(tasks,"get_project",lambda _:ctx)
    calls=[]
    monkeypatch.setattr(intake,"after_create",lambda project,task:calls.append(task.id) or {"status":"queued","instanceId":"magic-run"})
    result=tasks.create_task(tasks.TaskCreate(title="A raw request", enter_conductor=True, session_id="intake-test"),project="fixture")
    assert result["intake"]["instanceId"]=="magic-run" and result["advanced"] is None
    assert result["task"].id in calls
    assert svc.get(result["task"].id).status=="pending"


def test_mcp_creation_uses_the_same_intake_boundary(tmp_path,monkeypatch):
    from prism_service.mcp import tools
    from prism_service import project_context
    from prism_service.services.task_service import TaskService
    svc=TaskService(str(tmp_path/"tasks.db"))
    ctx=SimpleNamespace(task_svc=svc,brain_svc=None,memory_svc=None,workflow_svc=None,conductor_svc=None,governance=None)
    monkeypatch.setattr(project_context,"get_project",lambda _:ctx)
    calls=[]
    monkeypatch.setattr(intake,"after_create",lambda project,task:calls.append(task.id) or {"status":"queued","instanceId":"magic-run"})
    result=tools._dispatch_tool("task_create",{"title":"A raw request"},project_id="fixture")
    output=json.loads(result[0].text)
    assert output["intake"]["instanceId"]=="magic-run" and output["id"] in calls


def test_real_authoring_controls_reject_false_test_oracle(environment,monkeypatch):
    from prism_service.services import oracle_authoring
    import importlib
    # Restore the production scorer rather than the isolated fixture's stub.
    importlib.reload(oracle_authoring)
    ctx,task,_=environment
    gathered(environment)
    value=candidate(); value["oracle"]="Unit tests pass"
    intake.propose(ctx,"fixture",task.id,lambda *a:{"contract":value})
    with pytest.raises(ValueError,match="user-reachable surface"):
        intake.check(ctx,task.id)


def test_architect_and_behavior_have_distinct_fsm_tiers():
    from prism_service.api.workflows import _apply_bot_tiers
    catalog=[{"id":"architect","kind":"bot","fsm_id":"pipeline","steps":[]},
        {"id":"create-task","parent_id":"architect","steps":[]}]
    _apply_bot_tiers(catalog)
    assert [c["tier"] for c in catalog]==[0,1]
