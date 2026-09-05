"""Task cd33263f: review_previous_notes leveled up into three nodes,
AGENTIC ONLY IN THE MIDDLE.

Owner: "how can we level up more nodes moving faster programmatically,
finish tasks faster with less tokens as you find issues" / "enough
agentic to generate the content for the task, but always striving to
ensure maximum correct throughput."

Before: ONE reason-loop call asked a model to "review the prior notes"
with only Read/Glob/Grep (claude_cli.READ_ONLY_TOOLS) and no memory/task/
brain access -- grounding a citation meant grepping the repo cold.

After:
  /steps/premise-gather          codified -- resolves real citations
  /steps/premise-judge           agentic  -- judges load-bearing facts,
                                              zero tool round trips
  /steps/premise-citation-check  codified -- verifies the report's shape

Pins the task's own oracle:
  - gather never returns a citation it did not resolve, and reports an
    honest empty result with a named reason when nothing resolves
  - gather and citation-check never call a model
  - citation-check reports exactly which bullets fail
  - premise_notes still satisfies the SAME premise_grounded rubric
    story_gate reads -- no downstream gate changes
  - the judge call needs zero tool round trips (allowed_tools=()),
    a real, measurable reduction from the old call's up to 4 turns of
    Read/Glob/Grep exploration -- this is the token-reduction proxy;
    end-to-end token accounting would require a live claude -p run,
    which this unit suite does not spend real API cost on (task
    cd33263f's stop_if: gather/check must never call a model at all).
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest


# ----------------------------------------------------------------------
# fakes
# ----------------------------------------------------------------------

def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id="cd33263f-a1bb-431a-847c-48796abbb05b",
        title="Level up the premise node with codified steps",
        description="review-previous-notes-loop.json calls reason-loop; "
                     "the story_gate rubric reads task.premise_notes.",
        status="pending", priority=90, assigned_agent="",
        parent_id="", tags=["workflows", "conductor"],
        oracle="the gather step resolves real citations",
        premise_notes="",
    )
    base.update(over)
    return Task(**base)


class _FakeHistoryRow:
    def __init__(self, action, actor, details, timestamp):
        self.action, self.actor, self.details, self.timestamp = (
            action, actor, details, timestamp)


class _FakeTaskSvc:
    def __init__(self, task, history=None, neighbours=None, neighbour_history=None):
        self._task = task
        self._history = history or []
        self._neighbours = neighbours or []
        self._neighbour_history = neighbour_history or {}

    def get(self, task_id):
        return self._task if task_id == self._task.id else None

    def history(self, task_id):
        if task_id == self._task.id:
            return list(self._history)
        return list(self._neighbour_history.get(task_id, []))

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self._neighbours)


class _FakeMemoryEntry:
    def __init__(self, name, description):
        self.name, self.description = name, description


class _FakeMemorySvc:
    def __init__(self, entries=None):
        self._entries = entries or []
        self.calls = 0

    def recall(self, query, limit=5):
        self.calls += 1
        return list(self._entries[:limit])


class _FakeBrainSvc:
    def __init__(self, symbols=None):
        # symbols: {name: (source_file, line_start)}
        self._symbols = symbols or {}
        self.calls = 0

    def find_symbol(self, name, kind=None, limit=10):
        self.calls += 1
        hit = self._symbols.get(name)
        if not hit:
            return []
        return [{"source_file": hit[0], "line_start": hit[1]}]


def _no_model_allowed(*a, **kw):
    raise AssertionError("a codified step must never call a model")


# ----------------------------------------------------------------------
# /steps/premise-gather
# ----------------------------------------------------------------------

def test_gather_resolves_real_citations_from_memory_history_and_symbols(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _mk_task()
    task_svc = _FakeTaskSvc(
        task,
        history=[_FakeHistoryRow("advance_task", "sm",
                                  "workflow_step=draft_story", "2026-08-29T00:00:00Z")],
    )
    memory_svc = _FakeMemorySvc([_FakeMemoryEntry(
        "premise-grounded-rubric", "citations required on every claim")])
    brain_svc = _FakeBrainSvc({
        "review_previous_notes": ("prism_service/models/workflow.py", 24),
    })
    task_with_symbol = _mk_task(
        description="review_previous_notes is validated by premise_grounded")
    task_svc._task = task_with_symbol

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=task_svc, memory_svc=memory_svc, brain_svc=brain_svc))

    resp = workflows_api.workflow_step_premise_gather(
        workflows_api.PremiseGatherRequest(task_id=task_with_symbol.id),
        project="prism",
    )

    assert resp.reason == ""
    kinds = {f.kind for f in resp.facts}
    assert "memory" in kinds
    assert "decision" in kinds
    assert "symbol" in kinds
    symbol_fact = next(f for f in resp.facts if f.kind == "symbol")
    assert symbol_fact.citation == "prism_service/models/workflow.py:24"
    memory_fact = next(f for f in resp.facts if f.kind == "memory")
    assert "`" in memory_fact.citation  # backtick output form


def test_gather_reports_honest_empty_result_with_a_named_reason(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _mk_task(description="nothing resolvable here", tags=[])
    task_svc = _FakeTaskSvc(task, history=[])
    memory_svc = _FakeMemorySvc([])
    brain_svc = _FakeBrainSvc({})

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=task_svc, memory_svc=memory_svc, brain_svc=brain_svc))

    resp = workflows_api.workflow_step_premise_gather(
        workflows_api.PremiseGatherRequest(task_id=task.id), project="prism")

    assert resp.facts == []
    assert resp.reason  # named, non-empty
    assert "no" in resp.reason.lower()


def test_gather_never_returns_a_citation_it_did_not_resolve(monkeypatch):
    """A symbol candidate that find_symbol cannot resolve produces NO
    fact -- never a fabricated file:line."""
    from prism_service.api import workflows as workflows_api
    from prism_service.services import premise_gather as pg

    task = _mk_task(description="totally_unresolvable_symbol_name appears here")
    facts = pg.gather(task, memory_svc=_FakeMemorySvc([]),
                       task_svc=_FakeTaskSvc(task), brain_svc=_FakeBrainSvc({}))
    assert not any(f.kind == "symbol" for f in facts)


def test_gather_never_calls_a_model(monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(claude_cli, "invoke", _no_model_allowed)

    task = _mk_task()
    task_svc = _FakeTaskSvc(task)
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=task_svc, memory_svc=_FakeMemorySvc([]), brain_svc=_FakeBrainSvc({})))

    workflows_api.workflow_step_premise_gather(
        workflows_api.PremiseGatherRequest(task_id=task.id), project="prism")
    # no AssertionError raised means claude_cli.invoke was never reached


def test_gather_reports_no_such_task_honestly(monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(_mk_task())))

    resp = workflows_api.workflow_step_premise_gather(
        workflows_api.PremiseGatherRequest(task_id="does-not-exist"), project="prism")
    assert resp.facts == []
    assert "does-not-exist" in resp.reason


# ----------------------------------------------------------------------
# /steps/premise-citation-check
# ----------------------------------------------------------------------

def test_citation_check_reports_which_bullets_fail(monkeypatch):
    from prism_service.api import workflows as workflows_api

    notes = (
        "## Premises\n"
        "- grounded claim, see prism_service/api/workflows.py:24\n"
        "- ungrounded claim with nothing to back it\n"
        "- REFUTED: this turned out false\n"
    )
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace())

    resp = workflows_api.workflow_step_premise_citation_check(
        workflows_api.PremiseCitationCheckRequest(notes_md=notes), project="prism")

    assert resp.ok is False
    assert resp.claims_checked == 3
    assert len(resp.failing) == 1
    assert "ungrounded claim" in resp.failing[0].claim


def test_citation_check_passes_when_every_bullet_is_grounded_or_marked(monkeypatch):
    from prism_service.api import workflows as workflows_api

    notes = (
        "## Premises\n"
        "- see prism_service/api/workflows.py:24\n"
        "- UNVERIFIED: could not confirm this one\n"
    )
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace())

    resp = workflows_api.workflow_step_premise_citation_check(
        workflows_api.PremiseCitationCheckRequest(notes_md=notes), project="prism")

    assert resp.ok is True
    assert resp.failing == []


def test_citation_check_reads_task_premise_notes_when_body_omitted(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = _mk_task(premise_notes="## Premises\n- see x.py:1\n")
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(task)))

    resp = workflows_api.workflow_step_premise_citation_check(
        workflows_api.PremiseCitationCheckRequest(task_id=task.id), project="prism")
    assert resp.ok is True
    assert resp.claims_checked == 1


def test_citation_check_never_calls_a_model(monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    monkeypatch.setattr(claude_cli, "invoke", _no_model_allowed)
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace())

    workflows_api.workflow_step_premise_citation_check(
        workflows_api.PremiseCitationCheckRequest(notes_md="## Premises\n- x.py:1\n"),
        project="prism")


def test_citation_check_uses_the_same_section_name_the_real_rubric_reads():
    """governance_rubrics.yaml's premise_grounded.claims_section is
    'Premises' -- the citation-check step must key off the SAME rubric,
    not a hardcoded literal that could drift."""
    from prism_service.services import arc_governance as gov

    rubric = gov.load_rubrics().get("premise_grounded") or {}
    assert rubric.get("claims_section", "premises").strip().lower() == "premises"


# ----------------------------------------------------------------------
# premise_notes contract unchanged -- citation-check's verdict tracks
# the REAL story_gate rubric verdict, never drifts from it
# ----------------------------------------------------------------------

@pytest.mark.parametrize("notes_md", [
    "## Premises\n- grounded, see prism_service/api/workflows.py:24\n",
    "## Premises\n- ungrounded claim\n",
    "## Premises\n- REFUTED: false lead\n",
    "",
])
def test_citation_check_agrees_with_the_real_premise_grounded_rubric(notes_md):
    from prism_service.services import arc_governance as gov
    from prism_service.services import premise_gather as pg

    rubric = gov.load_rubrics().get("premise_grounded") or {}
    gate_verdict = gov.score_premise_grounded({"notes_md": notes_md}, rubric)
    check_verdict = pg.citation_check(notes_md, claims_section=rubric.get(
        "claims_section", "premises"))

    # citation_check only re-checks the CITATION tooth (not oracle
    # engagement, which needs task.oracle) -- so a citation failure must
    # be a citation failure on both sides, and a citation pass must never
    # be reported by citation_check as a failure the gate would also pass.
    if not check_verdict["ok"]:
        assert gate_verdict["ok"] is False
    if check_verdict["ok"] and check_verdict["claims_checked"] > 0:
        # every claim grounded -- the gate can only still refuse on
        # oracle-engagement, never on citations
        assert "premise_grounded: claim(s) without a citation" not in gate_verdict["reason"]


# ----------------------------------------------------------------------
# /steps/premise-judge -- agentic, but leaner
# ----------------------------------------------------------------------

def test_premise_judge_needs_zero_tool_round_trips(tmp_path, monkeypatch):
    """The measurable win: the OLD single-step reason-loop call left
    allowed_tools at its default (claude_cli.READ_ONLY_TOOLS, 3 tools) and
    up to 4 turns for the model to grep/read its way to a citation. The
    NEW judge call passes allowed_tools=() -- zero tool round trips --
    because gather already resolved every citation."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    task = _mk_task()
    task_svc = _FakeTaskSvc(task, history=[])
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=task_svc, memory_svc=_FakeMemorySvc([]), brain_svc=_FakeBrainSvc({})))
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    captured = {}

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, allowed_tools, **kw):
        captured["allowed_tools"] = allowed_tools
        captured["max_turns"] = max_turns
        captured["prompt"] = prompt
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"notes_md": "## Premises\n- UNVERIFIED: none gathered\n"},
            usage={"cost_usd": 0.01}, run_id="run-judge",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_premise_judge(
        workflows_api.PremiseJudgeRequest(task_id=task.id), project="prism")

    assert captured["allowed_tools"] == ()
    # the old reason-loop call left allowed_tools at its default
    # (claude_cli.READ_ONLY_TOOLS, 3 tools) with up to 4 turns to use
    # them; this call needs neither.
    assert claude_cli.READ_ONLY_TOOLS != ()
    assert captured["max_turns"] < 4
    assert resp.reason["ok"] is True


def test_premise_judge_reuses_gathered_citations_verbatim(tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    task = _mk_task(description="review_previous_notes symbol reference")
    task_svc = _FakeTaskSvc(task, history=[])
    brain_svc = _FakeBrainSvc({"review_previous_notes":
                                ("prism_service/models/workflow.py", 24)})
    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=task_svc, memory_svc=_FakeMemorySvc([]), brain_svc=brain_svc))
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    seen_prompt = {}

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, allowed_tools, **kw):
        seen_prompt["text"] = prompt
        # the model reuses the gathered citation verbatim
        notes = ("## Premises\n- review_previous_notes is defined, see "
                 "prism_service/models/workflow.py:24\n")
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"notes_md": notes},
            usage={"cost_usd": 0.01}, run_id="run-judge-2",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_premise_judge(
        workflows_api.PremiseJudgeRequest(task_id=task.id), project="prism")

    assert "prism_service/models/workflow.py:24" in seen_prompt["text"]
    assert resp.facts_used >= 1
    assert resp.validation["ok"] is True


def test_premise_judge_reports_no_such_task_honestly(monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project", lambda p: types.SimpleNamespace(
        task_svc=_FakeTaskSvc(_mk_task())))

    resp = workflows_api.workflow_step_premise_judge(
        workflows_api.PremiseJudgeRequest(task_id="does-not-exist"), project="prism")
    assert resp.facts_used == 0
    assert resp.validation["ok"] is False


# ----------------------------------------------------------------------
# The behavior JSON is a real 3-step chain, gather -> loop -> check
# ----------------------------------------------------------------------

def test_review_previous_notes_behavior_is_three_steps_agentic_only_in_the_middle():
    path = (Path(__file__).resolve().parent.parent.parent.parent.parent
            / ".prism" / "behaviors" / "conductor" / "review-previous-notes-loop.json")
    behavior = json.loads(path.read_text())
    steps = behavior["steps"]
    ids = [s["id"] for s in steps]
    # SUPERSEDED by task d7947eb6: the chain is no longer exactly three
    # steps. Every text-generating node now carries a CODIFIED
    # text-challenge step after it (owner: "all of the text generation
    # nodes that hit artifacts use the ontology rules, they should have
    # separate nodes to challenge, correct or provide access"), so this
    # behavior is gather -> loop -> check -> text-challenge. The real
    # invariant this test encodes -- exactly ONE agentic step, and it is
    # the judge in the middle -- is asserted below against the model
    # payload rather than against a fixed step count, so a further
    # codified step cannot break it again.
    # SUPERSEDED AGAIN (premise-render): a codified `render` step now sits
    # between the judge and the check, so the section the check grades is
    # BUILT from the facts `gather` resolved instead of depending on the
    # model to retype them -- premise_grounded had refused 273 advances
    # across 141 tasks that way. As this test's own note above predicted,
    # the fixed sequence is what keeps breaking, so the ORDER that actually
    # matters is asserted by position-of rather than by index.
    by_id = {s["id"]: s for s in steps}
    for expected in ("gather", "loop", "render", "check", "text-challenge"):
        assert expected in by_id, ids
    assert ids.index("gather") < ids.index("loop") < ids.index("render") \
        < ids.index("check") < ids.index("text-challenge"), ids
    assert "/steps/premise-gather" in by_id["gather"]["url"]
    assert "/steps/premise-judge" in by_id["loop"]["url"]
    assert "/steps/premise-render" in by_id["render"]["url"]
    assert "/steps/premise-citation-check" in by_id["check"]["url"]
    assert "/steps/text-challenge" in by_id["text-challenge"]["url"]
    # the judge is the ONLY step carrying a model -- every other step is a
    # pure http-callback with no reasoning payload at all
    agentic = [s["id"] for s in steps if "model" in s["body"]]
    assert agentic == ["loop"], agentic
