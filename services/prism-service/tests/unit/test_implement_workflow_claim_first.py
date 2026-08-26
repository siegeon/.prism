"""A drive is visible in PRISM from its first minute (task ea3f4a62).

On 2026-08-14 the 01ec3894 drive ran pre-flight (~60s) and then Locate spent
~4.7 minutes reading source/brain context BEFORE claiming the task, so the
task page showed PENDING and the board showed IDLE while real work was
happening. Owner rule: the instant a task is worked, PRISM shows it.

The workflow scripts have no JS test runner (repo convention: UI/JS ACs are
pinned by asserting the ACTUAL source), so the invariant is pinned here by
asserting the ordering inside .claude/workflows/implement.js:

1. a Claim phase (in_progress + session link + first heartbeat) runs BEFORE
   the Pre-flight agent, i.e. before anything else the drive does;
2. the Locate prompt orders the claim/heartbeat ahead of the brain-first
   context sweep (the server-pull path, where the task id is only known
   after task_next, has no Claim phase to lean on);
3. a pre-flight halt un-claims and stamps its reason on the drive heartbeat
   instead of leaving a driverless in_progress row.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


def _source() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def test_locate_claims_before_context():
    src = _source()

    # 1. Claim phase exists and precedes Pre-flight in the drive sequence.
    claim_at = src.index("phase('Claim')")
    preflight_at = src.index("phase('Pre-flight')")
    assert claim_at < preflight_at, (
        "the Claim phase must run before Pre-flight - visibility precedes "
        "everything, including fail-fast"
    )

    # The claim agent actually flips the row and links the session.
    claim_block = src[claim_at:preflight_at]
    assert 'status="in_progress"' in claim_block
    assert "task_link_session" in claim_block
    assert "/api/drive-heartbeat/beat" in claim_block

    # 2. Locate orders the claim ahead of the context sweep. The claim
    #    instructions are assembled in claimFirstInstr and injected at the
    #    TOP of the Locate task list, before the brain-first context bullet.
    locate_claim_at = src.index("CLAIM VISIBILITY FIRST")
    locate_context_at = src.index("Build a brain-first context_summary")
    assert locate_claim_at < locate_context_at, (
        "Locate must claim the task before its brain/grep context sweep - "
        "the server-pull path has no Claim phase and this ordering is its "
        "only claim"
    )

    # Locate heartbeats through the sweep, so minutes of reading never render
    # as "nothing is being done" on the owner's board.
    assert '"step":"locate"' in src


def test_preflight_halt_leaves_a_visible_trace():
    src = _source()
    # A pre-flight halt must un-claim (no driverless in_progress row) and
    # stamp the halt reason onto the drive heartbeat.
    assert "pre-flight-halt" in src
    halt_at = src.index("pre-flight-halt")
    assert 'status="pending"' in src[halt_at - 2000 : halt_at], (
        "the halt path must return the row to pending - a dead drive must "
        "never leave the task looking actively worked"
    )


def test_claim_never_clobbers_a_task_that_is_already_done():
    """Task 39244a32 (2026-08-26): a relaunched drive's Claim step blindly
    flipped status="in_progress" over a task the machine gate adjudicator
    had already, legitimately, finished (status=done) moments earlier -- no
    check, no guard. The clobber then propagated: the pre-flight halt that
    followed "un-claimed" the row straight to pending, permanently losing
    the correct done state until a human caught the discrepancy. Both
    write paths must read status FIRST and refuse to write over "done"."""
    src = _source()
    claim_at = src.index("phase('Claim')")
    preflight_at = src.index("phase('Pre-flight')")
    claim_block = src[claim_at:preflight_at]

    # The guard read happens before either mutating call in the SAME prompt.
    guard_at = claim_block.index('status=="done"')
    write_at = claim_block.index('status="in_progress"')
    link_at = claim_block.index("task_link_session(task_id=")
    assert guard_at < write_at < preflight_at - claim_at, (
        "the already-done guard read must precede the in_progress write"
    )
    assert guard_at < link_at

    # The schema and the JS control flow both carry the short-circuit -- an
    # agent that reports already_done must never reach the write path, and
    # the script itself must return immediately rather than falling through
    # to Pre-flight/Locate/Drive for a task with nothing left to do.
    assert "already_done" in src
    already_done_at = src.index("claim.already_done")
    assert already_done_at < preflight_at, (
        "the already_done short-circuit must be checked before Pre-flight runs"
    )
    return_block = src[already_done_at:preflight_at]
    assert "return {" in return_block
    assert "done: true" in return_block


def test_preflight_halt_also_guards_against_clobbering_a_task_that_finished_mid_flight():
    """Same race, the other write path: between Claim setting in_progress
    and Pre-flight halting, the task can legitimately finish (status ->
    done) out from under the drive. The halt's own "un-claim to pending"
    step must not blindly overwrite that -- it needs the same guard read
    Claim itself now has."""
    src = _source()
    halt_at = src.index("pre-flight-halt")
    halt_prompt = src[halt_at - 2500 : halt_at]
    guard_at = halt_prompt.index('status=="done"')
    write_at = halt_prompt.index('status="pending"')
    assert guard_at < write_at, (
        "the halt handler must read status and refuse to overwrite an "
        "already-done task before it un-claims to pending"
    )


def test_graph_phase_heartbeats():
    src = _source()
    assert '"step":"graph"' in src, (
        "the Graph phase runs minutes of call-graph work between Locate and "
        "Drive - without beats the board reads it as idle"
    )


def test_draft_story_instructs_the_nested_oracle_shape_and_a_self_check():
    """Task 3a3f90da (2026-08-26): a drive's own draft_story step wrote a
    story where every one of 9 ACs had a correctly-placed nested oracle
    line, and it STILL stalled story_gate - the rubric parser had a real
    bug (fixed the same day, arc_governance.py's _ac_lines). Fixing the
    reader alone is a one-sided fix: the WRITER'S instructions must also
    state the exact expected shape unambiguously and tell the agent to
    verify its own indentation before submitting, so this class of defect
    is caught before it ever reaches the rubric."""
    src = _source()
    draft_at = src.index("draft_story: [")
    verify_plan_at = src.index("verify_plan: [")
    draft_block = src[draft_at:verify_plan_at]
    assert "MORE INDENTED THAN ITS AC" in draft_block, (
        "draft_story must state the oracle line must be MORE indented than "
        "its AC, not merely 'a line' - that ambiguity is what let the real "
        "story pass a human read while failing the rubric"
    )
    assert "SELF-CHECK BEFORE YOU REPORT" in draft_block, (
        "draft_story must instruct the agent to verify its own indentation "
        "before sending proof=, not just describe the correct shape"
    )
    assert "3a3f90da" in draft_block, (
        "the real incident should be named so a future editor understands "
        "why this instruction is this specific and this strict"
    )


def test_review_previous_notes_instructs_the_same_nested_citation_shape():
    """Same bug class, second rubric (premise_grounded / _claim_lines,
    fixed the same day as _ac_lines): a citation written as a nested child
    bullet under its claim must be named explicitly, or review_previous_notes
    can reproduce the exact story that stalled 3a3f90da."""
    src = _source()
    review_at = src.index("review_previous_notes: [")
    draft_at = src.index("draft_story: [")
    review_block = src[review_at:draft_at]
    assert "premise_grounded" in review_block
    assert "nested child bullet" in review_block or "nested" in review_block.lower(), (
        "review_previous_notes must warn about a citation written as a "
        "nested sub-bullet, the same shape that broke draft_story's oracle "
        "lines"
    )
