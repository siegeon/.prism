"""RED scaffold - cancelled/archived/deleted tasks stop presenting a live,
actionable gate card (task e948008a).

Pins the DEFECT: TaskDetailPage.tsx's gate-card render branch (~1530), the
gatePanelOwnsOracle flag (~1357), and the pinned-test auto-run guard (~1048)
all key off task.gate_state / task.workflow_step alone, with no check on
task.status. A cancelled/archived/deleted task parked at a pending or
failed gate therefore renders the full live decision panel - Approve,
Override, re-run - and merely opening its page fires a real pytest run,
even though the decision is moot. Repro fixture: task
696cacf5-d8ce-46b8-866c-4016d19c621c (status=cancelled, gate_state=failed).

Source-reading tests (this repo has no JS test runner - see
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6): comments are
stripped before any assertion, and every branch is located by brace/paren
balancing from an unambiguous marker, never a fixed character window - a
comment or an inserted line must not be able to satisfy these (repo lesson
e139295d / CLAUDE.md Lessons).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
# .../services/prism-service/tests/unit/<file> -> service root is 3 up.
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"

_DEAD_STATUSES = ("cancelled", "archived", "deleted")

# The dead-task branch-condition marker pins the EXACT boolean guard this
# slice is expected to author.
#
# The former sibling _LIVE_MARKER (the notification banner's own guard,
# `{conductorOn && (task.gate_state === "pending" || task.gate_state ===
# "failed")`) is RETIRED (task d9f082fe follow-up, owner live, 2026-08-24)
# along with the banner itself - see the FR-1/FR-2 retirement notes below.
_DEAD_MARKER = (
    '{conductorOn && (task.gate_state === "pending" || task.gate_state === "failed") '
    '&& (task.status === "cancelled" || task.status === "archived" || task.status === "deleted")'
)


def _read() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _TSX.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy
    a source assertion (the repeated failure mode in the lessons).

    The trailing `(?<!\\\\)` guards against a real false positive found in
    this file: regex literals like `/^https?:\\/\\//` embed an escaped
    "//" that is not a comment start - without the guard the scan swallows
    the rest of the line (and every brace on it), which desyncs every
    balanced-brace scan downstream of that line."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _balanced(src: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the close char that matches the open char at open_idx."""
    depth = 0
    for k in range(open_idx, len(src)):
        if src[k] == open_ch:
            depth += 1
        elif src[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    raise AssertionError(f"unbalanced {open_ch}{close_ch} scanning from {open_idx}")


def _const_stmt(src: str, name: str) -> str:
    """A `const <name> = ...;` statement, found by scanning to the
    terminating `;` at paren/brace/bracket depth 0 - never a fixed
    character window."""
    marker = f"const {name} ="
    start = src.index(marker)
    depth = 0
    for k in range(start, len(src)):
        c = src[k]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == ";" and depth == 0:
            return src[start:k + 1]
    raise AssertionError(f"no terminating ; found for {marker}")


def _dead_task_block(src: str) -> tuple[int, str]:
    """The inert dead-task gate card - start index and text - found the
    same way, by brace balance from its own unique (longer) opening
    marker."""
    start = src.index(_DEAD_MARKER)
    close = _balanced(src, start, "{", "}")
    return start, src[start:close + 1]


# ---------------------------------------------------------------------
# FR-6 - gatePanelOwnsOracle carries the identical status exclusion as the
# AC-1 render branch, so the oracle hands back to PlanView on a dead task.
# ---------------------------------------------------------------------


def test_gate_panel_owns_oracle_excludes_dead_statuses():
    src = _strip_comments(_read())
    stmt = _const_stmt(src, "gatePanelOwnsOracle")
    for status in _DEAD_STATUSES:
        assert f'task.status !== "{status}"' in stmt, (
            f"gatePanelOwnsOracle must exclude status={status!r} (FR-6) so "
            "the oracle hands back to PlanView (TaskDetailPage.tsx:1887) "
            "instead of rendering twice on a dead task's page"
        )


# ---------------------------------------------------------------------
# FR-5 - the pinned-tests effect guard excludes dead statuses, so opening a
# cancelled/archived/deleted task's page never fires a live pytest run.
# ---------------------------------------------------------------------


def test_at_gate_effect_guard_excludes_dead_statuses():
    src = _strip_comments(_read())
    stmt = _const_stmt(src, "atGate")
    assert 'task.status !== "done"' in stmt, (
        "sanity: the existing done exclusion must survive this change"
    )
    for status in _DEAD_STATUSES:
        assert f'task.status !== "{status}"' in stmt, (
            f"atGate must exclude status={status!r} (FR-5) so opening a "
            "dead task's page never fires a live "
            "/api/tasks/{id}/tests?run=true pytest run"
        )


# ---------------------------------------------------------------------
# FR-1 - the gate-card render branch excludes cancelled/archived/deleted
# inline (no hoisted const), so the live decision panel never renders for
# a dead task.
#
# RETIRED (task d9f082fe follow-up, owner live, 2026-08-24): "it looks
# like you left the evidence stuff on the overview tab". The Overview
# notification banner this test's `_LIVE_MARKER` pinned is deleted
# outright - the live decision panel is gated ONLY by the `docTab ===
# "evidence" && gatePanelOwnsOracle` tab condition now, no separate
# inline JSX branch exists to source-read. `_LIVE_MARKER`'s own comment
# ("the dead marker is the live marker's prefix ... src.index() on the
# live marker always finds the FIRST (live) occurrence") is no longer
# true - with the live banner gone, the live marker's first (and only)
# match IS the dead marker. The underlying FR-1 invariant (dead statuses
# excluded from the live decision panel) is still enforced and still
# pinned - by test_gate_panel_owns_oracle_excludes_dead_statuses (FR-6)
# above, which checks `gatePanelOwnsOracle`'s own definition directly
# rather than an inline render-branch condition that no longer exists.
# ---------------------------------------------------------------------


# ---------------------------------------------------------------------
# NFR-1 - the live-task gate path stays byte-identical except for its
# guard: a pending/failed gate on a non-dead task still renders Approve,
# Override and re-run exactly as before. Pin it explicitly, never assume.
# ---------------------------------------------------------------------


def test_live_gate_card_still_has_approve_override_rerun_unchanged():
    # RELOCATED (task d9f082fe follow-up, owner live, 2026-08-24): "this
    # evidence stuff should not be a expanding panel, but rather a tab on
    # the work screen". The Approve/Override/re-run controls moved out of
    # the banner's expand-in-place `{gatePanelOpen && (...)}` body into a
    # dedicated `{docTab === "evidence" && gatePanelOwnsOracle && (...)}`
    # tab body -- the notification banner itself (and its own guard) is
    # deleted outright, see the FR-1/FR-2 retirement notes above.
    src = _strip_comments(_read())
    # String-anchored, not paren-balanced (this file's own naive char-level
    # scan turned out to be fragile the moment a refactor's prose/JSX text
    # carries an unpaired '(' character -- see
    # test_stale_gate_banner_inspect_vs_override.py's _gate_panel_block for
    # the full account). The known sibling boundary is unambiguous either way.
    start = src.index('{docTab === "evidence" && gatePanelOwnsOracle && (')
    end = src.index('{docTab === "overview" && (<>', start)
    block = src[start:end]
    assert 'gateDecide("approve")' in block, (
        "NFR-1: the live gate card must still wire the Approve action"
    )
    assert "checked={gateOverride}" in block, (
        "NFR-1: the live gate card must still render the Override checkbox"
    )
    assert "mintEvidence" in block, (
        "NFR-1: the live gate card must still offer the re-run/mintEvidence "
        "affordance"
    )
    assert '"Approve (recover)"' in block, (
        "NFR-1: the failed-gate recovery label must still render"
    )


# ---------------------------------------------------------------------
# FR-2 - a dead task parked at a pending/failed gate renders an INERT
# banner, never hidden or unmounted (the ticket's own likely_misfire).
#
# RETIRED (task d9f082fe follow-up, owner live, 2026-08-24): "it looks
# like you left the evidence stuff on the overview tab". The "live vs
# dead, never interleaved" ordering concern this test guarded doesn't
# apply the same way anymore - the live decision panel and the dead-task
# inert card are no longer two JSX branches competing for the SAME
# surface (Overview) at all. The live panel now lives entirely on its own
# Evidence tab (`docTab === "evidence"`); the dead-task card still lives
# on Overview (`docTab === "overview"`), gated by `gatePanelOwnsOracle`'s
# own dead-status exclusion (FR-6) on one side and the dead-status
# INclusion on the other. They render on mutually exclusive tabs, which
# is a STRONGER guarantee than "never interleaved" ever was - they
# literally cannot both be mounted into the same view at once, so neither
# can hide or shadow the other's DOM.
# ---------------------------------------------------------------------


def test_dead_task_banner_names_the_status_in_words():
    src = _strip_comments(_read())
    _, block = _dead_task_block(src)
    assert "decision moot" in block, (
        'FR-2: the inert banner must name the dead state in words a person '
        'reads, e.g. "CANCELLED, decision moot"'
    )
    assert "task.status.toUpperCase()" in block, (
        "the banner must render the ACTUAL status value (not a hardcoded "
        "single-state string) so archived/deleted read correctly too"
    )


# ---------------------------------------------------------------------
# FR-3 - the Approve button, the Override checkbox and the re-run/
# mintEvidence affordance are all ABSENT from the dead-task branch's JSX.
# ---------------------------------------------------------------------


def test_dead_task_banner_has_no_approve_override_or_rerun_affordance():
    src = _strip_comments(_read())
    _, block = _dead_task_block(src)
    assert "gateDecide(" not in block, (
        "FR-3: no Approve/Reject action may wire in the dead-task banner - "
        "the decision is moot"
    )
    assert "checked={gateOverride}" not in block, (
        "FR-3: no Override checkbox may render in the dead-task banner"
    )
    assert "mintEvidence" not in block, (
        "FR-3: no re-run/mintEvidence affordance may render in the "
        "dead-task banner"
    )


# ---------------------------------------------------------------------
# FR-4 - evidence rows (ProofShots, pinned-test rows) and gate history
# still render read-only, via the branch's OWN smaller projection (never
# a shared helper hoisted above the return - the oracle-ordering
# invariant test_task_oracle_always_visible_ui.py pins on that).
# ---------------------------------------------------------------------


def test_dead_task_banner_renders_evidence_and_history_readonly():
    src = _strip_comments(_read())
    _, block = _dead_task_block(src)
    assert "<ProofShots" in block, (
        "FR-4: visual evidence (ProofShots) must still render read-only on "
        "a dead task's parked gate"
    )
    assert "pinTests" in block, (
        "FR-4: pinned-test evidence rows must still render read-only"
    )
    assert "gateEvidenceLines(history)" in block, (
        "FR-4: gate history must still render read-only via the branch's "
        "own smaller projection"
    )
    assert "task.gate_reason" in block, (
        "FR-4: the stored gate reason must still render read-only"
    )
