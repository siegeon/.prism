"""The plan step is explicit: PRISM composes, the model fills typed slots,
PRISM renders and validates at the step (owner 2026-09-14: "we keep leaving
this up to the model ... be very explicit about what is supposed to happen;
the explicit comes from the planning steps").

The frozen round on e40c3efb: three plans, three refusals, the same missing
words each time. The words are now never the model's to choose.

Pins:
- AC-1  plan-compose states the pinned test, its colour, the fixed AC-1,
        the files in scope and the lexicon's terms.
- AC-2  the render inserts the pinned red_at_base AC when the model omits
        it, numbers the rest, writes the exact `(RED at base: <id>)` and
        `(regression guard, stays green)` shapes, and >= 2 diagram edges.
- AC-3  the plan_structured rubric refuses at the step with a named reason,
        and passes a rendered plan through form_complete + coverage.
- AC-4  verify-plan-loop.json v9: recall, colour, compose, loop, challenge;
        the loop schema types acs with the colour enum and uses the rubric.
- AC-5  the runner ends a refused verify_plan attempt with the refusal and
        never falls back to the inline prompt.
- AC-6  the lexicon declares Oracle, RedAtBase and RegressionGuard.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from prism_service.api import workflows as wf
from prism_service.services import plan_gate_checks as pgc
from prism_service.services import task_runner

_ROOT = Path(__file__).resolve().parents[4]
_NODE = _ROOT / ".prism" / "behaviors" / "conductor" / "verify-plan-loop.json"
_LEX = _ROOT / "services" / "prism-service" / "prism_service" / "ontology" / "model-lexicon.ttl"
_PIN = "services/prism-service/tests/unit/test_a_task_records_what_it_cost.py"


# AC-1 ------------------------------------------------------------------
def test_the_frame_states_the_facts_and_the_fixed_ac():
    frame = wf.plan_frame_text("A task records what it cost to play", _PIN,
                               "absent", "961105ac", ["a.py", "b.py"], "Drive one task")
    assert "PINNED TEST: " + _PIN in frame and "does not exist at base" in frame
    assert "AC-1 IS FIXED BY PRISM: colour red_at_base" in frame
    assert "FILES IN SCOPE (name only these, they exist): a.py, b.py" in frame
    assert "TERMS (from the PRISM lexicon)" in frame and "red_at_base" in frame


# AC-2 ------------------------------------------------------------------
def test_the_render_inserts_the_pinned_red_ac_and_writes_the_teeth_shape():
    task = SimpleNamespace(title="A task records what it cost to play")
    fields = {"goal": "Bill tokens per step.",
              "acs": [{"text": "Codified nodes still record 0", "colour": "guard",
                       "oracle": "agent_runs shows model=machine rows at 0"}],
              "files_to_change": ["services/prism-service/prism_service/services/agent_runs.py"]}
    out = wf._render_structured_plan(fields, task, _PIN, "absent")
    doc, diag = out["plan_doc"], out["plan_diagram"]
    assert f"- AC-1: The pinned suite {_PIN} exists and passes after the fix (RED at base: {_PIN})" in doc
    assert f"  - oracle: pytest {_PIN}" in doc
    assert "- AC-2: Codified nodes still record 0 (regression guard, stays green)" in doc
    assert pgc.form_complete(doc, diag) == ""
    assert pgc.plan_diagram_parses(diag) == ""
    assert pgc._RED_RE.search(doc) and pgc._GUARD_RE.search(doc)
    assert len(pgc._EDGE_RE.findall(diag)) >= 2


def test_the_render_keeps_a_model_supplied_red_ac_first_and_normalises_colours():
    task = SimpleNamespace(title="t")
    fields = {"goal": "g", "acs": [
        {"text": "guard first", "colour": "GUARD", "oracle": "o1"},
        {"text": "the pinned one", "colour": "red_at_base", "oracle": "pytest x", "pytest_id": _PIN}],
        "files_to_change": []}
    out = wf._render_structured_plan(fields, task, _PIN, "absent")
    assert [a["colour"] for a in out["acs"]] == ["guard", "red_at_base"]
    assert "(RED at base: " + _PIN + ")" in out["plan_doc"]


# AC-3 ------------------------------------------------------------------
def _ctx(monkeypatch, task):
    ctx = SimpleNamespace(task_svc=SimpleNamespace(get=lambda tid: task), memory_svc=None)
    monkeypatch.setattr(wf, "get_project", lambda project: ctx)
    monkeypatch.setattr(pgc, "repo_root_for", lambda t, p: Path("/tmp"))
    monkeypatch.setattr(pgc, "base_ref_for", lambda t, r: "961105acaaaa")
    import prism_service.api.tasks as _tasks
    monkeypatch.setattr(_tasks, "_git", lambda repo, *a: (128, ""))  # pinned absent
    return ctx


def test_the_rubric_refuses_an_empty_draft_with_a_named_reason(monkeypatch):
    task = SimpleNamespace(id="e40c3efb", title="t", verify=[_PIN])
    _ctx(monkeypatch, task)
    out = wf._score_rubric("plan_structured", {"goal": "", "acs": [], "files_to_change": []},
                           "prism", task_id="e40c3efb")
    # PRISM inserted AC-1 itself, so the only refusal left is coverage/none
    assert out["reason"].startswith("plan_structured:")


def test_the_rubric_passes_a_filled_draft_and_renders_the_fields(monkeypatch):
    task = SimpleNamespace(id="e40c3efb", title="A task records what it cost", verify=[_PIN])
    _ctx(monkeypatch, task)
    fields = {"goal": "Bill tokens per step.",
              "acs": [{"text": "Machine rows stay 0", "colour": "guard", "oracle": "agent_runs rows"}],
              "files_to_change": ["services/prism-service/prism_service/services/agent_runs.py"]}
    out = wf._score_rubric("plan_structured", fields, "prism", task_id="e40c3efb")
    assert out["ok"] is True, out
    assert "RED at base: " + _PIN in fields["plan_doc"]
    assert fields["plan_diagram"].startswith("flowchart TD")


# AC-4 ------------------------------------------------------------------
def test_the_node_is_v9_with_compose_and_the_typed_schema():
    node = json.loads(_NODE.read_text(encoding="utf-8"))
    assert [s["id"] for s in node["steps"]] == ["recall", "colour", "compose", "loop", "text-challenge"]
    assert node["version"] >= 9
    compose = node["steps"][2]
    assert "/api/workflows/steps/plan-compose" in compose["url"]
    assert "${colour}" in compose["body"] and "${base}" in compose["body"]
    loop = json.loads(node["steps"][3]["body"].replace("${refusalBlock}", "R")
                      .replace("${planFrame}", "F").replace("${taskHint}", "T")
                      .replace("${taskId}", "I"))
    assert loop["rubric"] == "plan_structured"
    acs = loop["json_schema"]["properties"]["acs"]
    assert acs["items"]["properties"]["colour"]["enum"] == ["red_at_base", "guard"]
    assert "plan_doc" not in loop["json_schema"]["properties"], "PRISM renders the document"
    assert "${planFrame}" in node["steps"][3]["body"]


# AC-5 ------------------------------------------------------------------
def test_a_refused_chain_result_is_a_failed_attempt_with_the_reason():
    r = task_runner._CodifiedResult("", refusal="plan_structured: no acceptance criteria")
    assert r.exit_code == 1 and r.refusal.startswith("plan_structured")
    assert "verify_plan" in task_runner._NO_INLINE_FALLBACK_AFTER_REFUSAL
    assert "plan-compose" in task_runner._step_handlers()
    src = Path(task_runner.__file__).read_text(encoding="utf-8")
    assert 'getattr(result, "refusal", "") or _failure_reason(' in src


# AC-6 ------------------------------------------------------------------
def test_the_lexicon_declares_the_planning_terms():
    ttl = _LEX.read_text(encoding="utf-8")
    for term in ("term/oracle", "term/red-at-base", "term/regression-guard"):
        assert f"<urn:prism:onto:{term}> a o:Term" in ttl
    import rdflib
    g = rdflib.Graph(); g.parse(data=ttl, format="turtle")
    labels = {str(o) for o in g.objects(None, rdflib.RDFS.label)}
    assert {"Oracle", "RedAtBase", "RegressionGuard"} <= labels
