"""Pre-red multiplier blocks (owner 2026-09-13/14, on task b490fabc's
lineage): "we should have multiplier steps before the red that are
pydantic to help speed up the inference by minimizing the ask of the
model to something much more direct and less open ended as we have our
ontology, patterns and practices."

Four typed blocks, three deterministic (zero model calls) that shrink
write_failing_tests's agentic prompt, and one grouping the existing
write/run/commit trio into one typed unit:

- red.targets_from_acs   -- api/workflows.py:workflow_step_red_targets_from_acs
- red.context_pack       -- api/workflows.py:workflow_step_red_context_pack
- red.prompt_compose     -- api/workflows.py:workflow_step_red_prompt_compose
- red.materialize        -- write-test-file + run-pinned-suite (rc==1) +
                             commit-tests-only, the SAME three already-
                             codified routes write-failing-tests-loop.json
                             dispatches today. Registered here as one typed
                             unit for visibility; the live behaviour file
                             still declares them as three separate steps
                             (unchanged, zero risk to an in-flight drive) --
                             this block exists for a future consolidation
                             and for anything that wants the whole
                             materialize step as one call.

Each wrapper resolves its target by MODULE-GLOBAL NAME at call time (the
prism_service.blocks lesson from the first landing: a captured function
object silently defeats monkeypatch.setattr in tests).
"""
from prism_service.blocks import Block, register_block


def _run_red_targets_from_acs(project: str, task_id: str = "",
                              verify: str = "", plan_doc: str = ""):
    from prism_service.api import workflows as _wf
    return _wf.workflow_step_red_targets_from_acs(
        _wf.RedTargetsRequest(task_id=task_id, verify=verify,
                              plan_doc=plan_doc),
        project=project)


def _run_red_context_pack(project: str, task_id: str, verify: str = "",
                          budget_chars: int = 2000):
    from prism_service.api import workflows as _wf
    return _wf.workflow_step_red_context_pack(
        _wf.RedContextPackRequest(task_id=task_id, verify=verify,
                                  budget_chars=budget_chars),
        project=project)


def _run_red_prompt_compose(project: str, task_id: str = "", task_hint: str = "",
                            oracle: str = "", targets_block: str = "",
                            context_block: str = "", scaffold_block: str = "",
                            refusal_block: str = ""):
    from prism_service.api import workflows as _wf
    return _wf.workflow_step_red_prompt_compose(
        _wf.RedPromptComposeRequest(
            task_id=task_id, task_hint=task_hint, oracle=oracle,
            targets_block=targets_block, context_block=context_block,
            scaffold_block=scaffold_block, refusal_block=refusal_block),
        project=project)


def _run_red_materialize(project: str, task_id: str, test_file_path: str,
                         test_code: str) -> dict:
    from prism_service.api import workflows as _wf
    write_res = _wf.workflow_step_write_test_file(
        _wf.WriteTestFileRequest(
            task_id=task_id, test_file_path=test_file_path,
            test_code=test_code),
        project=project)
    run_res = _wf.workflow_step_run_pinned_suite(
        _wf.RunPinnedSuiteRequest(task_id=task_id, expected_rc=1),
        project=project)
    commit_res = _wf.workflow_step_commit_tests_only(
        _wf.CommitTestsOnlyRequest(task_id=task_id), project=project)
    return {"write": write_res, "run": run_res, "commit": commit_res}


RED_TARGETS_FROM_ACS_BLOCK = Block(
    id="red.targets_from_acs",
    title="Pair pinned test ids with their acceptance criteria",
    kind="deterministic",
    owner_seat="workflows",
    scope="task",
    on_failure="continue",
    inputs=["task.verify", "task.plan_doc"],
    outputs=["targets", "targets_block"],
    description=(
        "Zero-model-call pairing of every pinned test id (task.verify) "
        "with the acceptance criterion it demonstrates (arc_governance."
        "_ac_lines over task.plan_doc) -- a checklist the draft step can "
        "follow instead of re-deriving one from prose."),
)
register_block(RED_TARGETS_FROM_ACS_BLOCK, _run_red_targets_from_acs)

RED_CONTEXT_PACK_BLOCK = Block(
    id="red.context_pack",
    title="Trim conventions + a nearby test example to a budget",
    kind="deterministic",
    owner_seat="workflows",
    scope="task",
    on_failure="continue",
    inputs=["task.verify"],
    outputs=["conventions", "fixtures", "example_test_header", "context_block"],
    cost_hint="low",
    description=(
        "Zero-model-call trim of ContextBuilder's own conventions list "
        "(computed by context-enrich but never exported downstream, "
        "since only scalar fields survive _exported_variables) plus up "
        "to 2 sibling test files' imports/fixtures, capped to a hard "
        "character budget."),
)
register_block(RED_CONTEXT_PACK_BLOCK, _run_red_context_pack)

RED_PROMPT_COMPOSE_BLOCK = Block(
    id="red.prompt_compose",
    title="Compose the short write_failing_tests prompt",
    kind="deterministic",
    owner_seat="workflows",
    scope="task",
    on_failure="continue",
    inputs=["task_hint", "targets_block", "context_block", "scaffold_block",
            "refusal_block", "task.oracle"],
    outputs=["prompt"],
    description=(
        "Builds the reason-loop step's prompt from the three typed "
        "blocks above plus the task's oracle, replacing the previous "
        "static megaprompt (which re-explained the rc==1 red-gate rule "
        "in prose on every draft) with a short, structured one. The "
        "downstream json_schema/rubric are unchanged -- they already "
        "enforce rc==1, so the prompt no longer has to re-teach it."),
)
register_block(RED_PROMPT_COMPOSE_BLOCK, _run_red_prompt_compose)

RED_MATERIALIZE_BLOCK = Block(
    id="red.materialize",
    title="Write, run (rc==1), and commit the drafted test",
    kind="deterministic",
    owner_seat="workflows",
    scope="task",
    on_failure="stop",
    inputs=["test_file_path", "test_code"],
    outputs=["write", "run", "commit"],
    description=(
        "Groups the three already-codified write-test-file / "
        "run-pinned-suite (expected rc=1) / commit-tests-only routes "
        "into one typed call -- the same trio write-failing-tests-"
        "loop.json already dispatches as three separate declared steps; "
        "this block wraps them for visibility and future consolidation "
        "without changing the live pipeline's step shape."),
)
register_block(RED_MATERIALIZE_BLOCK, _run_red_materialize)
