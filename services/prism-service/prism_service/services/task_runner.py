"""Server-side task runner — drives a task WITHOUT a human terminal
(epic 0784729f, AC-4).

THE GAP THIS CLOSES: the conductor state machine (models/workflow.py)
owns the STEP SEQUENCE, but nothing calls the loop verb unless a human's
Claude Code session sits at a terminal looping on `conductor_work`. The
board reads "paused" the moment nobody is present — not because the task
is blocked, but because the LOOP itself has no driver. This module IS
that driver: a background thread that plays the worker role
`conductor_work` expects, one eligible task and one step per tick, by
invoking the SAME `claude_cli.invoke` primitive every other background
inference call in this service already uses, then reporting through
`api/conductor_flow`'s `flow_start`/`flow_report` so every honest-gate
tooth (distinct-actor, proof-carrying artifact, worker contract) still
applies exactly as it does to a human-driven session.

OPT-IN, OFF by default (mirrors `gate_adjudicator`'s
PRISM_GATE_ADJUDICATOR_INTERVAL): an environment opts in with
PRISM_TASK_RUNNER_INTERVAL=<seconds>. With the var unset this module
allocates no thread, calls no `claude`, and costs nothing — production
behavior is byte-for-byte what it was before this file existed.

NEVER decides a gate: a task whose CURRENT step is type=="gate" is
skipped outright, whether pending, passed, or failed. Gate adjudication
is `gate_adjudicator`'s seat, with its own distinct-actor and evidence
rules; this runner only ever plays an AGENT step and hands the result to
the same `flow_report` a human session would call, which enforces every
rule (distinct-actor, worker contract, proof-carrying artifact) on its
own — this module adds no new gate logic of its own.

HOST-LOAD CIRCUIT BREAKER (real incident, 2026-08-26): this seat is
willing to spawn a real `claude -p` subprocess for every eligible task,
and this host runs MANY concurrent tasks/agents by design -- with no
check on actual host resource pressure, a heavily loaded night can pile
new subprocesses onto an already-saturated box (one background fix that
night was independently killed by its own internal timeout purely from
host contention). `_system_overloaded()` refuses new work, ACTIVE by
default whenever this seat is enabled (PRISM_TASK_RUNNER_MAX_LOAD_PER_
CORE, default 8.0 per core), the same two call sites as the spend
ceiling below.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import uuid
from typing import Optional

DEFAULT_INTERVAL_S = 0  # OFF unless an environment explicitly opts in
SEAT_ID = "prism-task-runner"  # distinct-actor identity on every report
# Real coding work needs more than READ_ONLY_TOOLS (batch analyzers never
# touch disk) — a build step must read, write and run tests in the
# task's own worktree.
BUILD_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit", "Bash")
# Mirrors mcp/tools.py's conductor_work report routing exactly, so a
# runner-produced report reads identically to a human-driven one.
_PLAN_STEPS = ("draft_story", "verify_plan")
_PREMISE_STEP = "review_previous_notes"
# SUPERSEDED by task 6738006b: the bound on how many gathered facts the
# premise step asserts now lives in premise_gather.DEFAULT_KEEP_MAX, and it
# SELECTS the load-bearing few rather than refusing the whole set. The
# invariant this constant carried is unchanged and still enforced there --
# a formatter must not assert every retrieved fact (owner 2026-09-05, on
# noise making the system unusable) -- but the bound is a selection bound
# now, not a bail-out threshold.
# Stall detection (task 82cc05ee): after this many non-advancing reports
# on ONE step, the next tick does NOT spawn another identical attempt --
# it splits the red test ids named in the last proof into children, or
# blocks the task with a reason naming the step. The count lives in task
# history (action=ATTEMPT_ACTION), never in process memory.
STALL_ATTEMPTS = 3
ATTEMPT_ACTION = "runner_attempt"
# A rewind (gate reject / auto-rewind) opens a NEW pass over the step it
# lands on, so the stall budget is counted from it — see _stall_count.
# Written by ConductorService's rewind path as this exact action name.
REWIND_ACTION = "auto_rewind"
_TEST_ID_RE = re.compile(r"(?m)(?:^|\s)((?:[\w./-]+)\.py::[\w\[\]./:-]+)")


def _scores_db_for(project: str) -> str:
    from prism_service.project_context import get_project

    return str(get_project(project)._data_dir / "scores.db")


def _interval_s() -> int:
    raw = os.environ.get("PRISM_TASK_RUNNER_INTERVAL", "")
    try:
        return int(raw) if raw.strip() else DEFAULT_INTERVAL_S
    except ValueError:
        return DEFAULT_INTERVAL_S


def _max_turns() -> int:
    try:
        return max(1, int(os.environ.get("PRISM_TASK_RUNNER_MAX_TURNS", "30")))
    except ValueError:
        return 30


def _max_budget_usd() -> float:
    try:
        return max(0.0, float(
            os.environ.get("PRISM_TASK_RUNNER_MAX_BUDGET_USD", "2.0")))
    except ValueError:
        return 2.0


# Steps that RUN THINGS rather than write about them. verify_green_state
# executes the whole suite, implement_tasks builds and re-runs it, and
# write_failing_tests writes tests AND runs them to prove red. The wall
# clock a paragraph needs is not the wall clock they need. The implement
# workflow already knew this ("known-slow steps get a multiple of it"); the
# task runner did not, and epic 9f60a849 stalled three times at
# verify_green_state with exit=-9 -- SIGKILL at the 900 s bound, on a host
# with 85 GB free and no OOM kills, so it was the clock and nothing else.
# Measured on live durations (filtered to under 4 hours): write_failing_tests
# p90 1528 s, implement_tasks p90 1426 s, verify_green_state p90 1759 s.
# Multipliers: write_failing_tests 2.0 (1800 s), implement_tasks 2.0 (1800 s),
# verify_green_state 3.0 (2700 s). verify_plan is NOT in this set (p90 663 s).
_SLOW_STEPS = {"verify_green_state": 3.0, "implement_tasks": 2.0,
               "write_failing_tests": 2.0}


def _step_timeout_s(step_id: str = "") -> float:
    """Wall-clock bound on a single claude_cli.invoke() call (epic
    3baadd19 AC-1): without this, a wedged `claude -p` child hangs the
    whole drive worker forever -- invoke() has supported timeout_s since
    af8ec904, but nothing called it with one until this wiring."""
    try:
        base = max(1.0, float(
            os.environ.get("PRISM_TASK_RUNNER_STEP_TIMEOUT_S", "900")))
    except ValueError:
        base = 900.0
    # The operator's variable still governs the base; the multiplier only
    # says which steps need more of whatever budget they chose.
    return base * _SLOW_STEPS.get(str(step_id or ""), 1.0)


# ----------------------------------------------------------------------
# Declared node plans (task 8848089d)
# ----------------------------------------------------------------------
# Every conductor node declares an execution plan in
# .prism/behaviors/conductor/<behavior>.json: which model, how many turns,
# what budget, and which sub-steps are CODIFIED -- deterministic Python
# that costs no tokens. This worker used to ignore all of it and fire ONE
# claude_cli.invoke per step with the blanket 30-turn / $2.00 / 900 s
# defaults, so services/premise_gather.py (task cd33263f) had no caller
# outside its own unit tests and review_previous_notes -- the FIRST step of
# every task -- grepped the repo cold on every drive (3,762 tokens, 1,725 s
# mean, against a declared 180 s bound).
#
# review_previous_notes and draft_story opt in with real declarations.
# review_previous_notes is the one behavior with a hand-tuned v2 plan and
# real codified sub-steps. draft_story declares haiku / 4 turns / $0.50 /
# 120 s, sized for a specific narrow prompt (not the wide step brief).
# The reasoning still holds for the BUILD steps (implement_tasks,
# verify_green_state, write_failing_tests) -- their template budget would
# break every drive and they still run the defaults -- but draft_story
# writes a document rather than running anything. Measured on 337 recorded
# draft_story runs in 2026-09 over scores.db, filtered to under 4 hours
# (the table also records seat rows measured in days): median 71 s, p90 255 s,
# and 10 runs (3.0%) at or past 890 s (the 900 s wall, where the step is
# SIGKILLed with no agent_runs record). Task d5808cd1 hit that tail three
# times and blocked with an empty story_md until the declared plan was
# adopted. A node joins this set when its declaration is real, never by
# default.
#
# verify_plan JOINS THEM (task a9f2bec7, 2026-09-10). Its declaration has been
# real since verify-plan-loop.json v3 -- reason-loop, haiku, 4 turns, $0.50, a
# 1295-character prompt and schema [plan_doc, plan_diagram] -- and nothing read
# it. Measured the same day on the same box: draft_story on its narrow plan
# with zero tools took 23.9s and went rubric-green, while verify_plan on the
# full brief with BUILD_TOOLS was killed at 900.1s having written nothing but
# the model's empty think wrapper.
#
# write_failing_tests JOINS THEM (task ab9166d5, 2026-09-10) -- and the rule
# that kept it out is not weakened, it is SATISFIED. That rule said a step
# whose declared prompt only DRAFTS ("do NOT write it to disk") must not be
# dispatched, because the tests-only commit is the red seat's anchor and a
# drive that skips it goes fast and green on nothing. 7.13.292 broke exactly
# that and was reverted the same day. What changed is the DECLARATION: the
# behaviour now also declares write-test-file, run-pinned-suite and
# commit-tests-only, so the chain does the work the draft cannot. The
# membership test lives in _runs_as_declared_steps and reads the declared
# ROUTES, never the step name -- remove a route from the JSON and the step
# falls back to the general agent rather than advancing on a draft.
#
# implement_tasks and verify_green_state still do NOT join, unchanged: their
# declarations remain draft-only ("do NOT write any code", "OBSERVE-only
# check"). A node joins this set when its declaration is real, never by
# default.
_PLANNED_STEPS = frozenset({"review_previous_notes", "draft_story",
                            "verify_plan", "write_failing_tests"})

# Steps whose declared narrow prompt only DRAFTS the work, so it must never
# stand in for the step itself when the declared chain did not run. Running
# such a prompt inline returns prose with exit 0, which advances the step on
# nothing. Membership here costs nothing while the chain works.
_DRAFT_ONLY_WITHOUT_CHAIN = frozenset({"write_failing_tests"})

# The routes a build step must declare before it may run as declared steps.
# A step that cannot write, run and commit has no business leaving the
# general-agent path, because its declared prompt only DRAFTS.
_BUILD_ROUTES = ("write-test-file", "run-pinned-suite", "commit-tests-only")

# Routes that now record their OWN agent_runs row from inside the route
# handler itself (api/workflows.py's _record_node_run, task 1cdf1d70) --
# a direct HTTP call needed the SAME row a declared-chain dispatch left,
# so recording moved to the one seam both paths go through. Recording
# again here would double the count for a task_runner-dispatched run, so
# the loop below skips these three by name. "reason-loop" stays OUT of
# this set deliberately: it is a real model call (tokens, cost), unlike
# the three deterministic build routes, and instrumenting it is separate
# follow-up work, not this ticket's measured misfire.
_SELF_RECORDING_ROUTES = frozenset(_BUILD_ROUTES)

# Steps where a red test can meaningfully exist. The stall mechanism
# (task 404ef4ce) reads codified red test ids to name the next action
# instead of parking for a human. That read is meaningful ONLY at steps
# that write tests and execute them: write_failing_tests pins the first
# red receipt, implement_tasks re-runs them, verify_green_state confirms
# them green. For review_previous_notes, draft_story, and verify_plan
# (which write documents, not tests), consulting the codified read sends
# the owner hunting for a nonexistent test problem steps before any test
# ever exists. The SIGKILL branch still outranks everything at every step.
_RED_TEST_STEPS = frozenset({"write_failing_tests", "implement_tasks",
                             "verify_green_state"})

# An operator moving a task OUT of blocked, as TaskService records it:
# action "updated", details carrying "status: 'blocked' -> 'in_progress'".
# DIRECTIONAL ON PURPOSE. The blocking row reads
# "status: 'in_progress' -> 'blocked'; blocked_reason: ..." and contains BOTH
# words, so a substring test matches it too and would reset the budget on the
# very row that blocks the task -- which retires the 3-attempt guard entirely.
# test_an_unresolved_stall_still_blocks pins this and goes red on a
# direction-blind match.
_OPERATOR_RESET_RE = re.compile(r"status:\s*'blocked'\s*->\s*'in_progress'")

# A model call that never reached a model: the gateway's own error shape
# ("API Error: <code> ...") followed by a connect failure. Anchored on that
# prefix, so a report that only MENTIONS a refused connection in its prose is
# never read as an outage (task b490fabc, 2026-09-11).
_ENDPOINT_OUTAGE_RE = re.compile(
    r"API Error: \d+.*?(?:Cannot connect to host ([^\s,]+)"
    r"|Connection refused|Name or service not known)", re.S)


def _endpoint_outage(text: str):
    """The match when `text` shows the model endpoint was unreachable."""
    return _ENDPOINT_OUTAGE_RE.search(text or "")

# Behavior ids per conductor step. Mirrors api/workflows._BEHAVIOUR_FOR_STEP
# for the steps THIS seat drives; the file is read straight off disk rather
# than through the AosWorkflows engine, so a drive never depends on that
# separate service being reachable.
_BEHAVIOR_FOR_STEP = {
    "review_previous_notes": "review-previous-notes-loop",
    "draft_story": "draft-story-loop",
    "verify_plan": "verify-plan-loop",
    "write_failing_tests": "write-failing-tests-loop",
    "implement_tasks": "implement-tasks-loop",
    "verify_green_state": "validation",
}

# A /steps/* route that calls a model. Everything else on a behavior is
# codified by construction (see api/workflows.py -- reason-loop is the one
# generic agentic endpoint, premise-judge the one bespoke one).
_AGENTIC_ROUTES = frozenset({"reason-loop", "premise-judge"})


def _behavior_dir(project: str):
    """The on-disk conductor behavior directory for `project`."""
    from pathlib import Path
    from prism_service.services.claude_transcripts import _project_source_path

    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() \
        else fallback
    return root / ".prism" / "behaviors" / "conductor"


def _node_plan(project: str, step_id: str) -> Optional[dict]:
    """The declared execution plan for `step_id`, or None.

    None means "no credible declaration" -- either the step is not in
    _PLANNED_STEPS, or its behavior file is missing/unreadable. Every
    caller then keeps this module's own defaults, so a malformed JSON can
    never make a drive cheaper than it should be, only unchanged.
    """
    import json

    if step_id not in _PLANNED_STEPS:
        return None
    behavior = _BEHAVIOR_FOR_STEP.get(step_id)
    if not behavior:
        return None
    path = _behavior_dir(project) / f"{behavior}.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    codified: list[str] = []
    agentic: Optional[str] = None
    model: Optional[str] = None
    max_turns: Optional[int] = None
    budget: Optional[float] = None
    timeout_s: Optional[float] = None
    prompt: Optional[str] = None
    # THE DECLARATION IS A FLOW, NOT A SETTINGS FILE (task eda5a843). The
    # loop below used to reduce a declared chain to scalars and a list of
    # route NAMES, so a step nobody had written an `if` for -- text-challenge
    # -- was collected and dropped. Keep each step whole, in file order, so a
    # dispatcher can run it. The scalars stay for the callers that already
    # read them; nothing below changes meaning.
    steps: list[dict] = []
    json_schema: Optional[dict] = None
    rubric: Optional[str] = None
    for step in doc.get("steps") or []:
        url = step.get("url") or ""
        route = url.split("/steps/")[-1].split("?")[0] if "/steps/" in url else ""
        if not route:
            continue
        # Parsed for EVERY route, not just the agentic one. A codified step
        # carries its own body (text-challenge names the step_id it judges),
        # and the old order parsed the body only after the codified `continue`
        # -- so that body was never read.
        try:
            body = json.loads(step.get("body") or "{}")
        except Exception:
            body = {}
        steps.append({"route": route, "body": body,
                      "url": url, "timeout_s": step.get("timeoutSeconds")})
        if route not in _AGENTIC_ROUTES:
            codified.append(route)
            continue
        agentic = route
        if isinstance(body.get("json_schema"), dict):
            json_schema = body["json_schema"]
        if isinstance(body.get("rubric"), str) and body["rubric"].strip():
            rubric = body["rubric"]
        # The agentic middle also declares its own WALL CLOCK, beside the
        # model and the spend caps. Read it: it is the only figure that
        # says how long this node was ever meant to take.
        declared_clock = step.get("timeoutSeconds")
        if isinstance(declared_clock, (int, float)) and declared_clock > 0:
            timeout_s = float(declared_clock)
        # The agentic middle carries the budget, in the body parsed above.
        model = body.get("model") or model
        # The declared PROMPT travels with the caps it was written for, so a
        # consumer can never adopt one without the other (task 6a7105f9).
        if isinstance(body.get("prompt"), str) and body["prompt"].strip():
            prompt = body["prompt"]
        if isinstance(body.get("max_turns"), int):
            max_turns = body["max_turns"]
        if isinstance(body.get("max_budget_usd"), (int, float)):
            budget = float(body["max_budget_usd"])
    return {"model": model, "max_turns": max_turns,
            "max_budget_usd": budget, "timeout_s": timeout_s,
            "prompt": prompt, "codified": codified, "agentic": agentic,
            "steps": steps, "json_schema": json_schema, "rubric": rubric}


def _step_handlers() -> dict:
    """Route name -> the in-process callable that IS that step.

    IN PROCESS ON PURPOSE, never a self HTTP call. This worker runs inside
    the same service that serves /api/workflows/steps/*, and the engine
    behind an agentic step serves one request at a time, so a worker that
    POSTed to its own API could wait on a slot it is itself holding. Calling
    the handler function directly runs the SAME code the route runs --
    Observe, the schema, and Validate all included -- with no socket in the
    middle.
    """
    from prism_service.api import workflows as _wf

    def _reason_loop(project: str, body: dict):
        req = _wf.ReasonLoopRequest(**body)
        return _wf.workflow_step_reason_loop(req, project=project)

    def _text_challenge(project: str, body: dict):
        return _wf.workflow_step_text_challenge(
            _wf.TextChallengeRequest(**body), project=project)

    # THE BUILD NODES (task ab9166d5). Without these three the declared
    # write-failing-tests chain could only DRAFT, so the step fell back to a
    # general claude -p with BUILD_TOOLS whose envelope 400'd at 163,315
    # tokens against a 131,072 window. They write, run and commit.
    def _write_test_file(project: str, body: dict):
        return _wf.workflow_step_write_test_file(
            _wf.WriteTestFileRequest(**body), project=project)

    def _run_pinned_suite(project: str, body: dict):
        return _wf.workflow_step_run_pinned_suite(
            _wf.RunPinnedSuiteRequest(**body), project=project)

    def _commit_tests_only(project: str, body: dict):
        return _wf.workflow_step_commit_tests_only(
            _wf.CommitTestsOnlyRequest(**body), project=project)

    return {"reason-loop": _reason_loop, "text-challenge": _text_challenge,
            "write-test-file": _write_test_file,
            "run-pinned-suite": _run_pinned_suite,
            "commit-tests-only": _commit_tests_only}


def _subst(value, variables: dict):
    """Fill ${name} placeholders in a declared body, recursively.

    The declaration is a TEMPLATE -- verify-plan-loop.json says ${taskHint}
    and ${taskId} -- so a dispatcher that passed the body through verbatim
    would send the model the literal string "${taskHint}".
    """
    if isinstance(value, str):
        for key, val in variables.items():
            value = value.replace("${" + key + "}", str(val))
        return value
    if isinstance(value, dict):
        return {k: _subst(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, variables) for v in value]
    return value


def _camel(name: str) -> str:
    """snake_case -> camelCase. The declarations interpolate camelCase."""
    head, *rest = str(name).split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _exported_variables(result) -> dict:
    """The names a LATER declared step may interpolate from an EARLIER one.

    THE CHAIN IS A PIPELINE, not a list of independent calls: the
    write-failing-tests declaration fills ${testCode} and ${testFilePath}
    from the draft that reason-loop returned one step earlier. Without this,
    _subst leaves an unknown placeholder verbatim and the write node is
    handed the literal string "${testCode}" as a file body.

    Both spellings are exported -- the schema fields are snake_case and the
    declarations are camelCase -- so a declaration may name either.
    """
    fields: dict = {}
    reason = getattr(result, "reason", None)
    if isinstance(reason, dict):
        fields.update(reason.get("fields") or {})
    if isinstance(result, dict):
        fields.update(result)
    out: dict = {}
    for key, val in fields.items():
        if not isinstance(key, str) or not isinstance(
                val, (str, int, float, bool)):
            continue
        out[key] = val
        out[_camel(key)] = val
    return out


def _dispatch_declared_steps(project: str, plan: Optional[dict],
                             *, handlers: Optional[dict] = None,
                             variables: Optional[dict] = None) -> list[dict]:
    """Run every step the node declares, in file order.

    A route with no handler is REPORTED, never skipped. Silence is what let
    text-challenge sit in a declaration for versions without running: the
    canvas drew it, the file declared it, and nothing said it had not run.

    Each step's output is exported into the variables the NEXT step
    interpolates, so a declaration can name a real pipeline.
    """
    if not plan:
        return []
    table = _step_handlers() if handlers is None else handlers
    live = dict(variables or {})
    out: list[dict] = []
    for step in plan.get("steps") or []:
        route = step.get("route") or ""
        fn = table.get(route)
        if fn is None:
            out.append({"ok": False, "route": route,
                        "reason": f"no handler for declared route {route!r}"})
            continue
        body = _subst(step.get("body") or {}, live)
        try:
            result = fn(project, body)
        except Exception as exc:
            out.append({"ok": False, "route": route,
                        "reason": f"{route} raised {type(exc).__name__}: {exc}"})
            continue
        out.append({"ok": True, "route": route, "result": result})
        live.update(_exported_variables(result))
    return out


def _runs_as_declared_steps(step_id: str, plan: Optional[dict]) -> bool:
    """True when this node's chain is safe to execute as declared.

    THE TEST IS THE DECLARED WORK, NEVER THE STEP NAME. A node qualifies
    only when what it declares can finish the step on its own:

      * verify_plan -- a reason-loop plus the json_schema that forbids a
        chat transcript;
      * write_failing_tests -- write, run AND commit, because the tests-only
        commit is the red seat's anchor.

    implement_tasks and verify_green_state stay out: their declarations
    merely DRAFT ("do NOT write any code", "OBSERVE-only check"), so
    dispatching one would make a drive fast and green on nothing --
    strictly worse than the timeout it replaced. review_previous_notes
    keeps its own codified premise path, which already resolves the step
    with no model call at all.
    """
    if not plan:
        return False
    routes = [s.get("route") for s in plan.get("steps") or []]
    if step_id == "verify_plan":
        return "reason-loop" in routes and bool(plan.get("json_schema"))
    # write_failing_tests joins ONLY on the strength of its declaration
    # (task ab9166d5). The 7.13.292 attempt admitted it while the file
    # still declared the draft alone, so the step would have advanced on a
    # draft with no tests-only commit and no red anchor -- fast and green
    # on nothing. The membership test is therefore the WORK, never the
    # step name: it must be able to write, run AND commit.
    if step_id == "write_failing_tests":
        return all(r in routes for r in _BUILD_ROUTES)
    return False


def _result_from_build_chain(rows: list) -> Optional["_CodifiedResult"]:
    """The write -> run -> commit chain's report, or None.

    THE RED ANCHOR IS THE PRODUCT. None whenever the chain left no
    tests-only commit, because the caller then falls back rather than
    advancing a step on nothing -- the same rule the document chain keeps.

    The rc is REPORTED, never interpreted. oracle_spec's red check wants
    rc==1 and refuses 0, 2 and 4 by name, so the GATE judges the run; this
    seat only carries the measured integer and the real pytest tail, which
    is what conductor_service mints the red receipt from.
    """
    got: dict = {}
    for row in rows or []:
        route = row.get("route")
        if route in _BUILD_ROUTES and row.get("ok"):
            payload = row.get("result")
            if isinstance(payload, dict) and payload.get("outcome") == "ok":
                got[route] = payload
    made = got.get("commit-tests-only") or {}
    if not made.get("committed"):
        return None
    wrote = got.get("write-test-file") or {}
    ran = got.get("run-pinned-suite") or {}
    paths = " ".join(ran.get("paths") or [])
    text = (
        f"Wrote {wrote.get('path', '?')} "
        f"({wrote.get('bytes', 0)} bytes) into the task worktree.\n"
        f"Ran the pinned suite {paths}: pytest exit code {ran.get('rc')}.\n\n"
        f"{ran.get('tail', '')}\n\n"
        f"Committed tests only as {str(made.get('sha', ''))[:12]}: "
        f"{', '.join(made.get('files') or [])}"
    )
    out = _CodifiedResult(text)
    out.structured_output = {"rc": ran.get("rc"), "sha": made.get("sha"),
                             "files": made.get("files") or [],
                             "paths": ran.get("paths") or []}
    return out


def _result_from_dispatch(rows: list) -> Optional["_CodifiedResult"]:
    """The declared chain's artifact, as a claude_cli-shaped result, or None.

    TWO CHAINS DECLARE TWO PRODUCTS: verify_plan's reason-loop returns a
    DOCUMENT (plan_doc + plan_diagram), and write_failing_tests' write ->
    run -> commit leaves a RED ANCHOR. Harvesting only the document is what
    made the build chain report None even after it had written, run and
    committed -- the caller then set dispatched=None and ran the inline
    call whose envelope 400s.

    None still means "the chain produced nothing", and every caller then
    falls back -- a declared flow that misfires must never advance a step
    on nothing.
    """
    built = _result_from_build_chain(rows)
    if built is not None:
        return built
    for row in rows or []:
        if row.get("route") != "reason-loop" or not row.get("ok"):
            continue
        reason = getattr(row.get("result"), "reason", None) or {}
        fields = reason.get("fields") or {}
        doc = (fields.get("plan_doc") or "").strip()
        if not doc:
            return None
        diagram = (fields.get("plan_diagram") or "").strip()
        # _route_proof reads the mermaid out of the report, so carry the
        # declared diagram FIELD back in the shape it already parses.
        text = f"{doc}\n\n```mermaid\n{diagram}\n```" if diagram else doc
        out = _CodifiedResult(text)
        out.structured_output = fields
        return out
    return None


class _CodifiedResult:
    """A claude_cli-shaped result for a step resolved with NO model call.

    Lets the deterministic path reuse every downstream branch (usage,
    proof routing, flow_report) unchanged, while reporting honestly that
    it cost nothing: exit 0, no usage, and the rendered text as its
    output."""

    __slots__ = ("_text", "exit_code", "usage", "run_id", "structured_output")

    def __init__(self, text: str) -> None:
        self._text = text
        self.exit_code = 0
        self.usage = None            # genuinely zero tokens, not "unknown"
        self.run_id = ""
        self.structured_output = None

    def final_text(self) -> str:
        return self._text

    def graceful_budget_stop(self) -> bool:
        return False


def _codified_step_proof(step_id: str, task, facts) -> str:
    """This step's report, built WITHOUT a model, or "" if that is not
    honestly possible.

    Returns a section only when it satisfies the SAME two teeth the gate
    scores -- the citation tooth and oracle engagement -- checked against
    arc_governance's own functions rather than a local copy. Anything less
    falls through to the model, so the programmatic path can never advance
    a step on evidence the rubric would have refused.
    """
    if step_id != _PREMISE_STEP or not facts:
        return ""
    # KEEP THE JUDGMENT WHERE JUDGMENT IS NEEDED -- AND CODIFY IT (task
    # 6738006b). A wide set does still need SELECTING, and bypassing that
    # entirely (7.13.245) made every retrieved fact a premise and turned a
    # throughput fix into a noise generator. But refusing on COUNT was the
    # wrong guard: measured 2026-09-08 over the 10 tasks blocked at this
    # step, 7 were refused by the old count bail and 6 of those 7 render a
    # section the real rubric ACCEPTS. Task 1bcb2b24 was one, and the judge
    # it fell through to failed 6 dispatches and parked it 3 times. So
    # select the load-bearing few HERE, deterministically, and keep the
    # bound that made the old guard safe -- premise_gather.DEFAULT_KEEP_MAX
    # is now that bound's single home.
    try:
        from prism_service.services import arc_governance as gov
        from prism_service.services import premise_gather as pg

        chosen = pg.select(task, facts)
        rendered = pg.render_premises(task, chosen.kept)
        if not rendered.strip():
            return ""
        rubric = gov.load_rubrics().get("premise_grounded") or {}
        section = rubric.get("claims_section", "premises")
        if not pg.citation_check(rendered, claims_section=section).get("ok"):
            return ""
        verdict = gov.score_oracle_engagement(
            str(getattr(task, "oracle", "") or ""), rendered, rubric)
        if not verdict.get("ok"):
            return ""
        return rendered
    except Exception:
        return ""            # a broken shortcut must fall back, never halt


def _build_task_hint(task) -> str:
    """Build the task material string (title and description) for prompts."""
    task_title = getattr(task, "title", "") or ""
    task_desc = getattr(task, "description", "") or ""
    return f"{task_title}\n\n{task_desc}".strip()


def _declared_agentic_prompt(step_id: str, task, facts, plan=None) -> str:
    """The NARROW prompt the node's agentic middle is written for, or "".

    THE OVERRUN THIS CLOSES. The node declares a bounded middle -- premise
    -judge, haiku, 2 turns, $0.50, 120s, and NO tools, because premise
    -gather already resolved every citation it needs. This worker sent
    job["instructions"] (the whole step brief) with 30 turns and the full
    BUILD_TOOLS set instead, so the declared caps could not be honoured and
    a step the node sizes at ~2 minutes ran for an hour or more. Worse, the
    declared chain never executed as steps, so every sub-node on the
    Workflows canvas read "too few runs (0/20)": the work was real and
    entirely invisible.

    _invoke_budget's own note set the condition -- "a future slice that
    also adopts the declared PROMPT may then adopt the caps that were
    written for it, never one without the other". This is that slice: the
    prompts below are the same ones the workflow steps build, so the caps
    beside them are the ones they were sized for.

    REFUSAL RULES BY STEP:
    - review_previous_notes: returns "" when no facts gathered -- the narrow
      prompt has no material and the full brief is the honest fallback.
    - draft_story: returns "" when the task hint is empty (no title/description
      loaded) -- a prompt with no task material must never replace the full
      brief, which at least contains it.
    - verify_plan: returns "" when no `plan` is in hand, or the plan declares
      no prompt. Its prompt is read from the DECLARATION rather than copied
      into this module, so verify-plan-loop.json stays the single source of
      truth for it and cannot drift from what the canvas shows.
    - write_failing_tests: same rule as verify_plan -- its prompt is read from
      write-failing-tests-loop.json, and the declared chain (write -> run ->
      commit) is what advances the step; _DRAFT_ONLY_WITHOUT_CHAIN still drops
      this prompt when the chain leaves no tests-only commit, so the full brief
      stays the fallback.

    Every refusal ensures the full brief is the fallback, not a
    materially-hollow narrow prompt.
    """
    if step_id == _PREMISE_STEP:
        if not facts:
            return ""
        facts_md = "\n".join(
            f"- ({f.kind}) {f.text} \u2014 {f.citation}" for f in facts)
        return (
            "Material already GATHERED for you is below; every line already "
            "carries a real citation. Decide which are load-bearing for this "
            "task and report them as a '## Premises' markdown list, one "
            "bullet per claim, reusing its citation VERBATIM. Never invent a "
            "new citation. You may add a claim of your own only if you mark "
            "it UNVERIFIED or REFUTED.\n\n"
            f"Task: {getattr(task, 'title', '')}\n{getattr(task, 'description', '')}\n\n"
            f"Gathered material:\n{facts_md}"
        )
    if step_id == "draft_story":
        task_hint = _build_task_hint(task)
        # REFUSE when the task material is empty: a narrow prompt with no
        # task to draft about is strictly worse than the full brief, which at
        # least includes task details. The fallback wide prompt is honest.
        if not task_hint:
            return ""
        return (
            "Draft a PRISM story document in markdown for this task: "
            f"{task_hint}\n\n"
            "It MUST have these exact headings: ## Summary, ## Requirements, "
            "## Acceptance Criteria. Every acceptance criterion bullet MUST "
            "carry an id like AC-1 and end with an oracle marker, e.g. "
            "'\u2014 oracle: <observable check>'."
        )
    if step_id in ("verify_plan", "write_failing_tests"):
        declared = str((plan or {}).get("prompt") or "")
        task_hint = _build_task_hint(task)
        if not declared or not task_hint:
            return ""
        return declared.replace("${taskHint}", task_hint)
    return ""


def _invoke_budget(step_id: str, plan: Optional[dict],
                   narrow: bool = False, after_kill: bool = False) -> dict:
    """claude_cli.invoke kwargs for `step_id` under `plan`.

    The declared plan governs the MODEL, TURN LIMIT and BUDGET -- the three
    caps that actually bound spend. The wall clock stays this module's:
    _step_timeout_s already encodes hard-won knowledge about which steps
    run things rather than write about them, and a timeout that is too
    small kills a drive outright while one that is too large costs nothing
    once the turn and budget caps bind first.

    ONE EXCEPTION, `after_kill` (task 1c42ee74): when the LAST recorded
    outcome at this step was a budget kill, the full clock has already been
    proven not to help. Task d5808cd1 spent seven runs at 900 s each on a
    draft_story that never emitted a `result` event -- three per stall
    ladder, then the resume actuator's whole ceiling of 12 dispatches. So a
    retry after a kill runs on the node's DECLARED bound where it has one
    (draft-story-loop declares 120 s), which turns the three-attempt ladder
    from 2700 s into 1140 s. A step with no declaration keeps its own
    budget: a kill must never invent a tighter clock out of nothing.
    """
    runner_timeout = _step_timeout_s(step_id)
    declared_clock = (plan or {}).get("timeout_s")
    if after_kill and isinstance(declared_clock, (int, float)) \
            and 0 < declared_clock < runner_timeout:
        runner_timeout = float(declared_clock)
    if not plan:
        return {"model": "", "max_turns": _max_turns(),
                "max_budget_usd": _max_budget_usd(),
                "timeout_s": runner_timeout}
    if narrow:
        # The declared prompt is in play, so the declared caps are the ones
        # it was written for -- adopt them together, never one alone.
        return {"model": plan.get("model") or "",
                "max_turns": plan.get("max_turns") or _max_turns(),
                "max_budget_usd": (plan.get("max_budget_usd")
                                   if plan.get("max_budget_usd") is not None
                                   else _max_budget_usd()),
                "timeout_s": runner_timeout}
    # ONLY THE MODEL IS SAFE TO ADOPT. A behavior's turn/budget caps are
    # sized for the behavior's OWN narrow prompt (premise-judge merely
    # rules on facts that premise-gather already resolved). This worker
    # still sends job["instructions"] -- the full step brief -- so those
    # caps do not fit it. Proven live on task 6a7105f9 (2026-08-30): with
    # the declared max_turns=2 applied to the full brief, three attempts
    # each spent their turns on "Let me fetch the task details..." and
    # died `exit=1 ... truncated mid-turn`, and the task blocked at
    # review_previous_notes. The saving is real without them: haiku
    # instead of the default model, and a gather that removes the tool
    # round trips the model used to spend finding its own citations.
    # A future slice that also adopts the declared PROMPT may then adopt
    # the caps that were written for it -- never one without the other.
    return {
        "model": plan.get("model") or "",
        "max_turns": _max_turns(),
        "max_budget_usd": _max_budget_usd(),
        "timeout_s": runner_timeout,
    }


def _failure_reason(result, budget_s: float, proof: str = "") -> str:
    """Why a step produced no usable report, in words that name the cause.

    `exit=-9` is PRISM'S OWN TIMEOUT (`claude_cli.TIMEOUT_EXIT_CODE`),
    returned when subprocess.run raises TimeoutExpired at `timeout_s`. It
    is not a host SIGKILL and not a crash. Reported as the old flat string
    "exit=-9, no usable output" it read as either, which is how task
    d5808cd1's seven timeouts were first diagnosed as an out-of-memory
    kill: the host had ~60 GB free and no OOM row in dmesg the whole time.
    Name the limit and the elapsed time so a reader can tell a step that
    ran out of clock from a step that fell over.
    """
    from prism_service.inference import claude_cli as _cli

    exit_code = getattr(result, "exit_code", None)
    # THE EXIT CODE IS THE AUTHORITY, tested FIRST and independently of what
    # came back. Caught live on d5808cd1's verify_plan (2026-09-10): the local
    # model emits an empty `<think></think>` wrapper before any real content,
    # so a run killed at its bound before writing anything still returns a
    # NON-EMPTY proof. That fell past the emptiness test and the row read
    # "exit=-9, non-graceful failure (crash/auth/truncated mid-turn)" for what
    # was a plain timeout -- the same class of lie this helper exists to end.
    if exit_code == _cli.TIMEOUT_EXIT_CODE:
        ran = float(getattr(result, "duration_s", 0.0) or 0.0)
        return (f"exit={exit_code}, the step exceeded its {int(budget_s)}s "
                f"budget: it was stopped after {ran:.1f}s without reporting")
    # AN OUTAGE IS NOT A CRASH (task b490fabc, 2026-09-11). The proxy answered
    # "Cannot connect to host inference.dev.internal:8080" because the engine
    # behind it was gone, and this row read "crash/auth/truncated". Name the
    # endpoint so a reader sees an outage the step could not fix.
    hit = _endpoint_outage(proof)
    if hit:
        where = hit.group(1) or "the model endpoint"
        return (f"exit={exit_code}, the model endpoint was unreachable "
                f"({where}): the step never reached a model")
    if not (proof or "").strip():
        return f"exit={exit_code}, no usable output"
    return (f"exit={exit_code}, non-graceful failure "
            "(crash/auth/truncated mid-turn)")


def _repair_premises(proof: str, task, facts) -> str:
    """Complete an ungrounded premise report with the RENDERED section.

    A FLOOR, not a rewrite. The codified citation check already ran here and
    was advisory only -- it recorded that the report cited nothing and let
    the step park anyway, which is how premise_grounded refused 273 advances
    across 141 tasks (measured 2026-09-05). The facts to satisfy it were in
    hand the whole time.

    When the report already passes, it is returned untouched: a model that
    got it right is never overwritten. When it does not, the grounded
    section is prepended and the model's own prose is KEPT below it, so
    nothing it reasoned out is thrown away -- the report gains evidence, it
    does not lose analysis.
    """
    try:
        from prism_service.services import premise_gather

        text = str(proof or "")
        if premise_gather.citation_check(text).get("ok"):
            return text
        rendered = premise_gather.render_premises(task, facts)
        if not rendered.strip():
            return text          # nothing gathered: nothing honest to add
        if not text.strip():
            return rendered
        # The rendered section becomes THE premises. The model's own
        # ungrounded bullets are demoted to notes rather than merged in:
        # its reasoning is kept and readable, but unsourced claims must not
        # ride under a heading that asserts they are grounded, and a second
        # "## Premises" heading would put them back in front of the tooth.
        demoted = re.sub(r'(?im)^\s{0,3}#{1,6}\s*premises\s*$',
                          "## Notes (model draft, ungrounded)", text)
        return f"{rendered}\n{demoted}"
    except Exception:
        return str(proof or "")   # a broken repair must never halt a drive


def _codified_preamble(project: str, task, memory_svc=None, task_svc=None,
                       brain_svc=None):
    """Resolved citations for `task`, as (preamble_text, facts).

    This is the CODIFIED gather sub-step running in-process: pure local
    reads, no model call, and no git/worktree lock (the 2026-08-29 daemon
    wedge this must not reproduce). Handing the judge real file:line
    citations up front is the whole token saving -- without it the model
    greps the repo cold, one tool round trip per claim.

    Degrades to ("", []) on ANY failure: a broken gather must leave the
    drive exactly as it was, never halt it.
    """
    try:
        from prism_service.services import premise_gather

        facts = premise_gather.gather(
            task, memory_svc=memory_svc, task_svc=task_svc,
            brain_svc=brain_svc)
    except Exception:
        return "", []
    if not facts:
        return "", []
    lines = [f"- [{f.kind}] {f.text} ({f.citation})" for f in facts]
    preamble = (
        "Grounded facts already resolved for this task (codified gather -- "
        "reuse a citation verbatim rather than searching for it again):\n"
        + "\n".join(lines))
    return preamble, list(facts)


def _record_codified_run(project: str, task_id: str, route: str,
                         run_id: str, ok: bool, summary: str) -> None:
    """Report a codified sub-step as its own ZERO-TOKEN agent_runs row.

    Without this the saving is invisible: the Workflows node card would
    keep reporting only the agentic middle, and nobody could tell the
    codified half ever ran.
    """
    try:
        from prism_service.services import agent_runs_data

        # STAMP THE TIME. Without started_at/ended_at these rows carry no
        # recency at all, so nothing downstream can say "this node is
        # running now" -- the Workflows canvas showed no activity on a
        # behaviour's sub-steps however often they fired, and a plain
        # count cannot distinguish 149 runs last week from one a second
        # ago. A codified step is effectively instantaneous, so start and
        # end are the same instant rather than a fabricated span.
        _now = time.time()
        agent_runs_data.upsert_agent_run(_scores_db_for(project), {
            "run_id": run_id, "workflow_name": "implement",
            "task_id": task_id, "agent_id": SEAT_ID, "role": "sm",
            "step": route, "model": "codified", "tokens": 0,
            "cost_usd": 0.0, "ok": 1 if ok else 0,
            "started_at": _now, "ended_at": _now, "duration_ms": 0,
            "verdict_summary": summary[:500],
        })
    except Exception:
        pass


def _max_total_usd() -> Optional[float]:
    """Aggregate spend ceiling across every tick this process ever runs
    (epic 3baadd19 AC-5) -- unlike `_max_budget_usd` (a PER-INVOCATION cap
    passed to claude_cli.invoke), this bounds the runaway case: a stuck
    loop, a wedged step retried forever, or simply many expensive tasks
    queued back to back. Unset/blank == unbounded (None), matching every
    other opt-in knob in this module."""
    raw = os.environ.get("PRISM_TASK_RUNNER_MAX_TOTAL_USD", "")
    if not raw.strip():
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _max_load_per_core() -> float:
    """Per-core 1-minute load-average ceiling for the host-overload circuit
    breaker (real incident, 2026-08-26: 30+ PRISM tasks sat `in_progress`
    simultaneously on this same host, each one this module is willing to
    spawn a real `claude -p` subprocess for, contending with many other
    concurrent agent/pytest processes already running -- one of that
    night's own background fixes was independently killed by its own
    internal timeout purely from host contention).

    Unlike `_max_total_usd` (an aggregate spend ceiling that is
    meaningless without an owner-chosen dollar figure, so it defaults to
    None/unbounded until explicitly set), a host-load ceiling has a sane
    universal default -- so this mirrors `_max_turns`/`_max_budget_usd`/
    `_step_timeout_s`'s shape instead: a real numeric default that is
    ACTIVE the moment task_runner itself is enabled, no second opt-in
    required. Deliberately NOT matching `_spend_ceiling_crossed`'s
    unset-means-unbounded posture: that would leave exactly the gap the
    owner asked to close (a heavily-loaded box with no guard at all until
    someone remembers a second env var).

    Default 8.0: this host idles around 0.15-0.2 per core (`uptime`
    observed load average 3.74 on 24 cores while quiet). Most concurrent-
    agent load is TIME SPENT BLOCKED ON NETWORK I/O (waiting on LLM
    calls), which Linux's load average does not count as runnable, so
    legitimate heavy multi-agent concurrency -- the normal, by-design
    state of this box -- should stay well under this ceiling. 8.0/core is
    reserved for genuine saturation (many CPU-runnable or disk-blocked
    processes queued for the same cores), the failure mode that silently
    killed one of tonight's background fixes via its own internal
    timeout.
    """
    try:
        return max(0.1, float(
            os.environ.get("PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE", "8.0")))
    except ValueError:
        return 8.0


def _system_overloaded() -> bool:
    """True (and logged) once the 1-minute load average per CPU core
    exceeds `_max_load_per_core()`. Fails SAFE -- returns False, never
    raises -- on any platform/environment where `os.getloadavg()` is
    unavailable (Windows, some containers): exactly like
    `_spend_ceiling_crossed()` fails safe (False) when its own env var is
    unset, a check this module cannot perform must never be treated as a
    reason to refuse work."""
    try:
        load1, _, _ = os.getloadavg()
        cpu_count = os.cpu_count() or 1
    except (OSError, AttributeError, NotImplementedError):
        return False
    ceiling = _max_load_per_core()
    per_core = load1 / cpu_count
    if per_core <= ceiling:
        return False
    _log(f"host overloaded: {load1:.2f} 1-min load avg / {cpu_count} cores "
         f"= {per_core:.2f} per-core > {ceiling:.2f} ceiling "
         "(PRISM_TASK_RUNNER_MAX_LOAD_PER_CORE) -- refusing further work "
         "until load drops")
    return True


# One verdict per window. sweep_once asks once per project, and a tick over
# 130 projects must not become 260 HTTP probes.
ENGINE_PROBE_TTL_S = 15.0
_ENGINE_PROBE: dict = {"at": None, "down": False}


def _http_ok(url: str, timeout_s: float = 2.0) -> bool:
    """True when `url` answers 2xx within `timeout_s`. Never raises."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _engine_unreachable() -> bool:
    """True (and logged) while the LOCAL engine cannot answer a step.

    Sibling of `_system_overloaded()`, at the same two call sites. Task
    b490fabc, 2026-09-11: the AOS engine died while its proxy stayed up,
    all three draft_story attempts failed on the outage, and the task was
    blocked with a reason that blamed the step. So the tick starts nothing:
    no claim, no attempt spent, and the task resumes when the engine does.
    Probes the engine as well as the proxy, since the proxy answers
    liveness with nothing behind it. The default backend never probes.
    """
    import time
    from prism_service import config

    if (config.INFERENCE_BACKEND or "").strip().lower() != "local":
        return False
    now = time.monotonic()
    at = _ENGINE_PROBE.get("at")
    if at is not None and now - at < ENGINE_PROBE_TTL_S:
        return bool(_ENGINE_PROBE.get("down"))
    probes = [(config.LOCAL_INFERENCE_BASE_URL or "").rstrip("/")
              + "/health/liveliness"]
    if (config.LOCAL_ENGINE_HEALTH_URL or "").strip():
        probes.append(config.LOCAL_ENGINE_HEALTH_URL.strip())
    dead = [u for u in probes if not _http_ok(u)]
    _ENGINE_PROBE.update(at=now, down=bool(dead))
    if dead:
        _log(f"local engine unreachable: {', '.join(dead)} did not answer "
             "-- starting no step until it does")
    return bool(dead)


def _log(msg: str) -> None:
    print(f"[task-runner] {msg}", file=sys.stderr, flush=True)


def is_enabled() -> bool:
    """True when this environment opted into the seat (interval > 0)."""
    return _interval_s() > 0


# Reentrancy guard: never re-enter a task already being driven by this
# process's own runner — the tick loop is single-threaded so this only
# matters for overlapping in-process callers (e.g. concurrent tests).
_IN_FLIGHT: set[str] = set()
_IN_FLIGHT_LOCK = threading.Lock()

# Round-robin offset into get_all_projects()'s (alphabetically SORTED,
# config.py:list_projects) result -- without this, sweep_once always
# starts scanning from the same first project every tick, so a project
# that always has an eligible task (a continuous backlog) permanently
# starves every alphabetically-later project of the single per-tick work
# slot. Observed live (epic 3baadd19 AC-2, task 12403c60): "csregs-
# datamanagement" < "prism" sorts first, and its own continuously-
# refilled backlog held every tick for the runner's entire uptime while
# an eligible prism task sat untouched.
_RR_LOCK = threading.Lock()
_rr_index = 0


class _SpendTracker:
    """Process-lifetime accumulator of `usage["cost_usd"]` across every
    step this seat has run (epic 3baadd19 AC-5). Charged immediately after
    each claude_cli.invoke() returns, checked against `_max_total_usd()`
    before the NEXT tick claims any work."""

    def __init__(self) -> None:
        self.spent = 0.0
        self._lock = threading.Lock()

    def charge(self, amount: object) -> None:
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return
        with self._lock:
            self.spent += amount


_spend_tracker = _SpendTracker()


def _spend_ceiling_crossed() -> bool:
    """True (and logged) once accumulated spend has exceeded the
    configured ceiling -- unset/blank PRISM_TASK_RUNNER_MAX_TOTAL_USD is
    always False (unbounded)."""
    ceiling = _max_total_usd()
    if ceiling is None:
        return False
    spent = _spend_tracker.spent
    if spent <= ceiling:
        return False
    _log(f"spend ceiling crossed: ${spent:.2f} spent > ${ceiling:.2f} "
         "ceiling (PRISM_TASK_RUNNER_MAX_TOTAL_USD) -- refusing further "
         "work until the ceiling is raised")
    return True


# The value this seat writes into a drive heartbeat's `driver` column, and
# the ONE driver name eligible_task ignores when it decides whether somebody
# else is already on a task (task e4c631d7). The runner beats for itself at
# the start of every step, and a step transition wakes the next tick well
# inside HEARTBEAT_WINDOW_S -- a guard that only asked "is there a beat"
# would make this seat skip its own work forever.
RUNNER_DRIVER = "prism-task-runner"


def _foreign_driver_on(project: str, task_id: str) -> str:
    """The name of a DIFFERENT driver that beat for `task_id` inside
    drive_heartbeat.HEARTBEAT_WINDOW_S, or "" when nobody else is on it.

    This is the evidence half of the owner's "two queues of control"
    complaint (task d9f082fe): a session driving a task over the REST flow
    now marks it in_progress, which is what makes it visible on the board --
    and would also make it claimable here. A live foreign beat says the task
    already has a driver, so this seat stands down.
    """
    from prism_service.services import drive_heartbeat
    try:
        beat = drive_heartbeat.latest(_scores_db_for(project), task_id)
    except Exception:
        return ""
    if beat is None:
        return ""
    if beat.get("age_s") is None or beat["age_s"] > drive_heartbeat.HEARTBEAT_WINDOW_S:
        return ""
    driver = str(beat.get("driver") or "")
    return "" if driver in ("", RUNNER_DRIVER) else driver


def _claim(task_id: str) -> bool:
    with _IN_FLIGHT_LOCK:
        if task_id in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(task_id)
        return True


def _release(task_id: str) -> None:
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT.discard(task_id)


def _concurrency() -> int:
    """How many tasks one sweep may drive at the same time.

    This seat used to drive exactly ONE task per tick, globally, across every
    project -- so the only actor that can move the board unattended advanced
    one step at a time while ~90 tasks sat open (owner, 2026-09-05: "i still
    see 89 open tasks"). implement_tasks alone has a ~474s median, so a serial
    seat could never finish a real backlog however many gates were cleared.

    The bound stays REAL, and it is not the only one: the host load breaker
    (_system_overloaded) and the spend ceiling (_spend_ceiling_crossed) still
    refuse an entire tick before any of these start, and run_one_step's own
    claim lease still makes driving one task twice at once impossible.
    """
    try:
        return max(1, int(os.environ.get("PRISM_TASK_RUNNER_CONCURRENCY", "4")))
    except ValueError:
        return 4


def eligible_tasks(project: str, limit: int = 1) -> list[str]:
    """Up to `limit` task ids this tick may drive in `project`.

    THE one eligibility rule -- `eligible_task` is this function with
    limit=1, never a second copy that can drift away from it.
    """
    from prism_service.project_context import get_project
    from prism_service.services.conductor_service import ConductorService

    if limit <= 0:
        return []
    if _spend_ceiling_crossed():
        return []
    if _system_overloaded():
        return []
    if _engine_unreachable():
        return []

    out: list[str] = []
    ctx = get_project(project)
    for t in ctx.task_svc.list(status="in_progress"):
        if t.id in _IN_FLIGHT:
            continue
        # Somebody else is demonstrably driving this task right now. Skipping
        # is not a refusal to work -- the beat goes stale within
        # HEARTBEAT_WINDOW_S of that driver stopping, and the task becomes
        # eligible again on a later tick.
        foreign = _foreign_driver_on(project, t.id)
        if foreign:
            _log(f"skipping {t.id[:8]}: driver {foreign!r} is live on it")
            continue
        if not t.workflow_step:
            out.append(t.id)
        else:
            step = ConductorService._step_by_id(t.workflow_step)
            if step is None or step["type"] == "gate":
                continue
            # Terminal steps (done, intake) are not driven by task_runner.
            # They are auto-advanced by conductor_service when reached.
            if step["type"] in ("done", "intake"):
                continue
            out.append(t.id)
        if len(out) >= limit:
            break
    return out


def eligible_task(project: str) -> Optional[str]:
    """The id of the one task this tick may drive in `project`, or None.

    Eligible == in_progress and its CURRENT step is an AGENT step — a task
    sitting at a gate (pending, passed, or failed) is never eligible; gate
    adjudication belongs to a distinct seat, not this one. A task that has
    NOT YET entered the flow (workflow_step=="") is also eligible: WORKFLOW_
    STEPS[0] (review_previous_notes) is always type=="agent", never a gate,
    and _run_one_step's own flow_start call is what sets workflow_step for
    the first time — this seat must be the one to make that call, or a task
    that only ever gets `status=in_progress` (never separately driven by a
    human/session into the flow) sits forever with workflow_step=="" and is
    never eligible under any check that requires it non-empty first (epic
    3baadd19's own oracle: set in_progress and touch nothing else).
    """
    found = eligible_tasks(project, 1)
    return found[0] if found else None


def _mermaid_source(proof: str) -> str:
    """The mermaid SOURCE inside a markdown report, or "".

    `arc_governance.score_plan_*` reads the plan_diagram FIELD and refuses on
    "plan_diagram is missing" or "does not parse as mermaid" (arc_governance.py
    :279-285). A narrow verify_plan returns ONE markdown blob, so the diagram
    has to be lifted out of it or the field stays empty and a working plan is
    refused -- which reads to a human as the drive's fault.

    Returns "" when there is no diagram. NEVER fabricates one: an absent
    diagram must stay absent so the rubric can refuse honestly.
    """
    import re as _re

    from prism_service.services.arc_governance import _MERMAID_KEYWORDS

    # A fenced block whose first non-blank line names a diagram type. Covers
    # ```mermaid and a bare ``` fence holding "flowchart TD" alike.
    for block in _re.findall(r"```[^\n]*\n(.*?)```", proof, _re.S):
        head = block.strip().split("\n", 1)[0].strip().lower()
        if head.startswith(_MERMAID_KEYWORDS):
            return block.strip()
    # Unfenced: a run of lines starting at a diagram-type keyword.
    lines = proof.split("\n")
    for i, line in enumerate(lines):
        if line.strip().lower().startswith(_MERMAID_KEYWORDS):
            return "\n".join(lines[i:]).strip()
    return ""


def _route_proof(task_svc, task_id: str, step_id: str, proof: str) -> None:
    """Write a successful step's output to the field its gate rubric
    reads — the SAME routing conductor_work's MCP handler applies."""
    try:
        if step_id == "verify_plan":
            # TWO fields, because the plan rubric reads two. plan_doc keeps the
            # WHOLE report (coverage is diffed by looking for each AC-<n> in
            # it), and plan_diagram gets the lifted mermaid source.
            fields = {"plan_doc": proof, "completion_proof": proof}
            diagram = _mermaid_source(proof)
            if diagram:
                fields["plan_diagram"] = diagram
            task_svc.update(task_id, **fields)
        elif step_id in _PLAN_STEPS:
            task_svc.update(task_id, plan_doc=proof, completion_proof=proof)
        elif step_id == _PREMISE_STEP:
            task_svc.update(task_id, premise_notes=proof,
                            completion_proof=proof)
        else:
            task_svc.update(task_id, completion_proof=proof)
    except Exception:
        pass


def _stall_count(task_svc, task_id: str, step_id: str) -> int:
    """Non-advancing runner reports recorded on `step_id` SINCE the most
    recent rewind OR operator status reset (durable).

    Counting the WHOLE life of the task made every gate rejection
    terminal (task ce471e06, 2026-09-04): a reject rewinds the task to
    its producing step, and if that step had already spent its
    STALL_ATTEMPTS budget on the earlier pass, the guard fired on the
    very FIRST tick of the new pass and blocked the task again — with a
    reason ("did not advance after 3 attempts") describing work the
    driver had not been allowed to attempt. A reject is a fresh mandate
    carrying new direction, so the budget starts over with it; attempts
    within one pass still stall exactly as before.

    An operator manually moving a task from blocked to in_progress is
    an identical fresh mandate to retry, so it is a budget boundary the
    same way as REWIND_ACTION (task 1ecbd866). TaskService records that
    transition as an `updated` row whose details carry the transition
    text; it writes NO actor on it (task_service.py:1322 passes none), so
    the act is durable and attributable to a time but not yet to a
    person. Ticket 1ecbd866 asks for an actor; supplying one needs
    TaskService, which is outside this slice, and is filed separately."""
    marker = f"step={step_id}; advanced=false"
    rows = list(task_svc.history(task_id) or [])
    start = 0
    for i, h in enumerate(rows):
        action = str(getattr(h, "action", "") or "")
        # REWIND_ACTION is a gate rejection (existing path)
        # "updated" action from blocked -> in_progress is an operator reset (new path)
        if action == REWIND_ACTION:
            start = i + 1
        elif action == "updated":
            details = str(getattr(h, "details", "") or "")
            # Match DIRECTIONAL pattern: status: 'blocked' -> 'in_progress'
            # Blocks on reverse direction (in_progress -> blocked) and other transitions
            if _OPERATOR_RESET_RE.search(details):
                start = i + 1
    # AN OUTAGE IS NOT AN ATTEMPT (task b490fabc, 2026-09-11): a run that
    # died on an unreachable model endpoint never reached a model, so it
    # must not spend the step's budget. Retries stay bounded by
    # dispatch_guard's ceiling, and _engine_unreachable() stops dispatch
    # while the engine is down.
    return sum(1 for h in rows[start:]
               if h.action == ATTEMPT_ACTION and marker in h.details
               and not _endpoint_outage(h.details))


def red_test_ids(proof: str) -> list[str]:
    """Distinct pytest node ids named in a proof, in first-seen order."""
    seen: list[str] = []
    for m in _TEST_ID_RE.findall(proof or ""):
        if m not in seen:
            seen.append(m)
    return seen


def _shipped_sha_for_stall(task_id: str) -> str:
    """The origin/main commit carrying this task's own trailer, or "".

    Delegates to the SAME squash-safe reader the gate's unshipped tooth
    uses (api/tasks._shipped_sha_on_main), so the stall path and the gate
    can never disagree about whether a task landed.
    """
    from prism_service.api.tasks import _shipped_sha_on_main
    from prism_service.services.task_workspace import _prism_repo_root
    return _shipped_sha_on_main(str(_prism_repo_root()), task_id) or ""


def _stall_work_is_shipped(task_id: str) -> bool:
    """True when this task's work is already on origin/main.

    FAILS CLOSED: any error answers False, so a git or repo problem leaves
    the original split behaviour untouched rather than closing a task on an
    exception.
    """
    try:
        return bool(_shipped_sha_for_stall(task_id))
    except Exception:  # noqa: BLE001 - a stall handler never raises
        return False


def _last_outcome_was_a_kill(task_svc, task_id: str, step_id: str) -> bool:
    """True when this step's most recent recorded outcome was a SIGKILL.

    Reads the durable history rather than in-memory state, so a restart
    between the kill and the stall still tells the truth. Any error answers
    False, which keeps the original message: a stall handler never raises,
    and a wrong "killed" claim would be its own dishonesty.
    """
    try:
        marker = f"step={step_id}"
        for row in reversed(list(task_svc.history(task_id))):
            details = str(getattr(row, "details", "") or "")
            if getattr(row, "action", "") != "flow_report_failure":
                continue
            if marker not in details:
                continue
            return "exit=-9" in details
    except Exception:  # noqa: BLE001 - honesty is best-effort, never fatal
        return False
    return False


def _green_gate_ever_passed(task_svc, task_id: str) -> bool:
    """True when this task's OWN history carries a green_gate decision that
    approved. Shipped-ness proves the work exists; only this proves somebody
    judged it. FAILS CLOSED: any error answers False, so a history problem
    refuses to close a task rather than closing one nobody adjudicated."""
    try:
        rows = task_svc.history(task_id) or []
    except Exception:  # noqa: BLE001 - a stall handler never raises
        return False
    for r in rows:
        if getattr(r, "action", "") != "gate_decide":
            continue
        d = str(getattr(r, "details", "") or "")
        if "green_gate" in d and "action=approve" in d:
            return True
    return False


def _codified_red_test_ids(project: str, task_id: str) -> tuple[list[str], str]:
    """This task's red test ids, read off data PRISM already persists.

    THE DETERMINISTIC HALF of the stall splitter. task.verify names the
    pinned targets, ConductorService._red_step_sha resolves the red anchor,
    and oracle_spec.fresh_red_receipt records whether a trusted run actually
    demonstrated red there -- three facts a model should never have to
    retype into its prose for the runner to find them.

    Mirrors api.workflows.workflow_step_red_test_ids (the codified node task
    404ef4ce shipped) rather than re-deciding anything: same OracleSpec, same
    anchor, same receipt. PURE READS -- never runs pytest, never invokes a
    model, never takes a repo or worktree lock. Returns (ids, reason); an
    empty list always carries the reason it is empty, so a parked task can
    say WHY the deterministic read found nothing instead of blaming a proof
    that was never the authority.
    """
    try:
        from prism_service.services import oracle_spec as osp
        from prism_service import project_context as _pc

        # Resolved through the module global when a caller has replaced it
        # (the rest of this module imports get_project lazily to dodge an
        # import cycle, so there is no module-level name to patch).
        _get_project = globals().get("get_project", _pc.get_project)

        ctx = _get_project(project)
        task = ctx.task_svc.get(task_id)
        if task is None:
            return [], f"no such task: {task_id}"

        spec = osp.OracleSpec.from_task(task)
        if spec.adapter != osp.ADAPTER_PYTEST:
            return [], ("task's derived oracle spec is not pytest-backed "
                        f"(adapter={spec.adapter}) -- no pytest node ids to name")

        pinned = [t for t in spec.target.split() if t]
        if not pinned:
            return [], "task.verify names no pytest node ids or test paths"

        red_sha = ctx.conductor_svc._red_step_sha(task_id)
        if not red_sha:
            return [], ("no red-step commit resolved yet -- "
                        "write_failing_tests hasn't landed a tests-only commit")

        fresh = osp.fresh_red_receipt(project, task_id, red_sha, spec.spec_hash())
        if fresh is None:
            return [], ("no fresh red receipt for the current red-step commit "
                        f"({red_sha[:12]}) -- nothing observed failing there yet")

        return pinned, f"red demonstrated at {red_sha[:12]}: {fresh.reason}"
    except Exception as exc:                                   # never wedge a stall
        return [], f"codified red-test-id read failed: {exc}"


def _handle_stall(task_svc, task_id: str, step_id: str,
                  project: str = "") -> dict:
    """Fourth tick on a stalled step: close if shipped, else decompose or
    block, never invoke.

    THE SHIPPED CASE FIRST (live waste, 2026-08-29): task 4cbac65a's fix
    landed on origin/main as b2f8d88f, out of band. The runner kept driving
    its verify_green_state step, stalled, and split it into SIX children --
    one per pinned test -- every one of those tests already green on main.
    Six pieces of work that could only ever be waste, in a queue that
    drives one step per tick. A task whose own trailer is reachable from
    origin/main is finished; the honest move is to close it.
    """
    if _stall_work_is_shipped(task_id):
        sha = _shipped_sha_for_stall(task_id)
        # NEVER CLOSE OVER AN UNDECIDED GATE (task 8fbd5cf0, 2026-08-30).
        # TaskService refuses status=done while a gate is open, but that
        # guard reads is_open_gate_step(), which only fires when the
        # workflow_step ITSELF is a gate. A rewind moves the task back to an
        # AGENT step and leaves gate_state='pending' behind, so the row
        # carries an open gate the guard cannot see -- and this path calls
        # task_svc.update directly, past the route-level check. Observed
        # live: 8fbd5cf0 went status=done, workflow_step=implement_tasks,
        # gate_state=pending, i.e. closed while its green_gate had never
        # been decided. Shipped-ness is not a verdict; a gate is.
        # SHIPPED IS NOT ADJUDICATED (task 8fbd5cf0, closed falsely THREE
        # times on 2026-08-30). Checking only for an OPEN gate is not enough:
        # a task sitting on an AGENT step has gate_state="none", so this path
        # closed it on shipped-ness alone while its driver was still working
        # and its green_gate had never been decided. Observed at 08:53:05 —
        # the runner closed the ticket while its driver was actively landing
        # commits (06b6b28c, 5ed871ff) minutes earlier. Require a real
        # green_gate PASS in this task's own history before shipped-ness may
        # close it. A trailer reaching origin/main says the work exists; only
        # a gate decision says anybody judged it.
        _t_now = task_svc.get(task_id)
        _gate_state = str(getattr(_t_now, "gate_state", "") or "none")
        if not _green_gate_ever_passed(task_svc, task_id):
            reason = (f"step {step_id} did not advance after "
                      f"{STALL_ATTEMPTS} attempts and this task's work is "
                      f"on origin/main ({sha[:12]}), but its green_gate has "
                      f"never been decided — refusing to close. Shipped is "
                      f"not adjudicated.")
            task_svc.record_history(task_id, action="runner_stall",
                                    details=reason, actor=SEAT_ID)
            return {"ok": False, "task_id": task_id, "step": step_id,
                    "run_id": "", "tokens": 0, "cost_usd": 0.0,
                    "report": None, "reason": reason}
        if _gate_state in ("pending", "failed"):
            reason = (f"step {step_id} did not advance after "
                      f"{STALL_ATTEMPTS} attempts and this task's work is "
                      f"on origin/main ({sha[:12]}), but its gate is still "
                      f"undecided (gate_state={_gate_state}) -- refusing to "
                      f"close. Decide the gate first; shipped is not the "
                      f"same as adjudicated.")
            task_svc.record_history(task_id, action="runner_stall",
                                    details=reason, actor=SEAT_ID)
            return {"ok": False, "task_id": task_id, "step": step_id,
                    "run_id": "", "tokens": 0, "cost_usd": 0.0,
                    "report": None, "reason": reason}
        reason = (f"step {step_id} did not advance after {STALL_ATTEMPTS} "
                  f"attempts, but this task's work is already on "
                  f"origin/main ({sha[:12]}) -- closing instead of "
                  f"splitting it into children that cannot change anything.")
        task_svc.update(task_id, status="done", full_outcome_complete=True,
                        blocked_reason="")
        task_svc.record_history(task_id, action="runner_stall",
                                details=reason, actor=SEAT_ID)
        return {"ok": True, "task_id": task_id, "step": step_id, "run_id": "",
                "tokens": 0, "cost_usd": 0.0, "report": None,
                "stalled": {"action": "shipped", "children": [],
                            "reason": reason}}

    """Fourth tick on a stalled step: decompose or block, never invoke."""
    parent = task_svc.get(task_id)
    # ONLY THIS TASK'S OWN RED TESTS. `completion_proof` is a task-level
    # field holding whatever the last report wrote, INCLUDING a verify step's
    # note about unrelated suites that were already failing. Live on task
    # 72ccaf94 (2026-08-29) its proof named ten node ids from test_auto_updater,
    # test_lifespan_lock_recovery, test_pidfile_lifecycle and
    # test_task_page_bundle -- none of them this slice's work, none of them in
    # its `verify` -- and the splitter made a child for every one, then made
    # them AGAIN on the next stall: 30 children, 3 duplicate sets, all junk.
    # Intersect with the task's own pinned suite so a child can only ever be
    # created for a test this task is actually responsible for.
    pinned = [str(v) for v in (getattr(parent, "verify", []) or [])]

    def _is_ours(test_id: str) -> bool:
        # A task that pins NOTHING keeps the original behaviour: there is no
        # ownership signal to filter on, and refusing to split there would
        # retire a contract this change has no quarrel with.
        if not pinned:
            return True
        # FILE against FILE on BOTH sides. `pinned` entries are whatever
        # task.verify holds, which is routinely a `path.py::test_name` node
        # id -- comparing a bare file basename against that full string never
        # matched, so every id was filtered out and the splitter parked a task
        # that had named its red tests perfectly well. Live shape: task
        # cdb8e365 pins five ::-qualified ids.
        f = test_id.split("::", 1)[0].split("/")[-1]
        return any(f == p.split("::", 1)[0].split("/")[-1] for p in pinned)

    ids = [i for i in red_test_ids(getattr(parent, "completion_proof", "") or "")
           if _is_ours(i)]
    # THE PROSE IS NOT THE AUTHORITY. When the agentic step's report happens
    # not to retype a pytest node id, fall back to the CODIFIED read: the
    # pinned ids this task already declares, confirmed red by a fresh receipt
    # at its own anchor. Without this, a task with everything it needs sitting
    # in the database still parked for a human -- 6a7105f9 and 0b5dd37c were
    # both blocked that way on 2026-09-05, and task 404ef4ce built the node
    # that answers it (owner: "making maximum codified nodes from agentic
    # blocks so we can not stall"). Consulted ONLY when the prose names
    # nothing, so the existing path keeps its behaviour unchanged.
    # SCOPE: only meaningful for steps that can have red tests (write_failing_tests,
    # implement_tasks, verify_green_state). For document-writing steps
    # (review_previous_notes, draft_story, verify_plan), a red read tells the
    # owner to hunt for tests steps before any test ever exists.
    codified_reason = ""
    if not ids and project and step_id in _RED_TEST_STEPS:
        # No project => nothing to read; stay a strict no-op so the
        # pre-existing message and behaviour are untouched.
        _codified, codified_reason = _codified_red_test_ids(project, task_id)
        ids = [i for i in _codified if _is_ours(i)]
    # IDEMPOTENT. A stall that fires twice must not double the board: skip any
    # test id an OPEN child already covers. 72ccaf94 reached 30 children as
    # three identical sets of ten before this existed.
    existing: set = set()
    try:
        for c in (task_svc.list(parent_id=task_id) or []):
            if getattr(c, "status", "") in ("cancelled", "done"):
                continue
            for v in (getattr(c, "verify", []) or []):
                existing.add(str(v))
    except Exception:
        pass
    ids = [i for i in ids if i not in existing]
    children: list[str] = []
    for test_id in ids:
        child = task_svc.create(
            title=f"Make {test_id.rsplit('::', 1)[-1]} green",
            description=f"Split from {task_id} at step {step_id}: "
                        f"{test_id} stayed red for {STALL_ATTEMPTS} attempts.",
            priority=getattr(parent, "priority", 0) or 0,
            channel="daemon",
            tags=list(getattr(parent, "tags", []) or []),
            parent_id=task_id, verify=[test_id], proof_type="test",
            oracle=f"pytest {test_id} passes with rc 0.")
        children.append(child.id)
    action = "decomposed" if children else "blocked"
    reason = (f"step {step_id} did not advance after {STALL_ATTEMPTS} "
              f"attempts; ")
    if children:
        reason += f"split into children: {', '.join(children)}"
    elif _last_outcome_was_a_kill(task_svc, task_id, step_id):
        # HONESTY. exit=-9 is SIGKILL: the step ran out of wall clock (or
        # the host killed it), it did not fail to name a test. Epic
        # 9f60a849 reported "no red test id was named in the last proof"
        # three times for a step that was being killed at the 900 s bound,
        # which sends whoever reads it hunting for a test problem that does
        # not exist. Name the real thing, and name the knob that changes it.
        # SIGKILL outranks everything at every step.
        # "KILLED" is the load-bearing word here and two other suites pin
        # it (test_slow_step_budget_and_honest_stall,
        # test_draft_story_node_runs_its_declared_plan): a killed step must
        # say so rather than blame a missing test id. What CHANGED in task
        # 1c42ee74 is the attribution and the advice. exit=-9 is PRISM's own
        # TIMEOUT_EXIT_CODE, not a host SIGKILL -- d5808cd1's seven timeouts
        # were first read as an OOM kill on a host with ~60 GB free -- and
        # "raise PRISM_TASK_RUNNER_STEP_TIMEOUT_S" was the wrong first move:
        # the step was spending its budget on a toolset its declared plan
        # never asked for.
        reason += (f"the step was KILLED before it reported (exit=-9, "
                   f"PRISM's own timeout, not the host) -- it ran past its "
                   f"{int(_step_timeout_s(step_id))}s budget. Narrow what "
                   f"this step has to run: check that its declared node "
                   f"plan is in force, because a step handed tools it does "
                   f"not need spends the budget on them")
    elif step_id in _RED_TEST_STEPS and codified_reason:
        # Says what the DETERMINISTIC read found, not just what the prose
        # lacked -- the real answer is one of the codified node's own reasons
        # (no anchor yet, no fresh receipt, not pytest-backed), which names
        # the next action. Keeps the canonical opening phrase verbatim: it is
        # this stall's stable, asserted-on wording, and only a KILL is
        # allowed to replace it (see _last_outcome_was_a_kill above).
        reason += ("no red test id was named in the last proof, and the "
                   f"codified read found none either: {codified_reason}")
    elif step_id in _RED_TEST_STEPS:
        reason += "no red test id was named in the last proof"
    else:
        # For non-red-test steps (review_previous_notes, draft_story,
        # verify_plan), the step did not produce a usable report. These steps
        # write documents, not tests, so no red test id could exist here.
        reason += ("the step did not produce a usable report. This step writes "
                   "a document rather than tests, so no red test id could exist yet")
    task_svc.update(task_id, status="blocked", blocked_reason=reason)
    task_svc.record_history(task_id, action="runner_stall",
                            details=reason, actor=SEAT_ID)
    return {"ok": True, "task_id": task_id, "step": step_id, "run_id": "",
            "tokens": 0, "cost_usd": 0.0, "report": None,
            "stalled": {"action": action, "children": children,
                        "reason": reason}}


def run_one_step(project: str, task_id: str) -> dict:
    """Drive exactly one AGENT step of `task_id` end to end: fetch the
    job via `flow_start`, invoke `claude_cli` against the task's OWN
    worktree, and report the outcome via `flow_report`. Never raises —
    every failure path returns a result dict so a tick loop stays alive.
    """
    if not _claim(task_id):
        return {"ok": False, "task_id": task_id,
                "reason": "already in flight"}
    try:
        return _run_one_step(project, task_id)
    finally:
        _release(task_id)



def _claim_service(project: str):
    """The per-task lease, or None when it cannot be built.

    Returns None rather than raising: a seat that cannot reach the claims db
    must still be able to drive, exactly as it did before this wiring. The
    lease is a safety net, never a new single point of failure.
    """
    try:
        from prism_service.services.claim_service import ClaimService

        return ClaimService(db_path=_scores_db_for(project))
    except Exception:
        return None


def _run_one_step(project: str, task_id: str) -> dict:
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace

    task_svc = get_project(project).task_svc
    started = flow.flow_start(
        flow.Ident(task_id=task_id, session_id=SEAT_ID), project=project)
    if not started.get("ok"):
        return {"ok": False, "task_id": task_id,
                "reason": started.get("error") or "flow_start refused"}
    job = started.get("job")
    if not job or job.get("kind") == "gate":
        return {"ok": False, "task_id": task_id,
                "reason": "no eligible agent job (gate or terminal)"}

    if _stall_count(task_svc, task_id, job["step"]) >= STALL_ATTEMPTS:
        return _handle_stall(task_svc, task_id, job["step"], project=project)

    ws = task_workspace.workspace_for(task_id) or {}
    work_dir = ws.get("path")
    if not work_dir:
        return {"ok": False, "task_id": task_id, "step": job["step"],
                "reason": "no workspace on file for task"}

    from prism_service.inference import claude_cli
    from prism_service.services import drive_heartbeat

    # ONE DRIVER PER TASK WORKTREE (task 1bcb2b24). On 2026-08-30 two
    # `claude -p` processes ran the same step against this same directory --
    # same index, same HEAD -- and a test file was overwritten mid-write while
    # an agent authored it. _foreign_driver_on above is not enough: it sees a
    # driver only if that driver posts a HEARTBEAT, and it checks once rather
    # than for the life of the run. ClaimService's partial unique index makes
    # a second claim fail closed inside sqlite, and reaps an expired lease so
    # a crashed holder cannot wedge the task.
    claim = _claim_service(project)
    claim_id = None
    if claim is not None:
        claim_id = claim.acquire(
            task_id, holder_id=SEAT_ID, ttl_s=_step_timeout_s(job["step"]))
        if claim_id is None:
            holder = claim.holder_of(task_id) or "another driver"
            # Losing the race is not a failure: somebody else is doing the
            # work. Skip without touching the task's step or status.
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "reason": f"already driving: held by {holder}"}

    scores_db = _scores_db_for(project)
    drive_heartbeat.record_heartbeat(scores_db, {
        "task_id": task_id, "step": job["step"], "elapsed_s": 0,
        "last_tool": "claude_cli.invoke", "work_units": 1,
        "driver": RUNNER_DRIVER,
    })
    # A node's DECLARED plan governs this invoke (task 8848089d): the
    # codified sub-steps run here as Python at zero tokens, and the agentic
    # middle gets the declared model/turns/budget instead of the blanket
    # 30-turn / $2.00 default. `plan` is None for every step without a
    # credible declaration, and then nothing below changes behaviour.
    plan = _node_plan(project, job["step"])
    prompt = job["instructions"]
    run_id = str(uuid.uuid4())
    # Bound before the branch: the citation-check/render step below reads
    # both, and a NameError there would be swallowed as "no repair" rather
    # than surfacing. Load task for any plan so both premise-gather and
    # draft_story can access task.title/description for their prompts.
    task = None
    facts: list = []
    if plan:
        ctx = get_project(project)
        task = task_svc.get(task_id)
    if plan and "premise-gather" in (plan.get("codified") or []):
        ctx = get_project(project)
        preamble, facts = _codified_preamble(
            project, task, memory_svc=getattr(ctx, "memory_svc", None),
            task_svc=task_svc, brain_svc=getattr(ctx, "brain_svc", None))
        if preamble:
            prompt = preamble + "\n\n" + prompt
        _record_codified_run(
            project, task_id, "premise-gather", run_id, bool(facts),
            f"resolved {len(facts)} grounded facts")

    # RUN THE DECLARED MIDDLE, NOT A MONOLITH. When the node declares an
    # agentic route and the gather produced material for it, this step runs
    # the narrow prompt that route was written for -- with its own caps and
    # NO tools, since every citation is already in hand. That is the whole
    # overrun: a ~2-minute declared step was being run as a 30-turn,
    # full-brief, full-toolset call. It is also why the canvas showed
    # nothing -- the declared sub-step now records a run of its own.
    # PROGRAMMATIC FIRST, THE MODEL LAST. For this node the deterministic
    # chain is COMPLETE on its own: `gather` resolves the citations and
    # `render` builds a Premises section that satisfies both teeth the
    # rubric actually scores. When it does, the step is finished here --
    # zero tokens, sub-second, and no `claude -p` at all. The model is the
    # fallback for the case the programmatic path cannot answer (nothing
    # gathered), not the default route.
    # The codified branch never invokes a model, so it never has a budget;
    # an empty mapping keeps the shared failure-reporting path below able to
    # ask for one without an unbound-name crash.
    budget: dict = {}
    # Bound before the branch so the dispatch check below can never read an
    # unassigned name on the codified path.
    dispatched: Optional[list] = None
    narrow_prompt: str = ""
    codified_proof = _codified_step_proof(job["step"], task, facts)
    if codified_proof:
        for route in ("premise-select", "premise-render",
                      "premise-citation-check"):
            _record_codified_run(
                project, task_id, route, run_id, True,
                f"resolved {job['step']} programmatically from "
                f"{len(facts)} gathered fact(s) -- no model call")
        result = _CodifiedResult(codified_proof)
    else:
        # Still not the old monolith: when the node declares a narrow middle
        # and there is material for it, run THAT prompt with the caps it was
        # written for and no tools, rather than the whole step brief at 30
        # turns with the full toolset.
        narrow_prompt = _declared_agentic_prompt(
            job["step"], task, facts, plan=plan)
        # RUN THE DECLARED FLOW (task eda5a843). When the node declares a
        # real chain, execute its steps instead of lifting the prompt out and
        # calling the model directly: only the declared route supplies the
        # json_schema that forbids a chat transcript and the rubric that
        # scores what came back. Falls through to the inline call below
        # whenever the chain cannot run, so a bad declaration is never worse
        # than today.
        dispatched = None
        if narrow_prompt and _runs_as_declared_steps(job["step"], plan):
            # Pass the raw task material to the dispatcher, not the
            # already-substituted prompt. _dispatch_declared_steps substitutes
            # ${taskHint} once; handing it the substituted prompt nests the
            # whole instruction inside its own placeholder. Observed live in
            # /proc/<pid>/cmdline on task b490fabc: the prompt opened "Draft a
            # failing test ... for this task: Draft a failing test ... for this
            # task: A running step agent ...". verify_plan shares this path and
            # had the same duplication.
            task_hint = _build_task_hint(task)
            dispatched = _dispatch_declared_steps(
                project, plan,
                variables={"taskHint": task_hint, "taskId": task_id,
                           "project": project})
            for row in dispatched:
                route = row.get("route") or "?"
                if route in _SELF_RECORDING_ROUTES:
                    continue  # the route already recorded its own run
                _record_codified_run(
                    project, task_id, route, run_id,
                    bool(row.get("ok")),
                    row.get("reason") or "ran as a declared step")
            result = _result_from_dispatch(dispatched)
            if result is None:
                dispatched = None
        # A DRAFT IS NOT A SUBSTITUTE FOR THE STEP (task ab9166d5). The
        # write-failing-tests narrow prompt says "do NOT write it to disk",
        # so running it inline returns prose with exit 0 -- which _route_proof
        # would store and the step would ADVANCE on, with no tests-only commit
        # and no red anchor. That is the misfire this ticket names, and it is
        # reachable only when the chain did not run. Drop the narrow prompt so
        # the step keeps its pre-change behaviour (the full brief) instead of
        # going fast and green on nothing.
        if dispatched is None and job["step"] in _DRAFT_ONLY_WITHOUT_CHAIN:
            narrow_prompt = ""
        if dispatched is None and narrow_prompt:
            prompt = narrow_prompt
            _record_codified_run(
                project, task_id, str(plan.get("agentic") or "premise-judge"),
                run_id, True,
                f"fell back to the node's declared agentic middle over "
                f"{len(facts)} gathered fact(s)")
    if not codified_proof and dispatched is None:
        # THE CHOKEPOINT (task ab9166d5 incident, dispatch_guard.py):
        # re-check, fresh, right before the GPU actually spends anything --
        # the claim above only proves no OTHER driver holds this task, it
        # says nothing about whether this task is still supposed to be
        # driven at all (a park can land between eligibility and here).
        # This is also the one place a REAL dispatch is counted, shared
        # across task_runner and resume_actuator, so the ceiling it
        # enforces cannot disagree with what actually ran.
        from prism_service.services import dispatch_guard
        ticket, refusal = dispatch_guard.try_begin(
            project, task_id, job["step"], SEAT_ID)
        if ticket is None:
            if claim is not None:
                claim.release(claim_id)
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "reason": refusal or "dispatch refused"}
        budget = _invoke_budget(
            job["step"], plan, narrow=bool(narrow_prompt),
            after_kill=_last_outcome_was_a_kill(
                task_svc, task_id, job["step"]))
        try:
            result = claude_cli.invoke(
                prompt, work_dir=work_dir, plugin_dir=work_dir,
                allowed_tools=() if narrow_prompt else BUILD_TOOLS,
                project=project,
                purpose=f"task-runner@{job['step']}#{task_id[:8]}",
                session_id=str(uuid.uuid4()), **budget)
        except Exception as exc:
            dispatch_guard.end_dispatch(ticket)
            if claim is not None:
                claim.release(claim_id)
            return {"ok": False, "task_id": task_id, "step": job["step"],
                    "reason": f"claude_cli invocation failed: {exc}"}
        dispatch_guard.end_dispatch(ticket)

    # The run's OWN usage, straight off the `result` stream event that
    # claude_cli already parsed for us. This seat reports under a SEAT NAME,
    # not a session UUID, so nothing downstream can recover these figures from
    # a transcript -- carrying them here is the only way a bot step's tokens,
    # model and cost ever reach its agent_runs row (task 9a51e670).
    # getattr-guarded: a lighter result object must never break the drive.
    usage = getattr(result, "usage", None)
    usage = dict(usage) if isinstance(usage, dict) and usage else None
    # Charged IMMEDIATELY after invoke() returns (epic 3baadd19 AC-2/AC-6):
    # this step already started, so it must still complete and report --
    # only the FOLLOWING tick's pre-claim check sees the crossed ceiling.
    if usage and usage.get("cost_usd"):
        _spend_tracker.charge(usage["cost_usd"])

    proof = (result.final_text() or "").strip()
    step_id = job["step"]
    # A graceful budget/turn ceiling (exit!=0 but the model's own turn
    # ended normally -- see ClaudeCliResult.graceful_budget_stop) still
    # carries a complete, usable report and must not be discarded just
    # because the post-hoc budget check marked the run is_error. Any OTHER
    # non-zero exit (crash, auth failure, truncation mid-generation) keeps
    # failing exactly as before.
    # The codified CHECK sub-step (task 8848089d): verify the report's own
    # shape with the SAME regexes the story_gate rubric uses, in-process and
    # at zero tokens, so a report that cites nothing is caught here rather
    # than one full gate sweep later. Advisory -- it records what it found
    # and never fails a step the model completed, because premise_grounded
    # remains the seat that actually decides.
    if plan and "premise-citation-check" in (plan.get("codified") or []) and proof:
        try:
            from prism_service.services import premise_gather

            verdict = premise_gather.citation_check(proof)
            _record_codified_run(
                project, task_id, "premise-citation-check", run_id,
                bool(verdict.get("ok")), str(verdict.get("reason") or ""))
            # ADVISORY WAS NOT ENOUGH. This check already knew the report
            # cited nothing and let the step park anyway -- premise_grounded
            # refused 273 advances across 141 tasks that way (measured
            # 2026-09-05), each one burning a whole drive over facts the
            # gather had already resolved. Complete the report from those
            # facts instead of reporting a refusal nobody acts on.
            if not verdict.get("ok"):
                repaired = _repair_premises(proof, task, facts)
                if repaired != proof:
                    proof = repaired
                    _record_codified_run(
                        project, task_id, "premise-render", run_id, True,
                        "rendered the grounded premises from the codified "
                        "gather; the model's own notes are kept below them")
        except Exception:
            pass

    if proof and (result.exit_code == 0 or result.graceful_budget_stop()):
        _route_proof(task_svc, task_id, step_id, proof)
        # CODIFIED RED TEST IDS ON LIVE PATH. When write_failing_tests
        # succeeds, consult the deterministic red-test-ids node to make those
        # ids AVAILABLE for downstream consumers (gates, stall handler) without
        # requiring a model to retype them. Previously this was ONLY called
        # inside _handle_stall, i.e. after a task had already stalled (task
        # 404ef4ce: make codified nodes from agentic blocks so we can not
        # stall). Now it runs on the live path, so the ids are available
        # immediately without waiting for a stall.
        if step_id == "write_failing_tests" and project:
            codified_ids, codified_reason = _codified_red_test_ids(project, task_id)
            if codified_ids:
                # Enhance the stored completion_proof with the authoritative ids
                # so red_test_ids() can extract them without calling
                # _codified_red_test_ids again. Use a format the regex can parse.
                enhanced_proof = proof + (
                    f"\nCodified red test ids (from oracle spec + red anchor):\n"
                    + "\n".join(f"  {i}" for i in codified_ids))
                task_svc.update(task_id, completion_proof=enhanced_proof)
        outcome: object = "pass"
    else:
        # ONE reason builder for every failure shape. It used to be two
        # branches keyed on whether `proof` was empty, which is why a timeout
        # carrying the model's empty `<think></think>` wrapper was reported as
        # a crash -- see _failure_reason.
        outcome = {"ok": False,
                   "reason": _failure_reason(
                       result,
                       float(budget.get("timeout_s")
                             or _step_timeout_s(step_id)),
                       proof=proof)}

    if claim is not None:
        claim.release(claim_id)

    report = flow.flow_report(flow.Ident(
        task_id=task_id, session_id=SEAT_ID, outcome=outcome,
        expected_step=step_id, usage=usage,
        model=(usage or {}).get("model") or None), project=project)
    if not report.get("advanced", report.get("ok")):
        task_svc.record_history(
            task_id, action=ATTEMPT_ACTION, actor=SEAT_ID,
            details=f"step={step_id}; advanced=false; proof={proof[:2000]}")
    return {"ok": bool(report.get("ok")), "task_id": task_id,
            "step": step_id, "run_id": result.run_id,
            "tokens": (int((usage or {}).get("input_tokens") or 0)
                       + int((usage or {}).get("output_tokens") or 0)),
            "cost_usd": float((usage or {}).get("cost_usd") or 0.0),
            "report": report}


def sweep_once() -> Optional[dict]:
    """One pass over every project, starting from a ROTATING offset: drive
    up to `_concurrency()` eligible tasks AT THE SAME TIME, then stop.

    This used to drive the first eligible task found and return -- at most
    one task per tick, globally. That bounded blast radius and also bounded
    THROUGHPUT to one step at a time for the only seat that can move the
    board unattended, which is why a ~90-task backlog stayed a ~90-task
    backlog. The blast radius is still bounded, just not at one: the host
    load breaker and the spend ceiling refuse the whole tick before anything
    starts, the count is capped by PRISM_TASK_RUNNER_CONCURRENCY, and every
    drive still goes through run_one_step's per-task claim lease.

    The starting project rotates every tick (module-level _rr_index) so no
    single project can monopolize the work slots forever just by sorting
    first and always having something eligible. Returns the first drive's
    result, or None when nothing was eligible.
    """
    from prism_service.project_context import get_all_projects

    if _spend_ceiling_crossed():
        return None
    if _system_overloaded():
        return None
    if _engine_unreachable():
        return None

    global _rr_index
    projects = get_all_projects()
    if not projects:
        return None
    with _RR_LOCK:
        start = _rr_index % len(projects)
    ordered = projects[start:] + projects[:start]

    limit = _concurrency()
    picked: list[tuple[str, str]] = []
    last_pid: Optional[str] = None
    for pid in ordered:
        if len(picked) >= limit:
            break
        try:
            found = eligible_tasks(pid, limit - len(picked))
        except Exception as exc:
            _log(f"{pid}: eligibility check failed: {exc}")
            continue
        if not found:
            continue
        last_pid = pid
        picked.extend((pid, tid) for tid in found)

    if not picked:
        return None

    with _RR_LOCK:
        # Next tick starts AFTER the LAST project this sweep served, so the
        # projects already drained go to the back of the queue rather than
        # being re-offered ahead of ones that got nothing. With the slots
        # filled from one project, that is the old one-project advance; with
        # them spread over several, it steps past all of them. Re-index
        # against the CURRENT `projects` list (not `ordered`) so a project
        # add/remove between ticks can't skew the offset.
        _rr_index = (projects.index(last_pid) + 1) % len(projects)

    if len(picked) == 1:
        pid, task_id = picked[0]
        res = run_one_step(pid, task_id)
        _log(f"{pid}/{task_id[:8]}: {res}")
        return res

    # DRIVEN AT THE SAME TIME, not merely in a tighter serial loop: each of
    # these blocks for minutes inside claude_cli, so overlapping them is the
    # whole point. Each still goes through run_one_step, so the per-task claim
    # lease, the worktree, and every gate tooth behave exactly as they do for
    # a single drive.
    from concurrent.futures import ThreadPoolExecutor

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(picked),
                            thread_name_prefix="prism-drive") as pool:
        futures = [(pid, tid, pool.submit(run_one_step, pid, tid))
                   for pid, tid in picked]
        for pid, tid, fut in futures:
            try:
                res = fut.result()
            except Exception as exc:                  # never kill the tick
                res = {"ok": False, "task_id": tid, "reason": str(exc)}
            _log(f"{pid}/{tid[:8]}: {res}")
            results.append(res)
    return results[0] if results else None


# A task becoming eligible (status -> in_progress, or a step/gate transition)
# is a REAL event -- task_service.update() already publishes it as
# "task.changed" for /sse/tasks. Until this existed, this runner's only
# way to notice was to wake up on a bare interval() and poll, so a task
# that became the ONLY eligible thing in the whole project could still
# sit untouched for the full interval (observed live: 900s/~16min wait for
# a single, uncontested task). `wake()` is called from that same publish
# path (task_service.py) so the loop below reacts to the real event instead
# of a fixed clock; the interval survives ONLY as a safety-net upper bound
# for events this module never learns about (a direct DB write, a crash
# recovery, etc.) -- never the primary trigger anymore.
_wake_event = threading.Event()


def wake() -> None:
    """Nudge the runner loop to sweep now instead of waiting out its
    interval. Safe to call even when the runner was never started
    (PRISM_TASK_RUNNER_INTERVAL unset) -- just sets a flag nobody reads."""
    _wake_event.set()


def _loop(interval_s: int, stop_event: Optional[threading.Event] = None) -> None:
    """`stop_event` is test-only plumbing (never passed by
    `start_task_runner`): without it, a test that spins up this loop in a
    background thread has no way to end it, so the thread outlives the
    test and its calls to the shared, module-level `sweep_once` keep
    firing on every later test's `wake()` -- observed live as exactly
    that leak in test_wake_cuts_the_wait_short_instead_of_sitting_out_the_
    interval, which used to cope by permanently stubbing `sweep_once` to
    a no-op instead, breaking every later test that calls it directly."""
    _log(f"started; interval={interval_s}s (event-driven, interval is a fallback)")
    while stop_event is None or not stop_event.is_set():
        try:
            sweep_once()
        except Exception as exc:
            _log(f"sweep error: {exc}")
        if _wake_event.wait(timeout=interval_s):
            _wake_event.clear()


def start_task_runner() -> Optional[threading.Thread]:
    """Spawn the task-runner daemon thread, unless disabled via
    PRISM_TASK_RUNNER_INTERVAL unset/<=0 (the default). Mirrors
    start_gate_adjudicator: same shape, same off-by-default posture."""
    interval = _interval_s()
    if interval <= 0:
        _log("disabled (default OFF; set PRISM_TASK_RUNNER_INTERVAL="
             "<seconds> to opt this environment in)")
        return None
    t = threading.Thread(target=_loop, args=(interval,),
                         name="prism-task-runner", daemon=True)
    t.start()
    return t
