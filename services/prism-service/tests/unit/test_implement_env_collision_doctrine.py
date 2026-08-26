"""Environment-collision (cwd-isolation leak) doctrine, pinned in source.

Twice in one real session, a step subagent spawned by implement.js had its
Bash tool unconditionally blocked mid-step with:

    This session is isolated in the worktree /home/.../.claude/worktrees/
    <some-other-worktree-name>, but this command's working directory
    resolved to the shared checkout... Refusing to run it there.

The worktree named belonged to a DIFFERENT, sibling agent in the same
session (once the MAIN session itself) -- never to the blocked step
subagent's own context, and the subagent was never told to use that
worktree or any worktree at all. ExitWorktree refused too ("cannot be
called from a subagent with a cwd override... This agent is already
isolated."), so the subagent was structurally unable to clear it itself.

This is a Claude Code harness/platform bug (filed separately as platform
feedback), NOT a PRISM or task defect, and NOT something implement.js can
root-cause-fix from inside this repo. The PRISM-side mitigation is to make
every step agent recognize the exact failure signature, stop cleanly instead
of flailing, and report a distinct, greppable halt reason instead of a vague
"couldn't complete this step" narrative.

The workflow scripts have no JS test runner (repo convention: UI/JS
behavior is pinned by asserting the ACTUAL source), so this doctrine is
pinned here by reading .claude/workflows/implement.js as raw text.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


def _source() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _env_collision_block(src: str) -> str:
    start = src.index("const ENV_COLLISION_DOCTRINE = [")
    end = src.index("].join('\\n')", start)
    return src[start:end]


def test_env_collision_doctrine_names_the_exact_signature():
    """The doctrine must recognize the literal refusal text observed live,
    and make clear the named worktree is never the agent's own."""
    block = _env_collision_block(_source())
    assert "This session is isolated in the worktree" in block, (
        "the doctrine must name the exact Bash-refusal signature so a step "
        "agent can recognize it verbatim, not paraphrase it"
    )
    assert "NEVER told to use" in block or "never told to use" in block, (
        "the doctrine must state that the named worktree is not one the "
        "agent was ever instructed to use - the distinguishing fact that "
        "separates this from a legitimate isolation refusal"
    )


def test_env_collision_doctrine_forbids_workarounds():
    """The doctrine must state plainly this is a known harness bug, never a
    task failure, and must forbid clever workarounds (retrying via another
    tool, trying to self-clear the isolation state) - a step subagent
    structurally cannot clear another agent's isolation (confirmed live by
    ExitWorktree's own refusal)."""
    block = _env_collision_block(_source())
    lowered = block.lower()
    assert "known" in lowered and (
        "harness" in lowered or "platform" in lowered
    ), "must call this out as a known harness/platform bug"
    assert "never a real task failure" in lowered or "never a task failure" in lowered
    assert "do not retry" in lowered or "not yours to retry" in lowered
    assert "exitworktree" in lowered, (
        "must cite the real ExitWorktree refusal as proof the agent cannot "
        "self-clear the isolation state"
    )
    assert "already isolated" in lowered, (
        "must quote the actual ExitWorktree refusal message"
    )


def test_env_collision_doctrine_names_a_distinct_greppable_halt_prefix():
    """The next reader (human or orchestrator) must be able to tell this
    halt apart from a generic step failure at a glance - a fixed, greppable
    prefix in halt_reason, not prose buried in a paragraph."""
    block = _env_collision_block(_source())
    assert "ENVIRONMENT COLLISION (cwd-isolation leak): " in block, (
        "the doctrine must specify the EXACT fixed halt_reason prefix a "
        "step agent must use, so the halt is greppable"
    )
    assert "halt_reason" in block
    assert "ok:false" in block, (
        "the doctrine must instruct the agent to report the step as failed "
        "(ok:false), not silently swallow the collision"
    )


def test_env_collision_doctrine_prescribes_fresh_relaunch_never_resume():
    """Recovery is a fresh relaunch of the whole drive once the collision has
    cleared - never resumeFromRunId, per this repo's own CLAUDE.md rule that
    cached pre-flight verdicts replay stale."""
    block = _env_collision_block(_source())
    assert "fresh relaunch" in block.lower() or "relaunch" in block.lower()
    assert "never resumeFromRunId" in block, (
        "must cite the existing never-resumeFromRunId rule rather than "
        "reinvent a new resume mechanism"
    )
    assert "mid-EnterWorktree" in block or "EnterWorktree" in block, (
        "recovery must be conditioned on no other agent still holding "
        "worktree isolation in the session"
    )


def test_env_collision_doctrine_reaches_self_heal_and_every_worker_step():
    """SELF_HEAL (injected into every workerPrompt/decompose/child-driver/
    locate/graph/settle agent via preamble()) must carry the doctrine, and
    the ladder intro must exempt it from the climb-the-ladder flow (there is
    no fix available to a step agent for this case)."""
    src = _source()
    self_heal_start = src.index("const SELF_HEAL = [")
    self_heal_end = src.index("].join('\\n')", self_heal_start)
    self_heal_block = src[self_heal_start:self_heal_end]
    assert "ENV_COLLISION_DOCTRINE" in self_heal_block, (
        "SELF_HEAL must inject ENV_COLLISION_DOCTRINE so every step agent "
        "that runs through preamble() carries it"
    )
    assert "NOT the environment collision" in self_heal_block, (
        "the self-heal ladder intro must explicitly exempt the environment "
        "collision case from the climb-the-ladder flow"
    )
    # ENV_COLLISION_DOCTRINE must be DEFINED before SELF_HEAL references it.
    doctrine_def_at = src.index("const ENV_COLLISION_DOCTRINE = [")
    assert doctrine_def_at < self_heal_start


def test_env_collision_doctrine_reaches_gate_prompt_too():
    """gatePrompt() builds its own prompt WITHOUT calling preamble(), so it
    does not inherit SELF_HEAL automatically - a gate agent's Bash calls
    (readiness curl, gate/mint curl) are exactly as exposed to the same
    harness collision as any worker step, so the doctrine must be injected
    directly into gatePrompt's own returned prompt array."""
    src = _source()
    gate_fn_at = src.index("function gatePrompt(job) {")
    next_fn_at = src.index("function workerPrompt(job) {")
    assert gate_fn_at < next_fn_at
    gate_block = src[gate_fn_at:next_fn_at]
    # gatePrompt's returned array must not open with a preamble(...) call the
    # way workerPrompt/decompose/child-driver do - if it starts doing so, the
    # standalone injection below would become a (harmless but redundant)
    # duplicate, so re-check whether it is still needed.
    assert "return [\n    preamble(" not in gate_block, (
        "this test's premise (gatePrompt's returned array does not open "
        "with a preamble() call) no longer holds - re-evaluate whether "
        "ENV_COLLISION_DOCTRINE still needs a direct injection here"
    )
    assert "ENV_COLLISION_DOCTRINE" in gate_block, (
        "gatePrompt must inject ENV_COLLISION_DOCTRINE directly since it "
        "never runs through preamble()/SELF_HEAL"
    )
