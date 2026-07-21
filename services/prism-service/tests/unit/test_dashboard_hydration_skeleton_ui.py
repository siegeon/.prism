"""Red tests for task 89e90d1a — "Dashboard stops flashing zeros before data loads".

The PRISM SPA has NO JS test runner, so UI-FIRST acceptance criteria are pinned
by asserting the ACTUAL web source (TSX) — the same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py and
tests/unit/test_animate_conductor_tasks_ui.py.

These FAIL against the current source: DashboardPage inits data/act to null with
NO hydration flag (:138-139) so the hero paints "No activity yet." (:227) and
every KPI/stat renders `?? []` / `?? 0` zeros (:234-238, :246-306) before the
first fetch resolves; LiveBar inits managed=[] (:67) so `state` computes "idle"
(:109-119) and paints "no task being driven — queue is quiet" (:248-252) before
the first poll; PageHeader gates its selector solely on projects.length > 0
(:31,49); ui.tsx exports no Skeleton; PRISM_VERSION is 7.1.22.

NOTE both files already contain a bare `useState(false)` (DashboardPage:79
`busy`, LiveBar:69 `collapsed`), so these assert a NAMED hydration flag rather
than any boolean hook — a bare-useState assertion would false-pass.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "prism_service" / "web" / "src"
_DASH = _SRC / "pages" / "DashboardPage.tsx"
_LIVEBAR = _SRC / "components" / "LiveBar.tsx"
_HEADER = _SRC / "components" / "PageHeader.tsx"
_UI = _SRC / "components" / "ui.tsx"
_VERSION = _ROOT / "prism_service" / "__version__.py"

# A NAMED hydration flag — "hydrated"/"loaded"/"polled"/"ready" — not any bool.
_FLAG_DECL = re.compile(
    r"const\s*\[\s*\w*(?:[Hh]ydrated|[Ll]oaded|[Pp]olled|[Rr]eady)\w*\s*,"
    r"\s*set\w+\s*\]\s*=\s*useState")
_FLAG_TOKEN = re.compile(r"[Hh]ydrated|[Ll]oaded|[Pp]olled|[Rr]eady")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _block(src: str, anchor: str, span: int = 900) -> str:
    """The JSX window starting at ``anchor`` — lets a test say 'THIS region is
    hydration-guarded' instead of matching anywhere in the file."""
    i = src.find(anchor)
    assert i != -1, f"anchor {anchor!r} vanished from the source — retarget the test"
    return src[i:i + span]


def _guarded(window: str) -> bool:
    """A region is honestly gated when it renders a Skeleton on an explicit
    hydration flag (not on data truthiness, which cannot tell 'not fetched yet'
    from 'fetched and genuinely empty')."""
    return "Skeleton" in window and bool(_FLAG_TOKEN.search(window))


# ---------------------------------------------------------------------------
# AC-1 — an explicit per-source hydration flag exists
# ---------------------------------------------------------------------------

def test_ac1_dashboard_declares_named_hydration_flag():
    src = _read(_DASH)
    assert _FLAG_DECL.search(src), (
        "AC-1: DashboardPage must declare a NAMED hydration flag (e.g. "
        "`const [stateLoaded, setStateLoaded] = useState(false)`) so a render "
        "can tell 'not fetched yet' from 'fetched and genuinely empty'. Today "
        "data/act init null (:138-139) with no such flag.")


def test_ac1_hydration_flag_is_set_when_each_fetch_resolves():
    src = _read(_DASH)
    # Anchor on the dashboard GET itself: "const load = useCallback" also
    # appears in StalenessCard higher up the file, and anchoring there tested
    # the wrong component.
    load = _block(src, "/api/dashboard/state", 700)
    assert _FLAG_TOKEN.search(load), (
        "AC-1: load() must FLIP the hydration flag as each of the two GETs "
        "settles (:141-146) — a flag never set is the same lie as no flag.")


# ---------------------------------------------------------------------------
# AC-2 — hero skeletons instead of "No activity yet."
# ---------------------------------------------------------------------------

def test_ac2_hero_is_hydration_guarded():
    src = _read(_DASH)
    hero = _block(src, "Brain activity")
    assert _guarded(hero), (
        "AC-2: pre-hydration the hero must render a Skeleton, not "
        "<Empty>No activity yet.</Empty> (:227).")


# ---------------------------------------------------------------------------
# AC-3 / AC-4 — KPI tiles and the four stat strips
# ---------------------------------------------------------------------------

def test_ac3_trend_kpi_tiles_are_hydration_guarded():
    src = _read(_DASH)
    kpis = _block(src, "<TrendKpi", 1200)
    assert _guarded(kpis), (
        "AC-3: the 5 TrendKpi tiles must render Skeletons pre-hydration — "
        "today `act?.series.searches ?? []` makes sum([]) paint a confident 0 "
        "(:234-238).")


def test_ac4_all_four_stat_strips_are_hydration_guarded():
    src = _read(_DASH)
    for anchor in ("Interactions · search", "Delivery flow",
                   "Token usage · per work session", "<SectionLabel>Governance"):
        assert _guarded(_block(src, anchor)), (
            f"AC-4: the {anchor!r} stat strip must render Skeletons "
            "pre-hydration, not 0 / em-dash placeholders (:246-306).")


# ---------------------------------------------------------------------------
# AC-5 — the skeleton must NEVER mask a genuinely empty store
# ---------------------------------------------------------------------------

def test_ac5_real_empty_states_survive_for_a_hydrated_empty_store():
    src = _read(_DASH)
    for literal in ("No activity yet.", "No searches yet.", "No events."):
        assert literal in src, (
            f"AC-5: {literal!r} must SURVIVE — the skeleton covers only the "
            "pre-hydration window; deleting the real empty state would make "
            "the dashboard lie in the opposite direction.")


def test_ac5_guard_keys_on_the_flag_not_on_data_truthiness():
    src = _read(_DASH)
    hero = _block(src, "Brain activity")
    assert not re.search(r"\{\s*pulse\s*\?", hero), (
        "AC-5: the hero must branch on the HYDRATION flag, not on `pulse` "
        "truthiness — `pulse ? ... : <Empty>` cannot distinguish 'not fetched "
        "yet' from 'fetched and genuinely empty', which is the whole defect.")


# ---------------------------------------------------------------------------
# AC-6 — LiveBar must not flash "Idle" before its first poll (likely_misfire)
# ---------------------------------------------------------------------------

def test_ac6_livebar_declares_a_polled_flag():
    src = _read(_LIVEBAR)
    assert _FLAG_DECL.search(src), (
        "AC-6: LiveBar must declare a NAMED polled/hydrated flag — today "
        "`managed` inits [] (:67) so `state` computes 'idle' before the first "
        "/api/conductor/state poll resolves.")


def test_ac6_idle_state_is_gated_behind_the_first_poll():
    src = _read(_LIVEBAR)
    state = _block(src, "const state:", 500)
    assert _FLAG_TOKEN.search(state), (
        "AC-6: the state union (:109-119) must account for 'not yet polled' "
        "so the bar cannot resolve to 'idle' pre-hydration.")


def test_ac6_queue_is_quiet_copy_is_gated():
    src = _read(_LIVEBAR)
    quiet = _block(src, "no task being driven", 400)
    before = src[max(0, src.find("no task being driven") - 400):
                 src.find("no task being driven")]
    assert _FLAG_TOKEN.search(before + quiet), (
        "AC-6: 'no task being driven — queue is quiet' (:248-252) must be "
        "gated behind the first poll — this is the recorded likely_misfire.")


# ---------------------------------------------------------------------------
# AC-7 — the project selector must not pop in from absent
# ---------------------------------------------------------------------------

def test_ac7_selector_not_gated_solely_on_projects_length():
    src = _read(_HEADER)
    assert _FLAG_TOKEN.search(src), (
        "AC-7: PageHeader must track a hydration flag (:31) so the selector "
        "holds a stable slot instead of popping in.")
    selector = _block(src, "{projects.length > 0", 300) if \
        "{projects.length > 0" in src else ""
    assert not selector or _FLAG_TOKEN.search(selector), (
        "AC-7: the selector must not be gated SOLELY on `projects.length > 0` "
        "(:49) — that renders nothing at all pre-hydration.")


# ---------------------------------------------------------------------------
# Skeleton primitive lives in the existing ui.tsx (no new component file)
# ---------------------------------------------------------------------------

def test_skeleton_primitive_exported_from_existing_ui_module():
    src = _read(_UI)
    assert re.search(r"export\s+(?:function|const)\s+Skeleton\b", src), (
        "ui.tsx must export a Skeleton primitive (none exists today; exports "
        "are Card/SectionLabel/Kpi/Pill/Button/Empty/Page/ErrorBanner) — added "
        "HERE, not in a new file (no-new-file-sprawl).")


# ---------------------------------------------------------------------------
# AC-8 — version bump rides the same commit
# ---------------------------------------------------------------------------

def test_ac8_version_patch_bumped_with_a_notes_line():
    src = _read(_VERSION)
    assert 'PRISM_VERSION = "7.1.23"' in src, (
        "AC-8: PRISM_VERSION must patch-bump 7.1.22 -> 7.1.23 in the same "
        "commit as the user-visible change.")
    assert "v7.1.23:" in src, (
        "AC-8: PRISM_VERSION_NOTES needs a one-line v7.1.23 entry.")
