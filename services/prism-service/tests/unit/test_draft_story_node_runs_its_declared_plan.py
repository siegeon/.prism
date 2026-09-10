"""The draft story node runs its DECLARED plan, and a stall names the step.

`.prism/behaviors/conductor/draft-story-loop.json` declares a bounded middle:
haiku, 4 turns, $0.50, no tools, and a prompt written for the exact thing the
`story_complete` rubric scores — the three headings, an `AC-<n>` id on every
criterion, and an `oracle:` marker. `_PLANNED_STEPS` admitted only
`review_previous_notes`, so the drive worker sent the FULL step brief with the
30-turn / $2.00 / 900 s defaults and the whole BUILD_TOOLS set instead.

Measured on 2026-09-09 over the 349 recorded `draft_story` runs in
projects/prism/scores.db: median 75 s, p90 293 s, and 22 runs (6.3%) at or past
890 s — the 900 s wall, where the step is SIGKILLed and records nothing at all.
Task d5808cd1 hit that tail three times in a row (exit=-9 at 17:26, 18:07 and
18:22), then an exit=1, and blocked at `draft_story` with an empty `story_md`.

The second defect is what the card then SAID. `_codified_red_test_ids` is a
RED-TEST read, but the stall consulted it for every step, so a `draft_story`
stall told the owner "no red test id was named in the last proof ... write_
failing_tests hasn't landed a tests-only commit" — sending whoever reads it
hunting for a test problem that cannot exist three steps before any test.
That is the exact harm the SIGKILL branch was written to prevent, one branch
further down.

These tests assert the LITERAL declared figures and the LITERAL wording, never
a reader applied to its own output.
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from prism_service.services import task_runner as tr

# Same hermetic resolution as test_task_runner_honors_declared_node_plan.py:
# _behavior_dir's fallback is Path.home()/projects/<project>, which exists only
# on a dev machine. The catalog under test is the COMMITTED one in this
# checkout.
_REPO_BEHAVIORS = (Path(__file__).resolve().parents[4]
                   / ".prism" / "behaviors" / "conductor")


@pytest.fixture(autouse=True)
def _hermetic_behavior_dir(monkeypatch):
    monkeypatch.setattr(tr, "_behavior_dir", lambda project: _REPO_BEHAVIORS)


def _task():
    return types.SimpleNamespace(
        id="d5808cd1", title="A hand landed task still reaches the reap",
        description="The reap fires only from ship_worker.", oracle="",
        tags=[])


# ----------------------------------------------------------------------
# AC-1: the node's declared plan is READ
# ----------------------------------------------------------------------

def test_draft_story_carries_its_declared_budget():
    """The literal figures draft-story-loop.json declares — haiku / 4 / $0.50.

    Literal values, never `_node_plan(...)` compared against itself: a reader
    that silently fell back to the runner's 30-turn default would still pass a
    self-referential assertion.
    """
    plan = tr._node_plan("prism", "draft_story")

    assert plan is not None, "draft_story declares a plan and must opt in"
    assert plan["model"] == "haiku"
    assert plan["max_turns"] == 4
    assert plan["max_budget_usd"] == pytest.approx(0.5)


def test_draft_story_names_its_codified_substep():
    """text-challenge is codified; the reason-loop middle is the agentic one."""
    plan = tr._node_plan("prism", "draft_story")

    assert "text-challenge" in plan["codified"]
    assert plan["agentic"] == "reason-loop"
    assert "reason-loop" not in plan["codified"]


# ----------------------------------------------------------------------
# AC-2: the DECLARED PROMPT is what runs — and needs no gathered facts
# ----------------------------------------------------------------------

def test_the_draft_story_prompt_is_the_declared_one():
    """The narrow prompt names the three headings and the AC/oracle markers.

    These literals are what the `story_complete` rubric scores. The full step
    brief the worker used to send names none of them, which is why five
    attempts produced an empty `story_md`.
    """
    prompt = tr._declared_agentic_prompt("draft_story", _task(), [])

    assert prompt, "draft_story's declared middle has a prompt of its own"
    assert "## Summary" in prompt
    assert "## Requirements" in prompt
    assert "## Acceptance Criteria" in prompt
    assert "AC-1" in prompt
    assert "oracle:" in prompt
    # the task under draft is the material
    assert "A hand landed task still reaches the reap" in prompt


def test_the_draft_story_prompt_needs_no_gathered_facts():
    """draft_story has no premise-gather, so an empty fact list is NORMAL.

    review_previous_notes returns "" with no facts because its narrow prompt is
    ABOUT the gathered citations. draft_story's material is the task itself, so
    gating it on facts would keep it on the 30-turn full-brief path forever.
    """
    assert tr._declared_agentic_prompt("draft_story", _task(), []) != ""


# ----------------------------------------------------------------------
# AC-3: prompt and caps are adopted TOGETHER, never one without the other
# ----------------------------------------------------------------------

def test_the_declared_caps_are_adopted_with_the_declared_prompt():
    """haiku / 4 turns / $0.50 — the caps the declared prompt was sized for."""
    plan = tr._node_plan("prism", "draft_story")
    kwargs = tr._invoke_budget("draft_story", plan, narrow=True)

    assert kwargs["model"] == "haiku"
    assert kwargs["max_turns"] == 4
    assert kwargs["max_budget_usd"] == pytest.approx(0.5)


def test_the_declared_caps_are_REFUSED_without_the_declared_prompt():
    """Task 6a7105f9 (2026-08-30): declared caps applied to the FULL brief
    spent every turn on "Let me fetch the task details..." and died exit=1.
    Only the model is safe to adopt when the prompt is the wide one."""
    plan = tr._node_plan("prism", "draft_story")
    kwargs = tr._invoke_budget("draft_story", plan, narrow=False)

    assert kwargs["model"] == "haiku"
    assert kwargs["max_turns"] == tr._max_turns()
    assert kwargs["max_budget_usd"] == pytest.approx(tr._max_budget_usd())


def test_the_declared_plan_never_shortens_the_wall_clock():
    """The declared 120 s must not cap what the runner allows: a too-small
    timeout kills a drive outright, a too-large one costs nothing once the
    turn and budget caps bind first."""
    plan = tr._node_plan("prism", "draft_story")
    kwargs = tr._invoke_budget("draft_story", plan, narrow=True)

    assert kwargs["timeout_s"] >= tr._step_timeout_s("draft_story")


# ----------------------------------------------------------------------
# AC-4: a stall names the step it actually stalled on
# ----------------------------------------------------------------------

class _StallTask:
    id = "t-1"
    verify: list = []
    completion_proof = ""
    proof_type = "test"
    oracle = ""
    likely_misfire = ""
    priority = 10
    tags: list = []
    status = "in_progress"
    gate_state = "none"


class _StallSvc:
    def __init__(self):
        self.updates = []
        self.hist = []

    def get(self, _tid):
        return _StallTask()

    def list(self, **_kw):
        return []

    def update(self, tid, **kw):
        self.updates.append((tid, kw))

    def record_history(self, tid, **kw):
        self.hist.append((tid, kw))

    def history(self, _tid):
        return []


RED_REASON = ("no red-step commit resolved yet -- write_failing_tests hasn't "
              "landed a tests-only commit")


@pytest.fixture
def stall(monkeypatch):
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _t: False)
    monkeypatch.setattr(tr, "_last_outcome_was_a_kill", lambda *_a, **_k: False)
    monkeypatch.setattr(tr, "_codified_red_test_ids",
                        lambda *_a, **_k: ([], RED_REASON))
    return tr


def _reason(svc):
    for _tid, kw in svc.updates:
        if kw.get("status") == "blocked":
            return kw.get("blocked_reason", "")
    return ""


def test_a_pre_test_stall_does_not_blame_the_red_tests(stall):
    """THE LIVE SHAPE (task d5808cd1, 2026-09-09). The card told the owner
    write_failing_tests had landed no tests-only commit, on a task that had
    never left draft_story. No red test can exist three steps before any test
    is written, so the red read must not be consulted here at all."""
    svc = _StallSvc()
    stall._handle_stall(svc, "t-1", "draft_story", project="prism")
    reason = _reason(svc)

    assert "draft_story" in reason
    assert "write_failing_tests" not in reason
    assert "no red test id was named" not in reason
    assert RED_REASON not in reason


@pytest.mark.parametrize("step", ["write_failing_tests", "implement_tasks",
                                  "verify_green_state"])
def test_a_build_step_stall_still_reads_the_codified_red_ids(stall, step):
    """THE GUARD AGAINST OVER-SCOPING. Task 404ef4ce built the codified red
    read precisely so a build-step stall names the next action instead of
    parking for a human. Narrowing it to the steps where a red test can exist
    must not retire it where it earns its keep."""
    svc = _StallSvc()
    stall._handle_stall(svc, "t-1", step, project="prism")

    assert RED_REASON in _reason(svc)


def test_a_killed_pre_test_step_still_says_it_was_killed(monkeypatch):
    """The SIGKILL branch outranks both: only a kill may replace the opening
    wording (epic 9f60a849), and that stays true for a pre-test step."""
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _t: False)
    monkeypatch.setattr(tr, "_last_outcome_was_a_kill", lambda *_a, **_k: True)
    svc = _StallSvc()
    tr._handle_stall(svc, "t-1", "draft_story", project="prism")

    assert "KILLED" in _reason(svc)


# ----------------------------------------------------------------------
# AC-5: the live-path defect — task must be loaded for draft_story
# ----------------------------------------------------------------------

def test_the_draft_story_prompt_refuses_empty_task_hint():
    """Task None means the material-less refusal works (the live-path defect).

    In _run_one_step, task is None until it is explicitly loaded. For
    draft_story, that used to never happen: its codified route is
    'text-challenge', not 'premise-gather', so the task stayed None and
    getattr(None, "title", "") returned empty string. The prompt then held no
    task material at all, strictly worse than the full brief.

    A narrow prompt with no material must refuse, not render hollow.
    """
    prompt = tr._declared_agentic_prompt("draft_story", None, [])

    assert prompt == "", "prompt must refuse when task is None (empty hint)"


def test_the_live_path_loads_task_for_draft_story(monkeypatch, tmp_path):
    """LIVE-PATH TEST: task is loaded for draft_story, so prompt holds its
    title. This test FAILS on bc524cb8 (before the fix) and PASSES after.

    On the defect (bc524cb8), draft_story's codified route is 'text-challenge',
    not 'premise-gather', so the task load block is skipped and task stays
    None. Then _declared_agentic_prompt returns "" (refusing empty hint),
    prompt stays the full brief, and the task title is NOT in it.

    After the fix, task IS loaded for any plan, so task_hint is non-empty,
    and draft_story's narrow prompt DOES contain the real title.
    """
    from prism_service.api import conductor_flow as flow
    from prism_service.inference import claude_cli
    from prism_service.services import task_workspace

    seen = {}

    monkeypatch.setattr(flow, "flow_start", lambda *a, **k: {
        "ok": True, "job": {"step": "draft_story", "kind": "agent",
                            "instructions": "DO THE STEP"}})
    monkeypatch.setattr(flow, "flow_report",
                        lambda *a, **k: {"ok": True, "advanced": True})
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(tmp_path)})
    monkeypatch.setattr(tr, "_stall_count", lambda *a, **k: 0)
    monkeypatch.setattr(tr, "_route_proof", lambda *a, **k: None)
    monkeypatch.setattr(tr, "_claim_service", lambda _proj: None)

    # Patch the project context to return a fake task with real title/description.
    # This is what the fix checks: the task gets loaded and passed to _declared_agentic_prompt.
    class FakeTaskSvc:
        def get(self, task_id):
            return _task()

        def list(self, **_kw):
            return []

        def record_history(self, *_a, **_kw):
            pass

    class FakeCtx:
        def __init__(self):
            self.task_svc = FakeTaskSvc()
            self._data_dir = tmp_path

        def __getattr__(self, name):
            return None

    def fake_get_project(_proj):
        return FakeCtx()

    # The get_project function is imported lazily inside _run_one_step,
    # so patch it at the project_context module level where it's actually used.
    import prism_service.project_context
    monkeypatch.setattr(prism_service.project_context, "get_project",
                        fake_get_project)

    # Patch drive_heartbeat to do nothing.
    from prism_service.services import drive_heartbeat
    monkeypatch.setattr(drive_heartbeat, "record_heartbeat",
                        lambda *a, **k: None)

    # Capture invoke's arguments.
    class FakeResult:
        exit_code = 0
        run_id = "r-live"
        usage = None

        def final_text(self):
            return '{"story_md": "## Summary\nFake story"}'

        def graceful_budget_stop(self):
            return False

    def fake_invoke(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return FakeResult()

    monkeypatch.setattr(claude_cli, "invoke", fake_invoke)

    # Drive the step.
    tr._run_one_step("prism", "task-id")

    # On bc524cb8 (defect): narrow_prompt is "" (task None, task_hint empty,
    # refused), so prompt stays full brief without the task title.
    # After fix: narrow_prompt is the declared one with the task title.
    prompt_text = seen.get("prompt", "")
    assert "A hand landed task still reaches the reap" in prompt_text, (
        f"Task title NOT in prompt (defect not fixed?). Prompt:\n{prompt_text}")
