"""Every layer of the flow can show its OWN execution instances.

Owner 2026-08-29: "the rail ... should render the execution instances of the
bot's layer we are looking at, each time we click into the next layer, then
it should move to that historical view for that instance."

A declarative FSM behaviour has no WorkflowCore run behind it -- the page's
own comment records that /runs/history 404s for anything but `validation`,
"so there was structurally nothing for them to fetch", and every bot-family
entry showed an empty rail forever.

But the executions happened and ARE recorded. Measured on this project:
2,012 gate_decide and 3,246 advance_task rows. A gate's instances are its
gate_decide rows; a step's are the advance_task rows that LEFT it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.api import workflows as wf  # noqa: E402


_REPO_ROOT = Path(__file__).resolve().parents[4]


def _pin_repo_source(monkeypatch):
    """Point the behaviour-file lookup at the REPO.

    _project_source_path resolves through the project registry, which under
    pytest points at a temp data dir with no .prism/behaviors tree -- so the
    version read returns None there while working perfectly in the daemon.
    Pinning it keeps the test about version ATTRIBUTION rather than about
    where a data dir happens to sit.
    """
    import prism_service.services.claude_transcripts as ct
    monkeypatch.setattr(ct, "_project_source_path",
                        lambda project: str(_REPO_ROOT))


def test_every_conductor_state_that_calls_a_behaviour_can_be_inverted():
    """The map must mirror get_workflows' linked_workflow_id chain, or a
    layer silently has no instances."""
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    for step_id, behaviour in wf._BEHAVIOUR_FOR_STEP.items():
        assert f'"{behaviour}" if step["id"] == "{step_id}"' in src \
            or step_id == "verify_green_state", (
            f"{step_id} -> {behaviour} is not what get_workflows links")
    # and the inverse is a real bijection
    assert len(wf._STEP_FOR_BEHAVIOUR) == len(wf._BEHAVIOUR_FOR_STEP)


class _Row(dict):
    def __getitem__(self, k):
        return dict.__getitem__(self, k)


class _DB:
    def __init__(self, rows):
        self.rows = rows
        self.sql = ""
        self.args = None

    def execute(self, sql, args=()):
        self.sql, self.args = sql, list(args)
        return self

    def fetchall(self):
        return self.rows


def _ctx(rows):
    import types
    svc = types.SimpleNamespace()
    svc._db = _DB(rows)
    return types.SimpleNamespace(task_svc=svc)


def _row(i, task, actor, action, details):
    return _Row(id=i, task_id=task, actor=actor, action=action,
                details=details, timestamp="2026-08-29T23:49:48")


def test_a_gates_instances_are_its_own_decisions(monkeypatch):
    rows = [
        _row(3, "t1", "conductor-adjudicator", "gate_decide",
             "gate=story_gate; action=approve; validation=story_complete"),
        _row(2, "t1", "conductor", "advance_task",
             "from=story_gate; to=verify_plan"),
        _row(1, "t1", "someone", "gate_decide",
             "gate=plan_gate; action=reject; reason=x"),
    ]
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx(rows))
    out = wf.workflow_instances("story-gate-check", project="prism", task_id="", version=None)
    assert out["step_id"] == "story_gate"
    assert [i["id"] for i in out["instances"]] == ["3"], out["instances"]
    assert out["instances"][0]["outcome"] == "approved"
    assert out["instances"][0]["actor"] == "conductor-adjudicator"


def test_a_rejection_reads_as_rejected(monkeypatch):
    rows = [_row(9, "t1", "a-human", "gate_decide",
                 "gate=green_gate; action=reject; reason=evidence is adjacent")]
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx(rows))
    out = wf.workflow_instances("green-gate-status", project="prism", task_id="", version=None)
    assert out["instances"][0]["outcome"] == "rejected"


def test_a_step_layers_instances_are_the_advances_that_left_it(monkeypatch):
    rows = [
        _row(5, "t1", "conductor", "advance_task",
             "from=draft_story; to=story_gate; gate=pending"),
        _row(4, "t1", "conductor", "advance_task",
             "from=verify_plan; to=plan_gate"),
    ]
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx(rows))
    out = wf.workflow_instances("draft-story-loop", project="prism", task_id="", version=None)
    assert [i["id"] for i in out["instances"]] == ["5"]
    assert out["instances"][0]["outcome"] == "advanced"


def test_drilling_in_from_one_task_scopes_to_THAT_task(monkeypatch):
    """Owner: clicking into the next layer moves to the historical view FOR
    THAT INSTANCE -- not every task's history at that layer."""
    ctx = _ctx([])
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    wf.workflow_instances("story-gate-check", project="prism", task_id="t1", version=None)
    assert "AND task_id = ?" in ctx.task_svc._db.sql
    assert ctx.task_svc._db.args == ["t1"]


def test_an_unknown_layer_returns_no_instances_rather_than_guessing(monkeypatch):
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx([_row(1, "t", "a", "gate_decide", "gate=x")]))
    out = wf.workflow_instances("not-a-behaviour", project="prism", task_id="", version=None)
    assert out["instances"] == []
    assert out["step_id"] == ""


# --------------------------------------------------------------- version
def test_an_instance_belongs_to_the_flow_version_that_ran_it(monkeypatch):
    """Owner 2026-08-29: "it is INSTANCE ran per THIS version of the
    Bot/Agentic flow."

    A run of plan-gate-check v1 (one opaque rubric callback) is not a run of
    v3 (rubric + three teeth + infer). Listing them together misrepresents
    what executed.
    """
    rows = [
        _row(2, "t1", "seat", "gate_decide",
             "gate=plan_gate; action=approve; flow_version=3; reason=ok"),
        _row(1, "t1", "seat", "gate_decide",
             "gate=plan_gate; action=approve; reason=older run, unstamped"),
    ]
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx(rows))
    out = wf.workflow_instances("plan-gate-check", project="prism", task_id="", version=None)
    by_id = {i["id"]: i for i in out["instances"]}
    assert by_id["2"]["flow_version"] == 3
    assert by_id["1"]["flow_version"] is None, (
        "an unstamped run must read as UNKNOWN, never as the current version")
    assert out["unstamped"] == 1


def test_filtering_by_version_never_sweeps_in_unknown_runs(monkeypatch):
    """Guessing would file a v1 execution under a v3 heading."""
    rows = [
        _row(2, "t1", "seat", "gate_decide",
             "gate=plan_gate; action=approve; flow_version=3"),
        _row(1, "t1", "seat", "gate_decide", "gate=plan_gate; action=approve"),
    ]
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx(rows))
    out = wf.workflow_instances("plan-gate-check", project="prism", task_id="", version=3)
    assert [i["id"] for i in out["instances"]] == ["2"]
    out1 = wf.workflow_instances("plan-gate-check", project="prism", task_id="", version=1)
    assert out1["instances"] == [], (
        "an unstamped run was matched against a version it never recorded")


def test_the_response_names_the_definitions_current_version(monkeypatch):
    """The rail needs to say which version the layer IS, to contrast with
    what its instances ran against."""
    monkeypatch.setattr(wf, "get_project", lambda p: _ctx([]))
    # the behaviour files live in the repo, not in the test data dir
    _pin_repo_source(monkeypatch)
    out = wf.workflow_instances("plan-gate-check", project="prism", task_id="", version=None)
    assert out["current_version"] == 3, out


def test_the_inference_seat_stamps_the_version_it_ran(monkeypatch):
    """Nothing stamped this before: 2,016 gate_decide rows on file, zero
    carrying a flow_version. Going forward a seat records it."""
    from prism_service.services import gate_agent as ga
    _pin_repo_source(monkeypatch)
    assert ga._flow_version("prism", "plan_gate") == 3
    assert ga._flow_version("prism", "not_a_gate") is None
    src = (Path(__file__).resolve().parent.parent.parent
           / "prism_service/services/gate_agent.py").read_text(encoding="utf-8")
    assert "flow_version={fv}" in src, "the seat does not stamp its version"
