"""Pin test: the pre-red multiplier blocks (prism_service/blocks/red_blocks.py)
-- owner 2026-09-13/14: "we should have multiplier steps before the red
that are pydantic to help speed up the inference by minimizing the ask
of the model... as we have our ontology, patterns and practices."

Covers: the four blocks are registered; red.targets_from_acs pairs
pinned test ids with their AC lines; red.context_pack trims conventions
to compact labels under a budget; red.prompt_compose builds a shorter
prompt than the retired static template and still carries the task's
oracle/title; the write-failing-tests-loop.json behaviour actually
declares the three new steps, in order, ahead of "loop", with a handler
for each.
"""
import json
from pathlib import Path

import pytest

from prism_service.blocks import Block, get_block
from prism_service.blocks import red_blocks  # noqa: F401 -- triggers registration
from prism_service.services import task_runner

_REPO_BEHAVIORS = (Path(__file__).resolve().parents[4]
                   / ".prism" / "behaviors" / "conductor")

EXPECTED_RED_BLOCK_IDS = [
    "red.targets_from_acs", "red.context_pack", "red.prompt_compose",
    "red.materialize",
]


@pytest.mark.parametrize("block_id", EXPECTED_RED_BLOCK_IDS)
def test_each_red_block_is_registered(block_id):
    block = get_block(block_id)
    assert isinstance(block, Block)
    assert block.owner_seat == "workflows"
    assert block.kind == "deterministic"
    assert block.title.strip()
    assert block.description.strip()


class _FakeTask:
    def __init__(self, plan_doc="", verify=None, oracle="", title="", description=""):
        self.plan_doc = plan_doc
        self.verify = verify or []
        self.oracle = oracle
        self.title = title
        self.description = description


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, task_id):
        return self._task


class _FakeProjectCtx:
    def __init__(self, task):
        self.task_svc = _FakeTaskSvc(task)
        self.brain_svc = None
        self.memory_svc = None
        self.workflow_svc = None
        self.governance = None


PLAN_DOC = """## Acceptance Criteria
- AC-1: the widget renders on load
- AC-2: clicking the widget opens the drawer
"""


def test_targets_from_acs_pairs_pinned_ids_with_ac_lines(monkeypatch):
    from prism_service.api import workflows as _wf

    task = _FakeTask(
        plan_doc=PLAN_DOC,
        verify=["tests/unit/test_widget.py::test_renders",
               "tests/unit/test_widget.py::test_opens_drawer"])
    monkeypatch.setattr(_wf, "get_project", lambda project: _FakeProjectCtx(task))

    resp = _wf.workflow_step_red_targets_from_acs(
        _wf.RedTargetsRequest(task_id="t1"), project="prism")

    assert len(resp.targets) == 2
    assert resp.targets[0].test_id == "tests/unit/test_widget.py::test_renders"
    assert resp.targets[0].ac_id == "AC-1"
    assert "AC-1" in resp.targets_block
    assert resp.targets[0].file_path == "tests/unit/test_widget.py"


def test_targets_from_acs_degrades_to_empty_on_no_task():
    from prism_service.api import workflows as _wf
    resp = _wf.workflow_step_red_targets_from_acs(
        _wf.RedTargetsRequest(task_id=""), project="prism")
    assert resp.targets == []
    assert resp.targets_block == ""


def test_context_pack_labels_are_compact_not_raw_reprs(monkeypatch):
    """The regression found live building this landing: dumping str() of
    a full ExpertiseEntry burns the entire char budget on one memory's
    repr. A label must be short."""
    from prism_service.api import workflows as _wf

    class _LongEntry:
        name = "some-convention-name"
        description = "x" * 5000

    task = _FakeTask(verify=["tests/unit/test_widget.py::test_renders"])
    monkeypatch.setattr(_wf, "get_project", lambda project: _FakeProjectCtx(task))

    class _FakeBuilder:
        def __init__(self, **kw):
            pass

        def build(self, **kw):
            return {"conventions": [_LongEntry()]}

    monkeypatch.setattr(_wf, "ContextBuilder", _FakeBuilder)
    monkeypatch.setattr(_wf, "_scaffold_source_root", lambda project, task_id: None)

    resp = _wf.workflow_step_red_context_pack(
        _wf.RedContextPackRequest(task_id="t1", budget_chars=2000),
        project="prism")

    assert resp.conventions == ["some-convention-name"]
    assert len(resp.context_block) < 200


def test_context_pack_respects_the_budget(monkeypatch):
    from prism_service.api import workflows as _wf

    task = _FakeTask()
    monkeypatch.setattr(_wf, "get_project", lambda project: _FakeProjectCtx(task))

    class _FakeBuilder:
        def __init__(self, **kw):
            pass

        def build(self, **kw):
            return {"conventions": [f"convention-{i}" for i in range(50)]}

    monkeypatch.setattr(_wf, "ContextBuilder", _FakeBuilder)

    resp = _wf.workflow_step_red_context_pack(
        _wf.RedContextPackRequest(task_id="t1", budget_chars=100),
        project="prism")
    assert len(resp.context_block) <= 100 + len("\n... (trimmed)")


def test_prompt_compose_is_shorter_than_the_retired_static_template():
    """The retired write-failing-tests-loop.json template (version <= 8)
    was 2151 chars BEFORE any block was filled in. The composed prompt
    with typical-sized blocks must come in shorter -- that is the entire
    point of this landing."""
    from prism_service.api import workflows as _wf

    OLD_STATIC_TEMPLATE_CHARS = 2151
    resp = _wf.workflow_step_red_prompt_compose(_wf.RedPromptComposeRequest(
        task_id="", task_hint="A short task title",
        targets_block="RED TARGETS -- write exactly these:\n- a::b",
        context_block="Conventions:\n- some-convention",
        scaffold_block="", refusal_block=""), project="prism")
    assert len(resp.prompt) < OLD_STATIC_TEMPLATE_CHARS
    assert "Draft a failing test" in resp.prompt
    assert "A short task title" in resp.prompt


def test_prompt_compose_carries_the_oracle(monkeypatch):
    from prism_service.api import workflows as _wf

    task = _FakeTask(oracle="Open the app. The widget is visible.")
    monkeypatch.setattr(_wf, "get_project", lambda project: _FakeProjectCtx(task))

    resp = _wf.workflow_step_red_prompt_compose(
        _wf.RedPromptComposeRequest(task_id="t1"), project="prism")
    assert "Open the app. The widget is visible." in resp.prompt


def test_materialize_calls_write_run_commit_in_order(monkeypatch):
    from prism_service.blocks.red_blocks import _run_red_materialize
    from prism_service.api import workflows as _wf

    calls = []
    monkeypatch.setattr(
        _wf, "workflow_step_write_test_file",
        lambda req, project: calls.append(("write", req.task_id)) or {"outcome": "ok"})
    monkeypatch.setattr(
        _wf, "workflow_step_run_pinned_suite",
        lambda req, project: calls.append(("run", req.expected_rc)) or {"outcome": "ok", "rc": 1})
    monkeypatch.setattr(
        _wf, "workflow_step_commit_tests_only",
        lambda req, project: calls.append(("commit", req.task_id)) or {"outcome": "ok"})

    out = _run_red_materialize("prism", "t1", "tests/unit/test_x.py", "def test_x(): assert False")
    assert calls == [("write", "t1"), ("run", 1), ("commit", "t1")]
    assert set(out.keys()) == {"write", "run", "commit"}


# ----------------------------------------------------------------------
# The behaviour file itself declares the new pre-red chain, in order,
# ahead of "loop" -- and every declared route has a real handler.
# ----------------------------------------------------------------------

def _plan():
    return task_runner._node_plan("prism", "write_failing_tests")


@pytest.fixture(autouse=True)
def _hermetic_behavior_dir(monkeypatch):
    monkeypatch.setattr(
        task_runner, "_behavior_dir", lambda project: _REPO_BEHAVIORS)


def test_the_behaviour_declares_the_pre_red_chain_before_loop():
    plan = _plan()
    assert plan is not None
    routes = [s["route"] for s in plan["steps"]]
    for needed in ("red-targets-from-acs", "red-context-pack",
                  "red-prompt-compose"):
        assert needed in routes, f"missing pre-red block route: {needed}"
    assert (routes.index("red-prompt-compose")
           < routes.index("reason-loop")), (
        "compose must run BEFORE the agentic loop step")
    assert (routes.index("test-scaffold")
           < routes.index("red-targets-from-acs")), (
        "the existing scaffold/recall/gather nodes stay upstream, unchanged")


def test_every_pre_red_route_has_a_handler():
    table = task_runner._step_handlers()
    for route in ("red-targets-from-acs", "red-context-pack",
                 "red-prompt-compose"):
        assert route in table, f"no handler registered for {route}"


def test_the_loop_step_no_longer_carries_a_static_megaprompt():
    """The loop step's own declared body must now just reference the
    composed prompt -- not restate the whole rc==1 essay inline."""
    path = _REPO_BEHAVIORS / "write-failing-tests-loop.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    loop_step = next(s for s in doc["steps"] if s["id"] == "loop")
    body = json.loads(loop_step["body"])
    assert body["prompt"] == "${prompt}", (
        "the loop step must interpolate the composed prompt, not carry "
        f"its own static template: {body['prompt']!r}")
