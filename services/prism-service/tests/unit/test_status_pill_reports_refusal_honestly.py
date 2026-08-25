"""setStatus (the "-> done / -> blocked / -> pending" quick-status pills)
must surface a refused PATCH honestly, not as a false-success toast.

`fetch()` only rejects on a network error, never on a non-2xx HTTP status
-- unchecked, a refused status PATCH (e.g. the open-gate close guard,
api/tasks.py's DONE_BLOCKED_BY_OPEN_GATE_FIX) still fell through to
`setNotice("Moved to done.")`, a false-success toast for a status change
that had NOT actually happened (2026-08-25 live near-miss: the click that
caused it also landed on the wrong control in the first place, but even a
correctly-targeted click on a refused transition would have lied the same
way before this fix).

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned
by asserting the ACTUAL TSX source (tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6). Comments are stripped before every assertion
(repo convention, CLAUDE.md Lessons e139295d).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_TSX = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
       / "pages" / "TaskDetailPage.tsx")


def _strip_comments(src: str) -> str:
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _set_status_function() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    src = _strip_comments(_TSX.read_text(encoding="utf-8"))
    start = src.index("const setStatus = async")
    end = src.index("\n  };", start)
    return src[start:end]


def test_set_status_checks_response_ok_before_declaring_success():
    fn = _set_status_function()
    assert re.search(r"if\s*\(\s*!r\.ok\s*\)", fn), (
        f"setStatus must check r.ok before treating the PATCH as a "
        f"success: {fn}"
    )


def test_set_status_success_toast_is_gated_behind_the_ok_check():
    """The success path (`Moved to ${status}.` + reload) must be
    unreachable when the response was refused -- pinned by requiring the
    ok-check's early return to appear BEFORE the success toast in the
    function body, not after."""
    fn = _set_status_function()
    ok_check_at = fn.index("if (!r.ok)")
    success_at = fn.index('setNotice(`Moved to ${status}.`)')
    assert ok_check_at < success_at, (
        "the !r.ok refusal branch must be checked BEFORE the success "
        f"toast fires: {fn}"
    )
    # The refusal branch itself must return before reaching the success
    # toast (not merely log and fall through).
    between = fn[ok_check_at:success_at]
    assert "return" in between, (
        f"the refusal branch must return early, not fall through to the "
        f"success toast: {between}"
    )
