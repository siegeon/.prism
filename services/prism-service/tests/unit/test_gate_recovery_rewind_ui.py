"""A user must be able to intuitively recover from a wrongly-decided gate
(owner 2026-08-25, live: "we need the user to be able to intuitively
recover from this state"). Before this fix, the ONLY lever was
POST /api/conductor/rewind -- callable via raw curl only, with no
affordance anywhere in the app. The gate-decision card itself only renders
while `gate_state` is "pending"/"failed" (`gatePanelOwnsOracle`), so once a
gate is wrongly APPROVED (`gate_state="passed"`) that card disappears
entirely -- there was no in-app path back.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned
by asserting the ACTUAL TSX source (tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6). Comments are stripped before every assertion
(repo convention, CLAUDE.md Lessons e139295d) so a comment can never
satisfy a guard the real code is supposed to own.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
       / "TaskDetailPage.tsx")

_RECOVERY_MARKER = 'summary="recovery — undo a gate decided in error"'
_RECOVERY_END_MARKER = 'docTab === "evidence" && gatePanelOwnsOracle && (<>'


def _read() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _TSX.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _recovery_section() -> str:
    """The Disclosure carrying the recovery summary, sliced back to its
    OWN gating condition (searched backwards from the marker) forward to
    the next section's distinct start marker."""
    src = _strip_comments(_read())
    marker_at = src.index(_RECOVERY_MARKER)
    # Walk back to the nearest `docTab === "evidence" &&` that GATES this
    # block -- the gating condition sits a few lines above the Disclosure's
    # own summary prop.
    gate_at = src.rindex('docTab === "evidence" &&', 0, marker_at)
    end = src.index(_RECOVERY_END_MARKER, marker_at)
    return src[gate_at:end]


def test_recovery_control_is_never_gated_on_gate_state():
    """The whole point is undoing a decision that has ALREADY been made --
    gate_state is typically 'passed' by then, so this control must NOT
    share gatePanelOwnsOracle's pending/failed gate."""
    section = _recovery_section()
    assert "conductorOn" in section, (
        "the recovery control must be gated on conductorOn (is this a "
        f"conductor-driven task at all), not on gate_state: {section[:200]}"
    )
    assert "gatePanelOwnsOracle" not in section, (
        "the recovery control must render independently of "
        "gatePanelOwnsOracle (pending/failed only) -- otherwise it "
        "disappears exactly when it's needed, right after a wrong approve"
    )


def test_recovery_section_has_a_stable_id_for_reliable_targeting():
    """Discovered live (2026-08-25): a plain CSS-only bridge/automation
    tool can't reliably target a bare `<Disclosure>` summary button by
    text -- no :has-text, no text= engine, and a positional selector like
    `div > button` matches ambiguously elsewhere on the page. A stable id
    (matching the existing #delivery-card pattern) is what makes this
    control genuinely, reliably clickable -- by a human, a script, or an
    agent -- which is the whole point of an "intuitive recovery" control."""
    section = _recovery_section()
    assert 'id="gate-recovery"' in section, (
        f"the recovery block's wrapper div must carry a stable id: {section[:300]}"
    )


def _do_rewind_function() -> str:
    """doRewind is defined as a function upstream of the JSX block that
    invokes it (onClick={doRewind}) -- sliced independently since the fetch
    call itself doesn't live inside the JSX."""
    src = _strip_comments(_read())
    start = src.index("const doRewind = async ()")
    end = src.index("\n  };", start)
    return src[start:end]


def test_recovery_button_invokes_do_rewind():
    section = _recovery_section()
    assert "onClick={doRewind}" in section, (
        f"the Rewind button must call doRewind: {section}"
    )


def test_rewind_button_posts_to_the_real_rewind_endpoint():
    fn = _do_rewind_function()
    assert "/api/conductor/rewind" in fn, (
        "doRewind must call the real audited rewind endpoint, not a "
        f"placeholder: {fn}"
    )
    assert '"task_id"' in fn or "task_id:" in fn, fn
    assert '"reason"' in fn or "reason:" in fn, fn


def test_rewind_requires_a_nonblank_reason_before_it_can_be_clicked():
    """Mirrors the backend's own guard (rewind_task refuses a blank
    reason) -- the button must be disabled without one, not merely
    refused server-side after a wasted round trip."""
    section = _recovery_section()
    assert "rewindReason.trim()" in section, (
        f"the Rewind button must be disabled while rewindReason is blank: {section[:400]}"
    )
    assert re.search(r"disabled=\{[^}]*!rewindReason\.trim\(\)", section), (
        "the disabled= expression must include !rewindReason.trim() so an "
        f"empty reason can't be submitted: {section}"
    )
