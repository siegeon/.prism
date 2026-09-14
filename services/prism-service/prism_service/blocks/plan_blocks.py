"""plan_gate multiplier blocks (task a65c66e5 lineage, owner 2026-09-13/14:
"a FORM failure is the planner's to fix" -- no gate parks for a person when
the machine can act).

One typed block today: `plan.form_check` wraps
`plan_gate_checks.form_complete` so the deterministic tooth that used to
be invisible Python is a registered, run-counted unit like every other
CHECKS entry could become. It is not a NEW behaviour -- `form_complete`
already runs on every `plan_gate_checks.run_all`/`refusal()` call (the
path `gate_adjudicator`'s `_hold` short-circuit and `plan_rewind.
maybe_rewind` both already take); this block gives that same call a
route id a future catalog entry can point a node at.

The full pre-plan multiplier chain the owner asked for (targets_from_
story / context_pack / prompt_compose / materialize, mirroring
red_blocks.py's red.* quartet) needs new workflow_step_* endpoints in
api/workflows.py plus a verify-plan-loop.json rewrite -- out of scope for
this landing; tracked as follow-up so the drop is not silently narrowed.
"""
from prism_service.blocks import Block, register_block


def _run_plan_form_check(project: str, task_id: str = "", plan_doc: str = "",
                         plan_diagram: str = "") -> dict:
    from prism_service.services import plan_gate_checks as _pgc
    reason = _pgc.form_complete(plan_doc, plan_diagram)
    return {"ok": not reason, "reason": reason}


PLAN_FORM_CHECK_BLOCK = Block(
    id="plan.form_check",
    title="Score the plan packet's form (oracle lines, diagram edges)",
    kind="deterministic",
    owner_seat="plan_gate_checks",
    scope="task",
    on_failure="continue",
    inputs=["task.plan_doc", "task.plan_diagram"],
    outputs=["ok", "reason"],
    description=(
        "Zero-model-call form scoring of a plan packet: every AC must "
        "carry an `oracle:` line, and plan_diagram must parse with at "
        "least two edges. A non-empty reason is what "
        "gate_adjudicator.py's `_hold` short-circuit reads to skip the "
        "certainty seat and what plan_rewind.maybe_rewind reads to "
        "rewind the task to verify_plan instead of escalating a form "
        "defect to the owner."),
)
register_block(PLAN_FORM_CHECK_BLOCK, _run_plan_form_check)


def _run_plan_refusal_recall(project: str, task_id: str = "") -> dict:
    from prism_service.api import workflows as _wf
    resp = _wf.workflow_step_plan_refusal_recall(
        _wf.PlanRefusalRecallRequest(task_id=task_id), project=project)
    return {"ok": True, "refusal_reason": resp.refusal_reason,
            "refusal_block": resp.refusal_block}


PLAN_REFUSAL_RECALL_BLOCK = Block(
    id="plan.refusal_recall",
    title="Recall the plan_gate refusal for the planner",
    kind="deterministic",
    owner_seat="task_runner",
    scope="task",
    on_failure="continue",
    inputs=["task.gate_reason"],
    outputs=["refusal_reason", "refusal_block"],
    description=(
        "Zero-model-call recall of the plan_gate refusal that rewound the "
        "task to verify_plan (plan_rewind writes it to task.gate_reason). "
        "verify-plan-loop.json's `recall` step runs it before the planner "
        "call and the prompt interpolates ${refusalBlock}, so a second "
        "attempt fixes the named defect instead of repeating the identical "
        "blind prompt. Empty when the task carries no plan refusal."),
)
register_block(PLAN_REFUSAL_RECALL_BLOCK, _run_plan_refusal_recall)


def _run_plan_base_colour(project: str, task_id: str = "") -> dict:
    from prism_service.api import workflows as _wf
    resp = _wf.workflow_step_plan_base_colour(
        _wf.PlanBaseColourRequest(task_id=task_id), project=project)
    return {"ok": True, "rc": resp.rc, "base": resp.base, "colour": resp.colour,
            "base_colour_block": resp.base_colour_block}


PLAN_BASE_COLOUR_BLOCK = Block(
    id="plan.base_colour",
    title="Measure the pinned suite at base for the planner",
    kind="deterministic",
    owner_seat="task_runner",
    scope="task",
    on_failure="continue",
    inputs=["task.verify", "workspace.baseline"],
    outputs=["rc", "base", "colour", "base_colour_block"],
    description=(
        "Runs task.verify at the plan's base commit with plan_gate_checks' "
        "own runner and frames the colour for the tool-less planner as "
        "${baseColourBlock}: RED gives it the `RED at base: <pytest id>` "
        "declaration the already_green_ac tooth accepts; GREEN tells it the "
        "work is done or a NEW red test is required. Closes the loop where "
        "the tooth said `measure it there` to a model that cannot run "
        "anything (tasks 6bc3e6c2 and 83dcd479)."),
)
register_block(PLAN_BASE_COLOUR_BLOCK, _run_plan_base_colour)


def _run_plan_compose(project: str, task_id: str = "", colour: str = "",
                      base: str = "") -> dict:
    from prism_service.api import workflows as _wf
    resp = _wf.workflow_step_plan_compose(
        _wf.PlanComposeRequest(task_id=task_id, colour=colour, base=base),
        project=project)
    return {"ok": True, "plan_frame": resp.plan_frame,
            "pinned_test": resp.pinned_test, "pinned_colour": resp.pinned_colour}


PLAN_COMPOSE_BLOCK = Block(
    id="plan.compose",
    title="Compose the explicit plan frame",
    kind="deterministic",
    owner_seat="task_runner",
    scope="task",
    on_failure="continue",
    inputs=["task.title", "task.verify", "task.allowed_files", "task.oracle",
            "plan.base_colour"],
    outputs=["plan_frame", "pinned_test", "pinned_colour"],
    description=(
        "Zero-model-call frame the planner fills: the pinned test and its "
        "colour at base, the AC PRISM writes itself (red_at_base on the "
        "pinned test), the files in scope, the task oracle, and the "
        "lexicon's terms (red_at_base, guard, oracle). Owner 2026-09-14: "
        "be explicit about what is supposed to happen; the explicit comes "
        "from the planning steps."),
)
register_block(PLAN_COMPOSE_BLOCK, _run_plan_compose)


def _run_plan_render(project: str, task_id: str = "", fields: dict | None = None) -> dict:
    from prism_service.api import workflows as _wf
    fields = dict(fields or {})
    return _wf._score_rubric("plan_structured", fields, project, task_id=task_id)


PLAN_RENDER_BLOCK = Block(
    id="plan.render",
    title="Render typed plan slots and validate with the gate's teeth",
    kind="deterministic",
    owner_seat="reason-loop",
    scope="task",
    on_failure="stop",
    inputs=["reason-loop.acs", "reason-loop.goal", "reason-loop.files_to_change"],
    outputs=["plan_doc", "plan_diagram", "ok", "reason"],
    description=(
        "The plan_structured rubric of verify-plan-loop's reason-loop: "
        "renders plan_doc and plan_diagram from typed slots in the exact "
        "shape plan_gate parses, inserts the pinned red_at_base AC when "
        "the model omitted it, then runs form_complete, plan_diagram_parses "
        "and plan_coverage at the STEP so a bad draft retries here instead "
        "of spending a plan_gate rewind."),
)
register_block(PLAN_RENDER_BLOCK, _run_plan_render)
