"""RED scaffold — Connectors must be REACHABLE (task 7fff8ef0).

The Connectors section shipped and renders at /settings/connectors, but the
settings nav in Sidebar.tsx never learned about it, so the only way there was
typing the URL. The owner's report stands: "i still see the integrations in the
claude auth section".

The lesson this file encodes: ASSERT THE AFFORDANCE A PERSON USES, not the
constant behind it. e139295d's AC-1 checked SectionId / KNOWN_SECTIONS /
SECTION_META — all correct — while the surface stayed unreachable.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_SIDEBAR = _WEB / "components" / "Sidebar.tsx"
_SETTINGS = _WEB / "pages" / "SettingsPage.tsx"

ROUTE = "/settings/connectors"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


# ── AC-1: the nav offers it ───────────────────────────────────────────

def test_the_settings_nav_has_a_connectors_entry():
    src = _read(_SIDEBAR)
    assert ROUTE in src, (
        "the settings nav must link to /settings/connectors — without it the "
        "section is only reachable by typing a URL")
    i = src.index(ROUTE)
    entry = src[max(0, i - 120):i + 160]
    assert "Connectors" in entry, (
        f"the entry pointing at {ROUTE} must be LABELLED Connectors; saw: "
        f"{entry.strip()[:160]}")


# ── AC-2: the link cannot drift from the page ─────────────────────────

def test_the_nav_route_is_the_route_the_page_renders():
    """A nav entry pointing at a section the page does not render is a dead
    link; tie the two together."""
    assert '"connectors"' in _read(_SETTINGS), (
        "SettingsPage must resolve a connectors section for the nav entry to "
        "land on")
    assert 'section === "connectors"' in _read(_SETTINGS)


# ── AC-4: nothing else lost ───────────────────────────────────────────
#
# AC-3 (`test_claude_auth_remains_a_sibling_entry`) is RETIRED, not weakened.
# It pinned "/settings/connections stays in the nav", which was true when this
# file was written. Task c89edbeb then removed that entry at the owner's
# request ("you now have two place for claude"), moving Claude's credentials
# onto its own connector card. The current contract is asserted by
# test_claude_single_home.py, which requires the entry to be ABSENT — the
# direct opposite. Keeping both would pin a contradiction (task 2ba63a22).


def test_the_other_settings_entries_survive():
    src = _read(_SIDEBAR)
    for route in ("/settings/projects", "/settings/activity",
                  "/settings/logs", "/settings/service"):
        assert route in src, f"{route} must remain in the settings nav"


# ── AC-5: the guard fails on the nav, not the constant ────────────────

def test_the_guard_reads_the_nav_source():
    """Meta-check: this suite must assert against Sidebar.tsx, so a perfect
    SectionId/SECTION_META cannot satisfy it while the nav entry is missing —
    the exact state that shipped an unreachable section."""
    me = _read(_HERE)
    assert "_SIDEBAR" in me and "Sidebar.tsx" in me
