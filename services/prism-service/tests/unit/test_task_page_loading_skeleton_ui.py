"""Red tests for task c3f4cf12 — "Task pages stop hiding behind a bare
Loading fallback".

The PRISM SPA has NO JS test runner, so UI-FIRST acceptance criteria are
pinned by asserting the ACTUAL web source (TSX) — the same pattern as
tests/unit/test_dashboard_hydration_skeleton_ui.py and
tests/unit/test_conductor_page_animated_cleanup_ui.py.

These FAIL against the current source: App.tsx:107-109 wraps every lazy
route (including /tasks/:id at :132, /tasks at :127, /conductor) in ONE
shared `<Suspense fallback={<div className="p-8 text-sm opacity-50">
Loading…</div>}>` — a bare text node, no Skeleton, no layout shape.

The fallback prop's value is extracted BRACE-BALANCED from its own start
(the `fallback={` open), never a fixed character window — so a comment
placed near the Suspense block cannot satisfy these assertions (lesson:
a source-reading test must match the rendered construct, not prose near
it).
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "prism_service" / "web" / "src"
_APP = _SRC / "App.tsx"

_SUSPENSE_OPEN = "<Suspense"
_FALLBACK_MARK = "fallback={"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _fallback_block(src: str) -> str:
    """Return the JSX value of the Suspense's `fallback={...}` prop,
    extracted brace-balanced starting from the prop's OWN `{` — not a
    fixed-width slice, so nothing outside the actual expression (a
    preceding comment, an unrelated later Skeleton usage) can leak in
    or satisfy an assertion about this block."""
    susp_i = src.find(_SUSPENSE_OPEN)
    assert susp_i != -1, "App.tsx must contain a <Suspense ...> element"
    fb_i = src.find(_FALLBACK_MARK, susp_i)
    assert fb_i != -1, (
        "App.tsx's <Suspense> must carry a fallback={...} prop")
    start = fb_i + len(_FALLBACK_MARK) - 1  # index of the opening '{'
    depth = 0
    i = start
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start + 1:i]
        i += 1
    raise AssertionError("fallback={...} brace never balanced/closed")


# ---------------------------------------------------------------------------
# AC-1 — no bare "Loading" text node in the fallback
# ---------------------------------------------------------------------------

def test_ac1_fallback_has_no_bare_loading_text_node():
    src = _read(_APP)
    fallback = _fallback_block(src)
    assert "Loading" not in fallback, (
        "AC-1: the Suspense fallback prop must no longer render the bare "
        '"Loading…" text node — today it is a single '
        '<div className="p-8 text-sm opacity-50">Loading…</div> '
        "(App.tsx:107-109).")


# ---------------------------------------------------------------------------
# AC-2 — the fallback composes Skeleton in more than one placeholder row
# ---------------------------------------------------------------------------

def test_ac2_skeleton_is_imported():
    src = _read(_APP)
    assert re.search(r"\bimport\s*\{[^}]*\bSkeleton\b[^}]*\}\s*from", src), (
        "AC-2: App.tsx must import the existing Skeleton primitive from "
        "components/ui (ui.tsx:172) — no new component file.")


def test_ac2_fallback_composes_skeleton_in_multiple_rows():
    src = _read(_APP)
    fallback = _fallback_block(src)
    count = len(re.findall(r"<Skeleton\b", fallback))
    assert count >= 2, (
        "AC-2: the fallback element must compose the Skeleton primitive in "
        "MORE THAN ONE placeholder row (a header-shaped row plus at least "
        "one more card/row shape), not a single bare rectangle — found "
        f"{count} <Skeleton usage(s) in the fallback block. Today the "
        "fallback is a single bare text div with zero Skeleton usages.")


def test_ac2_fallback_is_not_a_single_bare_div_anymore():
    src = _read(_APP)
    fallback = _fallback_block(src)
    # The pre-fix fallback is exactly one <div ...>Loading…</div> element
    # with no nested children — guard against a trivial rename that keeps
    # the single-element, no-Skeleton shape.
    assert "<Skeleton" in fallback, (
        "AC-2: fallback must render Skeleton, not merely a styled div.")


# ---------------------------------------------------------------------------
# AC-4 — scope stayed inside allowed_files; the pinned suite is this file
# ---------------------------------------------------------------------------

def test_ac4_route_definitions_are_untouched():
    src = _read(_APP)
    # The three routes this fallback covers per the story/plan must still
    # resolve through the SAME shared Suspense — no per-route special
    # casing was introduced to dodge these assertions.
    for route in (
        'path="/tasks"',
        'path="/tasks/:id"',
        'path="/conductor"',
    ):
        assert route in src, (
            f"AC-4: route {route!r} must remain defined in App.tsx — this "
            "slice changes only the shared Suspense fallback, not routing.")
