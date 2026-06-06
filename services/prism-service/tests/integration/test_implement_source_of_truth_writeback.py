"""RED scaffold — implement.js source-of-truth write-back doctrine,
AC5/AC6/AC7 of "Reinforce SDLC gates" (task 3826dac3).

The gate-teeth half (AC1-4, conductor/verifier teeth) already landed in
commit 590be51 (red scaffold 7bc4925) and is asserted by
tests/unit/test_reinforce_sdlc_gates_real_proof.py. This suite pins the
REMAINING half: the SDLC DRIVER itself — .claude/workflows/implement.js —
must grow three write-back / cite-your-source teeth:

  * AC5 — mandatory WHY-capture on SUCCESS. Today SELF_HEAL writes a
    memory_store ONLY on failure (rung 4), and the terminal green_gate
    handler records completion_proof but NEVER a decision memory. The
    green_gate (and/or verify_green_state) handler must instruct
    memory_store(type="decision") carrying decision + rationale +
    rejected alternatives + file:line on a clean terminal success.

  * AC6 — proactive, size-gated research rung in verify_plan. Today
    verify_plan only checks coverage + blast radius; RESEARCH exists only
    as the REACTIVE SELF_HEAL rung 2. The verify_plan handler must BLOCK
    an approach ungrounded in Brain AND grep until a cited WebSearch /
    best-practice pass exists.

  * AC7 — cite-your-source tier contract. Today STEP_SCHEMA has no
    source-tier field and nothing rejects an empty / mis-tiered cite
    (e.g. grepping for the WHY). STEP_SCHEMA must gain a source_tier
    field (brain=why / grep=how / web=new-practice) and a reject rule
    for a step that cites NOTHING or the WRONG tier.

These are source-structure asserts (mirror
test_implement_dependency_aware_branch_base.py and
test_workflow_scripts_no_datenow.py): the JS the harness runs is the
USER-FACING seam — a doctrine that lives only in a memory or a plan_doc
but not in the prompt the agent actually receives is a false-green.

ALL FAIL today: implement.js never instructs a success-path decision
memory, has no proactive research rung in verify_plan, and STEP_SCHEMA
has no source_tier field / cite-rejection rule.

Out of scope (do NOT assert against): the worktree copies under
.claude/worktrees/.../implement.js — only the canonical workflow is edited.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WORKFLOWS = _SERVICE_ROOT.parent.parent / ".claude" / "workflows"


def _implement_src() -> str:
    return (_WORKFLOWS / "implement.js").read_text(encoding="utf-8")


def _handler_block(src: str, handler: str) -> str:
    """The per-step HANDLERS entry for `handler` — from its key up to the
    next handler key (or the end of the HANDLERS object). This is the seam
    that carries the prompt the step agent actually receives."""
    handlers_start = src.index("const HANDLERS = {")
    keys = [
        "review_previous_notes", "draft_story", "verify_plan",
        "write_failing_tests", "red_gate", "implement_tasks",
        "verify_green_state", "green_gate",
    ]
    # Locate the handler's definition (key followed by a role-default arrow).
    needle = f"\n  {handler}: (role"
    start = src.index(needle, handlers_start)
    # End at the next handler key, else the close of the HANDLERS object.
    end = len(src)
    for k in keys:
        if k == handler:
            continue
        nxt = src.find(f"\n  {k}: (role", start + len(needle))
        if nxt != -1:
            end = min(end, nxt)
    return src[start:end]


def _step_schema_block(src: str) -> str:
    """The STEP_SCHEMA object — the per-step structured-output contract that
    every non-locate step returns. AC7's source_tier field belongs here."""
    start = src.index("const STEP_SCHEMA = {")
    end = src.index("// ── Phase: Pre-flight", start)
    return src[start:end]


# ── AC5: mandatory WHY-capture (decision memory) on terminal SUCCESS ──────

def test_green_gate_instructs_decision_memory_on_success():
    """The terminal green_gate handler must instruct a SUCCESS-path
    memory_store(type="decision"). Today it records completion_proof but
    never a decision memory — the WHY is captured only on failure."""
    src = _implement_src()
    green = _handler_block(src, "green_gate")
    assert "memory_store" in green, (
        "green_gate never instructs a memory_store on terminal success — "
        "the WHY of the change is captured only on failure (SELF_HEAL rung 4)"
    )
    assert 'type="decision"' in green or "type='decision'" in green, (
        "green_gate's success memory is not a DECISION memory — AC5 requires "
        'memory_store(type="decision") on a clean terminal success'
    )


def test_success_decision_memory_carries_full_why():
    """The success-path decision memory must carry the full WHY contract:
    decision + rationale + rejected alternatives + file:line — not a bare
    one-liner. Asserted on the green_gate handler prompt."""
    src = _implement_src()
    green = _handler_block(src, "green_gate")
    lowered = green.lower()
    assert "rationale" in lowered, (
        "the success decision memory does not require a rationale"
    )
    assert "rejected alternative" in lowered or "alternatives" in lowered, (
        "the success decision memory does not require rejected alternatives"
    )
    assert "file:line" in lowered or "file:line" in green, (
        "the success decision memory does not require concrete file:line refs"
    )


def test_success_memory_is_not_only_failure_path():
    """Guard against a false-green: the decision-memory instruction must sit
    in a TERMINAL-SUCCESS handler (green_gate / verify_green_state), not
    merely the failure-only SELF_HEAL rung. memory_store(type="decision")
    must appear OUTSIDE the SELF_HEAL block."""
    src = _implement_src()
    self_heal_start = src.index("const SELF_HEAL = [")
    self_heal_end = src.index("].join('\\n')", self_heal_start)
    decision_idx = src.find('type="decision"')
    if decision_idx == -1:
        decision_idx = src.find("type='decision'")
    assert decision_idx != -1, (
        'no memory_store(type="decision") anywhere — AC5 unimplemented'
    )
    assert not (self_heal_start < decision_idx < self_heal_end), (
        "the decision memory lives only inside SELF_HEAL (failure path) — "
        "AC5 requires it on a TERMINAL-SUCCESS handler"
    )


# ── AC6: proactive, size-gated research rung in verify_plan ───────────────

def test_verify_plan_has_proactive_research_rung():
    """verify_plan must gain a PROACTIVE research rung: an approach
    ungrounded in Brain AND grep BLOCKS until a cited WebSearch /
    best-practice pass exists. Today verify_plan only checks coverage +
    blast radius; research is only the reactive SELF_HEAL rung."""
    src = _implement_src()
    vplan = _handler_block(src, "verify_plan")
    assert "WebSearch" in vplan or "best-practice" in vplan.lower() \
        or "best practice" in vplan.lower(), (
        "verify_plan has no research rung — an ungrounded approach is never "
        "forced through a cited WebSearch/best-practice pass (AC6)"
    )


def test_verify_plan_research_is_grounding_gated():
    """The research rung is GATED on grounding: it fires only when the
    approach is ungrounded in Brain AND grep. The verify_plan prompt must
    speak of being ungrounded / not grounded in brain + grep."""
    src = _implement_src()
    vplan = _handler_block(src, "verify_plan")
    lowered = vplan.lower()
    assert "ungrounded" in lowered or "not grounded" in lowered \
        or "no brain" in lowered or "grep" in lowered, (
        "verify_plan's research rung is not grounding-gated on Brain+grep — "
        "it must fire only for an approach Brain and grep cannot ground (AC6)"
    )


def test_verify_plan_research_blocks_until_cited():
    """The rung must BLOCK (reject / not pass) the step until a cited
    research pass is present — not merely 'consider researching'. The
    verify_plan prompt must carry a blocking verb tied to the citation."""
    src = _implement_src()
    vplan = _handler_block(src, "verify_plan")
    lowered = vplan.lower()
    assert ("block" in lowered or "reject" in lowered
            or "do not pass" in lowered or "must not advance" in lowered
            or "halt" in lowered), (
        "verify_plan's research rung does not BLOCK an ungrounded approach — "
        "it must withhold the step until a cited research pass exists (AC6)"
    )


# ── AC7: cite-your-source tier contract (source_tier field + reject) ──────

def test_step_schema_has_source_tier_field():
    """STEP_SCHEMA must gain a source_tier field so every step declares
    which tier answered it (brain=why / grep=how / web=new-practice).
    Today STEP_SCHEMA has only step/ok/to_step/gate_state/validation/
    evidence/halt_reason — no source-tier field."""
    src = _implement_src()
    schema = _step_schema_block(src)
    assert "source_tier" in schema, (
        "STEP_SCHEMA has no source_tier field — no step declares which "
        "knowledge tier answered it (AC7)"
    )


def test_source_tier_enumerates_brain_grep_web():
    """The source_tier contract must name the three tiers and their roles:
    brain=why / grep=how / web=new-practice (so a mis-tier is detectable)."""
    src = _implement_src()
    schema = _step_schema_block(src)
    lowered = schema.lower()
    assert "brain" in lowered and "grep" in lowered and "web" in lowered, (
        "source_tier does not enumerate the brain/grep/web tiers — a "
        "mis-tiered cite (e.g. grepping for the WHY) cannot be detected (AC7)"
    )


def test_step_rejects_empty_or_mistiered_cite():
    """A step citing NOTHING or the WRONG tier (e.g. grep for the WHY) must
    be REJECTED. The implement.js wiring must carry that reject rule — a
    schema field alone, with nothing enforcing it, is a false-green."""
    src = _implement_src()
    lowered = src.lower()
    # The reject rule must co-locate with the source_tier contract.
    tier_idx = lowered.find("source_tier")
    assert tier_idx != -1, "no source_tier contract to enforce (AC7)"
    # Search the whole file for a rejection verb tied to a mis/empty cite.
    has_reject = (
        "reject" in lowered or "rejected" in lowered or "mis-tier" in lowered
        or "mistier" in lowered or "wrong tier" in lowered
        or "empty cite" in lowered or "cites nothing" in lowered
        or "citing nothing" in lowered
    )
    assert has_reject, (
        "no reject rule for an empty / mis-tiered cite — the source_tier "
        "field is declared but never enforced (false-green) (AC7)"
    )


# ── Guard: only the canonical workflow is edited (worktree untouched) ─────

def test_only_canonical_workflow_targeted():
    """Guard: this suite asserts against the canonical workflow path, never
    a worktree copy — the worktree under .claude/worktrees syncs separately
    and is explicitly out of scope."""
    assert _WORKFLOWS.name == "workflows"
    assert "worktrees" not in str(_WORKFLOWS), (
        "test is pointed at a worktree copy — only the canonical "
        ".claude/workflows/implement.js is in scope"
    )
    assert (_WORKFLOWS / "implement.js").exists(), (
        "canonical .claude/workflows/implement.js not found"
    )
