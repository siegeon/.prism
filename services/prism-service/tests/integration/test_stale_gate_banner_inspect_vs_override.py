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
    src = re.sub(r"(?m)(?<!:)//.*$", "", src)
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


def _banner_onclick_body(src: str) -> str:
    """The banner button's onClick={...} body, found by scanning BACK from
    the unique `setGatePanelOpen((v) => ...)` toggle call to its enclosing
    `onClick={`, then forward by brace balance - never a fixed character
    window (a comment or inserted line must not be able to hide the guard)."""
    marker = "setGatePanelOpen((v) =>"
    call_idx = src.index(marker)
    onclick_at = src.rfind("onClick={", 0, call_idx)
    assert onclick_at != -1, "no enclosing onClick={ found before the panel toggle"
    brace_open = onclick_at + len("onClick=")  # index of the '{' itself
    assert src[brace_open] == "{"
    close = _balanced(src, brace_open, "{", "}")
    return src[brace_open:close + 1]


def _gate_panel_block(src: str) -> str:
    """The `{gatePanelOpen && ( ... )}` expanded-panel body, found by paren
    balance from the guard - not a fixed window."""
    marker = "{gatePanelOpen && ("
    start = src.index(marker)
    paren_open = start + len(marker) - 1  # index of the '(' itself
    assert src[paren_open] == "("
    close = _balanced(src, paren_open, "(", ")")
    return src[start:close + 1]


# ---------------------------------------------------------------------
# AC - the banner's bare expand-click is a pure toggle: no side effect on
# gateReason or gateOverride (that is what a person clicks to merely LOOK).
# ---------------------------------------------------------------------


def test_banner_onclick_only_toggles_panel_no_reason_or_override_side_effect():
    src = _strip_comments(_read())
    body = _banner_onclick_body(src)
    assert "setGatePanelOpen(" in body, "sanity: this must be the panel toggle"
    assert "setGateOverride(true)" not in body, (
        "the banner's expand-click must not auto-arm the override - that is "
        "recover-with-override, a distinct action from inspecting the gate"
    )
    assert "setGateReason(" not in body, (
        "the banner's expand-click must not pre-fill the gate reason as a "
        "side effect of merely expanding/inspecting the panel - the pre-fill "
        "belongs inside the expanded panel body, not the click handler"
    )


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
