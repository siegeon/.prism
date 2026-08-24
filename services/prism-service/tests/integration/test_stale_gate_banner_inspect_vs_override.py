"""RED scaffold - stale gate banner separates inspect from override
(task c7ce0fc3).

Pins the DEFECT: the gate notification banner's onClick handler
(TaskDetailPage.tsx ~1515-1534) does THREE things on a bare expand-click -
toggles gatePanelOpen, pre-fills gateReason, and conditionally auto-arms
gateOverride(true) - so merely INSPECTING the gate silently arms an
override decision. The fix relocates the pre-fill/auto-arm into the
expanded panel body and leaves the banner's onClick a pure toggle; the
panel keeps two SEPARATE controls (re-run the oracle vs override) rather
than folding override-recovery into the banner click itself.

Source-reading tests (this repo has no JS test runner - see
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6): comments are
stripped before any assertion, and enclosing blocks are found by brace/
paren balancing, never a fixed character window - a comment or an
inserted line must not be able to satisfy these (the repeated failure
mode logged in CLAUDE.md's Lessons).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
# .../services/prism-service/tests/integration/<file> -> service root is 3 up.
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"


def _read() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _TSX.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy
    a source assertion (the repeated failure mode in the lessons)."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # (?<!:) protects https:// in a URL; (?<!\\) protects the tail of an
    # escaped-slash regex literal such as /^https?:\/\// on
    # TaskDetailPage.tsx:1574, whose final \ / / otherwise reads as a
    # comment start and takes the rest of that JSX line - closing ')' and
    # '}' included - with it. See the AC-1..AC-4 tests below.
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


def _gate_panel_block(src: str) -> str:
    """The `{docTab === "evidence" && gatePanelOwnsOracle && ( ... )}`
    Evidence-tab body.

    RELOCATED (task d9f082fe follow-up, owner live, 2026-08-24): this
    content used to be the banner's expand-in-place `{gatePanelOpen && (
    ...)}` body, DOUBLE-WRAPPED inside an outer `{conductorOn && (...) &&
    (...)}` notification condition. It now lives in its own tab, gated on
    a SINGLE flat `docTab === "evidence" && gatePanelOwnsOracle` condition
    - the redundant outer wrapper is gone (verified structurally sound by
    a clean `tsc -b`). That removed level was silently load-bearing for
    the OLD paren-balance scan below: the content itself carries one
    genuine net-unmatched '(' (inside prose/JSX text, not a real code
    defect - TSX text and string literals may contain unpaired parens
    freely), which the old double-`)}`  close happened to absorb by
    coincidence. A single-`)}` close leaves that scan one paren short of
    zero, landing anywhere later in the file depending on what other
    prose parens it wanders through next - exactly the fragility this
    file's own AC-1..AC-4 history already documents. Anchoring to the
    known, unambiguous sibling boundary instead is strictly more robust
    than chasing paren balance through prose ever was."""
    start = src.index('{docTab === "evidence" && gatePanelOwnsOracle && (')
    end = src.index('{docTab === "overview" && (<>', start)
    return src[start:end]


# ---------------------------------------------------------------------
# AC - the banner's bare expand-click is a pure toggle: no side effect on
# gateReason or gateOverride (that is what a person clicks to merely LOOK).
#
# RETIRED (task d9f082fe follow-up, owner live, 2026-08-24): "it looks
# like you left the evidence stuff on the overview tab". The Overview
# notification banner (and its onClick) is deleted outright, not merely
# de-toggled - the gate status summary is now a plain, non-interactive
# header at the top of the Evidence tab itself (nothing to click there
# either; you're already on the one place this content lives). There is
# no more banner onClick body to inspect for a reason/override side
# effect, because there is no more banner. The underlying concern (merely
# LOOKING at gate status must never arm a recovery decision) is trivially
# true now: reaching the Evidence tab is a plain tab-strip click wired to
# `setDocTab`, nothing else, and gateReason/gateOverride are set only by
# their own explicit controls inside that tab (still pinned below by
# test_expanded_panel_has_two_separate_recovery_controls).
# ---------------------------------------------------------------------


def test_banner_summary_no_longer_names_click_as_override_recovery():
    src = _strip_comments(_read())
    # The banner's own summary/status copy (line ~1551) must stop describing
    # a bare expand-click as the override-recovery action itself.
    assert "recover with override, or fix & re-run" not in src, (
        "banner summary text still tells the reviewer that clicking the "
        "banner IS override-recovery - inspecting and overriding must read "
        "as two different actions"
    )


# ---------------------------------------------------------------------
# AC - the expanded panel offers re-run-the-oracle and override-recovery as
# two SEPARATE, distinctly wired interactive elements, not one combined
# banner affordance.
# ---------------------------------------------------------------------


def test_expanded_panel_has_two_separate_recovery_controls():
    src = _strip_comments(_read())
    panel = _gate_panel_block(src)

    rerun = re.search(r"<button[^>]*onClick=\{mintEvidence\}", panel)
    assert rerun, (
        "expanded panel must offer a re-run-the-oracle control wired to "
        "mintEvidence, separate from the override control"
    )
    override = re.search(
        r'<input\s+type="checkbox"[^>]*checked=\{gateOverride\}[^>]*'
        r"onChange=\{\(e\) => setGateOverride\(e\.target\.checked\)\}",
        panel,
    )
    assert override, (
        "expanded panel must offer an override-recovery control wired to "
        "gateOverride/setGateOverride, separate from the re-run control"
    )
    assert rerun.start() != override.start(), (
        "re-run and override must be two distinct rendered elements"
    )


# ---------------------------------------------------------------------
# AC-1..AC-4 (task 3287e8b4) - the comment stripper must not mistake an
# ESCAPED slash inside a regex literal for the start of a // comment.
#
# TaskDetailPage.tsx:1572-1574 carries /(https?:\/\/\S+)/ and
# .replace(/^https?:\/\//, ""). In /^https?:\/\// the tail is \ / / - an
# escaped slash followed by the regex's own terminating delimiter, i.e.
# two ADJACENT '/' whose preceding char is '\', not ':'. The original
# (?<!:)//.*$ therefore matched and deleted the whole rest of line 1574,
# which is live JSX carrying real closing ')' and '}' tokens.
#
# The paren balance then never recovered: the scan ran off the end of the
# file and landed on the last stray ')' six characters before EOF. The
# suite passed on that coincidence, so any edit changing the file's total
# length re-broke it (parent task e948008a hit exactly this, as
# "unbalanced () scanning from 52337").
# ---------------------------------------------------------------------

_OLD_LINE_COMMENT_RE = r"(?m)(?<!:)//.*$"
_NEW_LINE_COMMENT_RE = r"(?m)(?<!:)(?<!\\)//.*$"


def test_escaped_slash_regex_literal_is_not_read_as_a_line_comment():
    """AC-1. The assertion text sits on the SAME line as the regex,
    because (?m)//.*$ only eats to the newline - a marker on the next
    line would survive even the broken stripper and prove nothing."""
    frag = 'const m = /^https?:\\/\\//; KEEP_ME_ON_THIS_LINE'
    assert "KEEP_ME_ON_THIS_LINE" in _strip_comments(frag), (
        "an escaped slash inside a regex literal was mistaken for the "
        "start of a // line comment, so the rest of the line was deleted"
    )
    # ...and on the REAL file: line 1574's tail carries genuine closing
    # ')' and '}' tokens that the unguarded strip removed.
    real = _strip_comments(_read())
    assert "{lead.slice(m.index + m[1].length)}" in real, (
        "TaskDetailPage.tsx:1574 was truncated by the comment stripper - "
        "its closing JSX tokens are gone, which is what breaks the "
        "paren-balance scan below"
    )


def test_gate_panel_block_closes_structurally_not_by_running_off_the_file():
    """AC-2. A real structural close, not an accidental EOF landing."""
    src = _strip_comments(_read())
    block = _gate_panel_block(src)
    end = src.index(block) + len(block)
    assert len(src) - end > 1000, (
        f"the gate-panel block closed {len(src) - end} chars from EOF - "
        "that is the scan running off the end of the file and landing on "
        "a stray ')', not the panel's true structural end"
    )
    tail = src[end:end + 400]
    # RE-ANCHORED at the e948008a merge: the dead-task inert gate card
    # (`{conductorOn && ... task.status === "cancelled"`) is now appended
    # directly after the live panel, pushing the `{task.status === "blocked"`
    # sibling beyond this window. The invariant is unchanged - the scan must
    # land on a REAL sibling JSX branch, not run off to EOF - so accept
    # either known sibling as the structural neighbour.
    assert "{task.status ===" in tail or "{conductorOn &&" in tail, (
        "the text after the panel block should be a sibling JSX branch "
        "(the dead-task inert card or the blocked banner); "
        f"got: {tail[:120]!r}"
    )


def test_gate_panel_block_is_stable_when_the_tsx_changes_length():
    """AC-3. This task's own regression-proofing clause. The appended
    block carries a SURVIVING statement as well as a comment: a
    comment-only append is stripped away and would pass even while
    broken. Mutation is IN MEMORY - TaskDetailPage.tsx is outside this
    slice's allowed_files and is never written to."""
    base = _strip_comments(_read())
    block_base = _gate_panel_block(base)

    # HONEST NOTE: this assertion is a STABILITY invariant, not a red
    # test. It passes at the pre-fix baseline too, because a BALANCED
    # append (any real code or comment) does not move the stray ')' the
    # broken scan lands on - measured, not assumed. What it locks in is
    # that the block cannot start tracking file length again later; the
    # CORRECTNESS of the close is pinned by the AC-2 test above, which
    # does fail at baseline.
    throwaway = "\n\n\n// throwaway\n\nconst THROWAWAY_TAIL = fn(1);\n"
    grown = _strip_comments(_read() + throwaway)
    block_grown = _gate_panel_block(grown)

    assert block_grown == block_base, (
        "appending an unrelated block to TaskDetailPage.tsx changed the "
        "extracted gate-panel block - the close index is tracking the "
        "file's total length instead of its structure"
    )


def test_the_escaped_slash_guard_does_not_start_missing_real_comments():
    """AC-4. Discharges this task's recorded likely_misfire with
    evidence: adding (?<!\\) must not cause a GENUINE // comment to stop
    being stripped. Every line whose stripping differs between the two
    patterns must differ because of an escaped slash (a regex literal),
    never because it was a real comment."""
    for n, line in enumerate(_read().split("\n"), 1):
        old = re.sub(_OLD_LINE_COMMENT_RE, "", line)
        new = re.sub(_NEW_LINE_COMMENT_RE, "", line)
        if old == new:
            continue
        assert "\\/" in line, (
            f"line {n} strips differently under the guard but carries no "
            f"escaped slash, so the guard is swallowing a real comment: "
            f"{line.strip()[:120]!r}"
        )
