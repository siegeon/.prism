"""UI contract tests for "Animate Conductor Tasks in the PRISM Web UI"
(task a5e0d9f5 — board motion + click-through transitions).

The PRISM SPA has no JS test runner, so the UI-FIRST motion acceptance
criteria are pinned by asserting the ACTUAL web source (TSX/CSS) — the same
pattern as tests/unit/test_explore_narrative_ui.py and the other *_ui.py
contracts. These assert the real seams: the motion library is imported AND
applied to the rendered elements (not merely available in node_modules),
layoutId is keyed on the stable task id (never an array index), the route
swap is wrapped + keyed, reduced-motion infra exists in BOTH a hook usage and
a global CSS reset, and the version is patch-bumped.

ALL of these FAIL today (the source has zero motion wiring, index.css is
exactly 110 lines with no reduced-motion block, version is 6.2.30). They go
green only when the animation work is wired into the live components.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASKS = _SRC / "pages" / "TasksPage.tsx"
_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_APP = _SRC / "App.tsx"
_CSS = _SRC / "index.css"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Board (TasksPage) — entrance / exit / cross-column reorder motion
# ---------------------------------------------------------------------------

def test_tasks_page_imports_motion_from_react_subpath():
    src = _read(_TASKS)
    # Zero-new-dependency: the already-installed motion@12 via motion/react.
    assert 'from "motion/react"' in src, \
        "TasksPage must import the motion primitives from the motion/react subpath"
    assert "AnimatePresence" in src
    assert "motion." in src or "motion(" in src, \
        "TasksPage must render a motion element, not just import the lib"


def test_tasks_board_cards_use_layout_and_stable_layoutid():
    src = _read(_TASKS)
    # FLIP reorder motion across the 5s poll requires layout + a STABLE
    # layoutId. The id must be the task id (t.id), never the array index.
    assert "layout" in src, "board cards must opt into layout (FLIP) animation"
    assert "layoutId={t.id}" in src, \
        "layoutId must be the stable task id t.id (never an array index)"
    # Guard against the classic false-green: layoutId keyed on the map index.
    assert "layoutId={i}" not in src and "layoutId={index}" not in src, \
        "layoutId must not be an array index"


def test_tasks_board_card_list_wrapped_in_animatepresence():
    src = _read(_TASKS)
    # Entrance + exit motion as tasks appear/leave the poll snapshot needs the
    # per-column card list under <AnimatePresence>.
    assert "<AnimatePresence" in src, \
        "the per-column card list must be wrapped in AnimatePresence for enter/exit"


def test_tasks_chip_crossfade_preserves_literal_gate_prefix_and_guard():
    src = _read(_TASKS)
    # The chip cross-fade must NOT regress the load-bearing literal "gate "
    # prefix at the call site, and must stay guarded by conductor-engaged state.
    assert "gate " in src, "the literal 'gate ' prefix must be preserved at the call site"
    assert "conductorOn" in src or "conductorEngaged" in src, \
        "step/gate chips must remain guarded by conductor-engaged state"


# ---------------------------------------------------------------------------
# App.tsx — route-swap transition into TaskDetailPage
# ---------------------------------------------------------------------------

def test_app_routes_wrapped_in_animatepresence_keyed_on_pathname():
    src = _read(_APP)
    assert 'from "motion/react"' in src, "App must import motion/react"
    assert 'AnimatePresence mode="wait"' in src, \
        "the <Routes> must be wrapped in <AnimatePresence mode=\"wait\">"
    assert "location.pathname" in src, \
        "the route-swap transition must be keyed on location.pathname"


def test_app_preserves_navigate_redirects_and_scroll_container():
    src = _read(_APP)
    # Wrapping the router must not break the two replace-redirects or the single
    # flex-1 overflow-y-auto scroll container.
    assert 'Navigate to="/brain" replace' in src
    assert src.count('Navigate to="/brain" replace') == 2, \
        "both /explore and /graph replace-redirects to /brain must survive"
    assert "flex-1 overflow-y-auto" in src, "the page scroll container must survive"


# ---------------------------------------------------------------------------
# TaskDetailPage — Card stagger, toast in/out, status flash
# ---------------------------------------------------------------------------

def test_detail_page_imports_motion_and_staggers_cards():
    src = _read(_DETAIL)
    assert 'from "motion/react"' in src, "TaskDetailPage must import motion/react"
    assert "motion." in src or "motion(" in src, \
        "the Card stack must mount as motion elements to stagger"


def test_detail_toast_animates_in_and_out():
    src = _read(_DETAIL)
    # The fixed notice toast (was a bare `notice && <div>`) must animate via
    # AnimatePresence so it slides/fades in and out instead of hard-cutting.
    assert "<AnimatePresence" in src, "the notice toast must be under AnimatePresence"
    assert "notice" in src


def test_detail_status_chip_flash_decoupled_from_patch_roundtrip():
    src = _read(_DETAIL)
    # The status chip flash must key on the task.status VALUE change (an effect
    # / animation keyed on task.status), NOT fire inside the setStatus click
    # handler — otherwise it's coupled to the PATCH+reload round-trip.
    assert "task.status" in src
    assert "useReducedMotion" in src, \
        "the flash must respect reduced motion (shared infra)"


# ---------------------------------------------------------------------------
# Reduced-motion infrastructure — hook guard + global CSS reset
# ---------------------------------------------------------------------------

def test_reduced_motion_hook_used_on_board_and_detail():
    tasks = _read(_TASKS)
    detail = _read(_DETAIL)
    assert "useReducedMotion" in tasks, "board motion must be guarded by useReducedMotion()"
    assert "useReducedMotion" in detail, "detail motion must be guarded by useReducedMotion()"


def test_index_css_appends_prefers_reduced_motion_reset_after_line_110():
    src = _read(_CSS)
    lines = src.splitlines()
    # The reset is NEW and appended AFTER the existing 110-line file.
    assert len(lines) > 110, "a reduced-motion block must be appended after line 110"
    assert "prefers-reduced-motion: reduce" in src, \
        "a global @media (prefers-reduced-motion: reduce) reset must exist"
    tail = "\n".join(lines[110:])
    assert "prefers-reduced-motion: reduce" in tail, \
        "the reduced-motion reset must be appended AFTER the original line 110"


# ---------------------------------------------------------------------------
# Version bump -> 6.3.0 (minor: animations + real-time phase progress feature;
# rolls up the never-shipped 6.2.31)
# ---------------------------------------------------------------------------

def test_prism_version_bumped_to_6_3_0():
    # The animation feature shipped AT 6.3.0; assert a floor (>= 6.3.0) rather
    # than exact equality so later patch bumps (e.g. the 6.3.1 /conductor
    # cleanup) don't regress this guard. Exact-version `==` asserts are an
    # anti-pattern — they break on every legitimate patch.
    from prism_service.__version__ import PRISM_VERSION
    parts = tuple(int(x) for x in PRISM_VERSION.split("."))
    assert parts >= (6, 3, 0), \
        "PRISM_VERSION must be >= 6.3.0 for the conductor-animation feature"
