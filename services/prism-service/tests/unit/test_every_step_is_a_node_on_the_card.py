"""Task 25b2a05c: every WORKFLOW_STEPS entry gets exactly one node on the
conductor canvas, and that node declares an honest kind.

Owner 2026-08-30: "no node no programmatic? we track this like golf. but
how can we see if you're hiding" then "make them agree." MEASURED: 10
WORKFLOW_STEPS (models/workflow.py) vs. 11 behaviorIds in
.prism/behaviors/conductor/bot.json's `pipeline` FSM — the two lists did
not describe the same pipeline. verify_green_state (agent, role=qa, the
step whose live job is "Verify full green against a real run" —
api/conductor_flow.py's `_GUIDE`) had no behaviorId of its own: instead
api/workflows.py's `get_workflows()` gave it `linked_workflow_id =
"validation"`, a completely different catalog entry (the external
AosWorkflows scripted build+test workflow), so the step read UNSCORED —
no node, no declared kind, no row on the scoreboard — despite carrying the
third-largest spend of any step. This is a SCORECARD, not a second FSM: it
reads WORKFLOW_STEPS and the real `.prism/behaviors/conductor/*.json`
files straight off disk, never a hand-maintained mirror of either side
(stop_if: "The check compares a list against itself instead of against
the Behavior files on disk").
"""

from __future__ import annotations

import json
import re
import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_REPO_ROOT = _SERVICE_ROOT.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_BEHAVIORS = _REPO_ROOT / ".prism" / "behaviors" / "conductor"
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"

# The one behaviorId this task's own premises (grounded against
# api/workflows.py:836-848 and :1270-1279, both already on file before this
# task) record as DELIBERATELY outside the pipeline: no WORKFLOW_STEPS
# entry triggers it, a developer runs it by hand. Adding a new undocumented
# id here is not this test's job — a real addition must extend THIS
# allowlist deliberately, which is exactly the "recorded decision" the
# task's own stop_if asks for.
_DELIBERATELY_UNLINKED_PIPELINE_IDS = frozenset({
    "ci-local-dev",
    # red-test-ids (task 404ef4ce, landed on main while this branch was in
    # flight). A CODIFIED helper that names which of task.verify's pinned
    # targets are demonstrated red at the task's red anchor, from data on
    # file, no model involved. It serves implement_tasks rather than being
    # its own WORKFLOW_STEPS entry, so it is deliberately unlinked - and
    # recording that here IS the decision this test demands, rather than a
    # way around it. This test caught the gap the moment the two branches
    # met, which is what it exists for.
    "red-test-ids",
})

# The behaviorIds that nest under conductor WITHOUT a WORKFLOW_STEPS
# linked_workflow_id of their own, because green_gate is the FSM's
# structurally-terminal step (14+ call sites in conductor_service.py, a
# control_plane.POLICY_FILES entry, treat it as literally last — neither
# task touches that file or inserts a step after it).
#
# SUPERSEDED (task f97c196d): this was the single id "land". `reap` is the
# step AFTER land — it removes the drive's git worktree and its
# prism/ws/<task_id> branch once the work is really on origin/main — and it
# nests through the SAME mechanism for the same reason, so the constant is
# now the pair. `land` alone is no longer the last pipeline node.
_TERMINAL_PIPELINE_IDS = frozenset({"land", "reap"})
_TERMINAL_PIPELINE_ID = "land"


def _bot_json() -> dict:
    path = _BEHAVIORS / "bot.json"
    assert path.exists(), f"expected {path} to exist"
    return json.loads(path.read_text(encoding="utf-8"))


def _pipeline_behavior_ids() -> list[str]:
    bot = _bot_json()
    fsms = bot.get("fsms") or []
    pipeline = next((f for f in fsms if f.get("fsmId") == "pipeline"), None)
    assert pipeline is not None, "bot.json must carry a 'pipeline' fsm"
    return list(pipeline.get("behaviorIds") or [])


def _behavior_file(behavior_id: str) -> dict:
    path = _BEHAVIORS / f"{behavior_id}.json"
    assert path.exists(), f"expected a real behavior file at {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# A step's body carrying one of these keys only makes sense when a model is
# actually being invoked -- detected from what the step's own PAYLOAD
# carries, never from matching its url against a known endpoint name like
# "/reason-loop". A URL-substring check silently OVER-reports codified
# nodes and would let any future agentic step evade the card just by
# calling a differently-named endpoint: caught live on this same task
# (team lead, 2026-08-30) when a URL-only check misclassified task
# cd33263f's premise-judge step as codified, even though its body carries
# model/persona/prompt/max_budget_usd/max_turns -- it simply does not
# route through /reason-loop. See test_kind_detector_reports_agentic_from_
# body_not_endpoint_name below for the regression pin.
_MODEL_REACHING_BODY_KEYS = frozenset(
    {"model", "persona", "prompt", "max_budget_usd", "max_turns"})


def _step_reaches_a_model(step: dict) -> bool:
    """True when this step's own body carries a model-reaching field --
    the honest signal that a node is AGENTIC, independent of its URL."""
    try:
        body = json.loads(step.get("body") or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(body, dict):
        return False
    return bool(_MODEL_REACHING_BODY_KEYS & set(body.keys()))


def _read_source(*parts: str) -> str:
    path = _SERVICE_ROOT.joinpath(*parts) if parts[0] != "web" else _WEB.joinpath(*parts[1:])
    assert path.exists(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


class _Svc:
    """Minimal task_svc stand-in — get_workflows() only ever lists."""

    def __init__(self, tasks=()):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _scripted_validation(project="prism"):
    return {
        "id": "validation", "name": "Build and test",
        "description": f"{project} validation", "project_type": "python+react",
        "steps": [], "bots": [], "occupancy": {},
    }


def _get_workflows_body(monkeypatch):
    """A real call to api.workflows.get_workflows() with only the two
    external-engine seams stubbed (the AosWorkflows HTTP calls this test
    has no business making) — every WORKFLOW_STEPS/linked_workflow_id/
    _CONDUCTOR_LINKED_BEHAVIOR_IDS/_BEHAVIOR_TRIGGER value comes from the
    REAL function under test, never reimplemented here."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=_Svc()))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)

    def _fake_conductor_behaviors(project):
        return [{"id": i, "name": i, "steps": [], "bots": [], "occupancy": {}}
                for i in _pipeline_behavior_ids()]
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        _fake_conductor_behaviors)
    return workflows_api.get_workflows("prism")


def test_every_workflow_step_has_a_real_node(monkeypatch):
    """AC-1: every WORKFLOW_STEPS id resolves to a real node — a
    linked_workflow_id that is itself a real pipeline behaviorId, or the
    one documented terminal-step exception (green_gate -> land, nested via
    _CONDUCTOR_LINKED_BEHAVIOR_IDS rather than a per-step link)."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    body = _get_workflows_body(monkeypatch)
    steps_by_id = {s["id"]: s for s in body["steps"]}
    pipeline_ids = set(_pipeline_behavior_ids())

    unscored = []
    for step in WORKFLOW_STEPS:
        sid = step["id"]
        linked = steps_by_id[sid].get("linked_workflow_id")
        if sid == "green_gate" and not linked:
            # The one documented exception: terminal step, nests directly.
            assert _TERMINAL_PIPELINE_ID in pipeline_ids
            continue
        if not linked or linked not in pipeline_ids:
            unscored.append(sid)
    assert not unscored, (
        f"WORKFLOW_STEPS id(s) with no real pipeline node: {unscored}")


def test_verify_green_state_node_is_honestly_agentic(monkeypatch):
    """AC-2: verify_green_state's own node is verify-green-state-loop, and
    that node's kind is genuinely agentic — detected from what its body
    actually carries (a model-reaching field), never from matching its url
    against a known endpoint name — never a codified label pasted over a
    step that still calls a model."""
    body = _get_workflows_body(monkeypatch)
    steps_by_id = {s["id"]: s for s in body["steps"]}

    linked = steps_by_id["verify_green_state"].get("linked_workflow_id")
    assert linked == "verify-green-state-loop", (
        f"verify_green_state must link to its own node, got {linked!r}")
    assert linked in _pipeline_behavior_ids()

    behavior = _behavior_file("verify-green-state-loop")
    steps = behavior.get("steps") or []
    assert steps, "verify-green-state-loop.json must declare at least one step"
    for step in steps:
        assert step.get("kind") == "http-callback", (
            "an honest agentic node calls out, never runs in-process")
    # SUPERSEDED by task d7947eb6: this used to require EVERY step in the
    # behavior to reach a model. The node now ends with a CODIFIED
    # text-challenge step that judges the completion proof against the
    # live ontology rules at zero tokens, so an all-steps-are-agentic
    # assertion would forbid exactly the kind of step this project is
    # converting agentic work into. The real invariant -- the GENERATING
    # step is honestly agentic, never a codified label pasted over a step
    # that still calls a model -- is asserted per step below, and the
    # challenge step must reach NO model at all.
    for step in steps:
        if step.get("id") == "text-challenge":
            assert not _step_reaches_a_model(step), (
                "the text-challenge step must be codified: its body carries "
                f"a model-reaching field: {step.get('body')!r}")
            continue
        assert _step_reaches_a_model(step), (
            "verify-green-state-loop's step body carries no model-reaching "
            f"field (model/persona/prompt/max_budget_usd/max_turns) — a "
            f"false agentic label with a url that merely looks right: "
            f"{step.get('body')!r}")
    assert any(_step_reaches_a_model(s) for s in steps), (
        "verify-green-state-loop has no agentic step left at all")


def test_kind_detector_reports_agentic_from_body_not_endpoint_name():
    """Regression for a real live mistake caught on this same task (team
    lead, 2026-08-30): a URL-substring check ("does the url contain
    /reason-loop") reported task cd33263f's premise-judge step as
    codified, because it reaches a model through a differently-named
    endpoint (/api/workflows/steps/premise-judge). The detector must
    classify by BODY CONTENT, so a synthetic step with a brand-new
    endpoint name but a model-reaching body is still caught as agentic —
    and a synthetic step whose URL merely LOOKS agentic but whose body
    carries only identifiers is not falsely flagged either."""
    agentic_synthetic = {
        "kind": "http-callback",
        "url": "${prismBackendUrl}/api/workflows/steps/a-brand-new-endpoint-name?project=${project}",
        "body": json.dumps({"persona": "qa", "prompt": "judge this",
                            "model": "haiku", "max_budget_usd": 0.5,
                            "max_turns": 2, "task_id": "${taskId}"}),
    }
    codified_synthetic = {
        "kind": "http-callback",
        "url": "${prismBackendUrl}/api/workflows/steps/reason-loop?project=${project}",
        "body": json.dumps({"task_id": "${taskId}"}),
    }
    assert _step_reaches_a_model(agentic_synthetic), (
        "a body carrying model/persona/prompt must be detected as agentic "
        "regardless of its endpoint name")
    assert not _step_reaches_a_model(codified_synthetic), (
        "a body carrying only identifiers must not be detected as agentic "
        "even when its url happens to contain reason-loop")


def test_every_pipeline_node_maps_to_a_step_or_is_recorded_as_deliberate():
    """AC-3: every bot.json pipeline behaviorId either names a real
    WORKFLOW_STEPS id (directly, or via the recorded exceptions above) or
    is in the small, explicit allowlist this file itself owns — a stray,
    undocumented addition fails loudly instead of being silently ignored."""
    from prism_service.models.workflow import WORKFLOW_STEPS

    step_ids = {s["id"] for s in WORKFLOW_STEPS}
    pipeline_ids = _pipeline_behavior_ids()

    # linked_workflow_id values a real WORKFLOW_STEPS entry can carry —
    # read straight off api/workflows.py's own source, never a duplicate
    # hand-maintained mapping (this IS the same "if step['id'] == ..."
    # ladder get_workflows() evaluates; scanning it, rather than importing
    # a private local, keeps this test from inventing a second source of
    # truth for the same ladder).
    api_src = _read_source("prism_service", "api", "workflows.py")
    linked_ids = set(re.findall(r'"([a-z-]+)" if step\["id"\] ==', api_src))

    unaccounted = []
    for behavior_id in pipeline_ids:
        if behavior_id in linked_ids:
            continue
        if behavior_id in _TERMINAL_PIPELINE_IDS:
            continue
        if behavior_id in _DELIBERATELY_UNLINKED_PIPELINE_IDS:
            continue
        unaccounted.append(behavior_id)
    assert not unaccounted, (
        f"pipeline node(s) with no step link and no recorded decision: {unaccounted}")
    # And the reverse direction: every linked_workflow_id the ladder can
    # produce must itself be a real pipeline node — a stale mapping to a
    # deleted behavior file would otherwise pass silently.
    stray_links = linked_ids - set(pipeline_ids)
    assert not stray_links, f"linked_workflow_id(s) with no pipeline node: {stray_links}"
    assert step_ids  # WORKFLOW_STEPS is non-empty; guards the import above


def test_scorecard_reads_the_real_behavior_files_not_a_mirrored_list():
    """AC-4: this suite's own data sources are the real files on disk, not
    a second hand-copied list. Reads bot.json fresh (no caching across
    calls) and cross-checks its content against the individual behavior
    files it names — a scorecard that only compared two copies of the
    same literal list would pass even if a real file went missing."""
    first_read = _pipeline_behavior_ids()
    second_read = _pipeline_behavior_ids()
    assert first_read == second_read, "two independent disk reads must agree"
    assert first_read, "the pipeline behaviorIds list must not be empty"
    for behavior_id in first_read:
        # A real file must back every declared id — this is what makes the
        # check "against the Behavior files on disk", not against bot.json
        # alone.
        path = _BEHAVIORS / f"{behavior_id}.json"
        assert path.exists(), (
            f"bot.json names {behavior_id!r} but no behavior file backs it")


def test_frontend_has_no_stale_validation_fallback_for_verify_green_state():
    """AC-5: the three `?? "validation"` fallbacks that existed only
    because verify_green_state previously had no real linked_workflow_id
    of its own are gone — the backend now always supplies a real id, so a
    surviving fallback would be dead code implying a false destination."""
    page = _read_source("web", "pages", "WorkflowsPage.tsx")
    assert '?? "validation"' not in page, (
        "a stale validation fallback survives verify_green_state's own node")


def test_stale_validation_link_assertions_were_superseded():
    """AC-6: test_workflows_section_ui.py's two literal-string assertions
    that pinned the OLD "validation" link are superseded in place with a
    comment naming this task — never silently deleted, never left to
    contradict the new wiring."""
    page = _read_source("tests", "unit", "test_workflows_section_ui.py")
    assert "superseded by task 25b2a05c" in page, (
        "the retired assertions must be marked with what replaced them")
    assert '"validation" if step["id"] == "verify_green_state"' not in page
    assert 'linked_workflow_id: step.linked_workflow_id ?? "validation"' not in page


def test_land_is_recorded_as_the_pipeline_terminal(monkeypatch):
    """AC-3 (land half): land is the FSM's real terminal step and nests
    under conductor even though green_gate carries no linked_workflow_id
    of its own — grounded against the SAME _CONDUCTOR_LINKED_BEHAVIOR_IDS
    set get_workflows() actually uses, read via a real call, not a
    duplicate constant."""
    assert _TERMINAL_PIPELINE_ID in _pipeline_behavior_ids()
    body = _get_workflows_body(monkeypatch)
    by_id = {w["id"]: w for w in body["workflows"]}
    assert by_id[_TERMINAL_PIPELINE_ID].get("parent_id") == "conductor"


def test_ci_local_dev_is_deliberately_outside_the_pipeline():
    """AC-3 (ci-local-dev half): recorded, not silently dropped — the
    trigger text on file for it says a person runs it by hand, matching
    this test's own allowlist."""
    # SUPERSEDED 2026-08-30: this pinned the allowlist to exactly one entry.
    # Task 404ef4ce landed red-test-ids on main while this branch was in
    # flight - a second, genuinely deliberate unlinked node. The EQUALITY
    # check is kept rather than loosened to a membership test, because it is
    # the guard that stops anyone quietly dumping nodes into the allowlist to
    # make the card pass: every addition must be reviewed and written here on
    # purpose. Expanded, not weakened.
    assert _DELIBERATELY_UNLINKED_PIPELINE_IDS == frozenset(
        {"ci-local-dev", "red-test-ids"})
    api_src = _read_source("prism_service", "api", "workflows.py")
    assert '"ci-local-dev":' in api_src
    assert "stays unparented" in api_src
