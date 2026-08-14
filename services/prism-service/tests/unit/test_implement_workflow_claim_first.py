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


def test_graph_phase_heartbeats():
    src = _source()
    assert '"step":"graph"' in src, (
        "the Graph phase runs minutes of call-graph work between Locate and "
        "Drive - without beats the board reads it as idle"
    )
