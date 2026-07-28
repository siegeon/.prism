"""RED scaffold — Claude has ONE home in settings (task c89edbeb).

Owner: "you now have two place for claude, claude is a sub section under
integrations". After 7fff8ef0 the settings nav offers Connectors (where Claude
is a peer card) AND a separate Claude auth entry rendering the same subject.
One subject, two doors.

Claude is an integration, so it lives under Connectors and nowhere else. Its
detail — CLI login status, credentials path, project source registration —
moves onto the Claude card as a COLLAPSED box, which by the owner's rule means
a lazy load: it must not mount, and must not start its 5s
/api/claude-auth/status poll, until someone opens it.

Per the lesson from 7fff8ef0: assert the AFFORDANCE a person clicks, not the
constant behind it. These read Sidebar.tsx and the ConnectorsSection body.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_SIDEBAR = _WEB / "components" / "Sidebar.tsx"
_SETTINGS = _WEB / "pages" / "SettingsPage.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _connectors_body() -> str:
    """The ConnectorsSection function body — where the Claude card is built."""
    src = _read(_SETTINGS)
    i = src.index("function ConnectorsSection")
    return src[i:]


# ── AC-1: the second door is gone ─────────────────────────────────────

def test_the_nav_has_no_claude_auth_entry():
    src = _read(_SIDEBAR)
    assert "Claude auth" not in src, (
        "the settings nav still offers a Claude auth entry — that is the "
        "second door the owner is looking at")
    assert "/settings/connections" not in src, (
        "the nav must not link to /settings/connections; Connectors is the "
        "only door to any integration")
    assert "/settings/connectors" in src, (
        "…while the Connectors entry itself must survive")


# ── AC-2: the content moved, it was not deleted ───────────────────────

def test_the_claude_card_owns_the_auth_and_source_detail():
    body = _connectors_body()
    for component in ("ClaudeAuthCard", "ClaudeSourceCard"):
        assert component in body, (
            f"{component} must render from inside ConnectorsSection — removing "
            f"the nav entry without this orphans the panel entirely")


def test_the_claude_card_has_something_to_click():
    """A detail nobody can open is the same as a deleted one."""
    body = _connectors_body()
    assert "Details" in body, (
        "the Claude card needs a visible Details affordance to open its "
        "auth/source panel")
    assert re.search(r"onClick=\{[^}]*[Dd]etail", body), (
        "the Details affordance must be wired to a click handler")


# ── AC-3: a collapsed box is a lazy load ──────────────────────────────

def test_the_detail_does_not_mount_until_it_is_opened():
    """ClaudeAuthCard polls /api/claude-auth/status every 5s while
    unauthenticated (SettingsPage.tsx ~line 736). Mounting it eagerly puts
    that poll on every visit to Connectors for a panel nobody opened."""
    body = _connectors_body()
    assert "ClaudeAuthCard" in body, (
        "cannot judge laziness of a panel that is not on the card yet")
    i = body.index("ClaudeAuthCard")
    # the guard sits immediately above the render, inside the same JSX branch
    guard = body[max(0, i - 400):i]
    assert re.search(r"(open|expand|show)\w*\s*===\s*|(open|expand|show)\w*\s*&&",
                     guard, re.IGNORECASE), (
        "the Claude detail must be guarded by an open-state so it mounts on "
        f"expand, not on page load; saw instead: {guard.strip()[-200:]}")


# ── AC-4: the old URL still lands somewhere real ──────────────────────

def test_the_legacy_connections_url_resolves_to_connectors():
    src = _read(_SETTINGS)
    i = src.index("function resolveSection")
    body = src[i:i + 900]
    assert re.search(r'"connections"\s*\)?\s*return\s+"connectors"', body) or \
        re.search(r'raw\s*===\s*"connections"[^\n]*"connectors"', body), (
        "resolveSection must map the legacy 'connections' value to "
        "'connectors', otherwise a bookmarked /settings/connections silently "
        "drops the user on Projects")


# ── AC-5: Claude auth cannot render as its own page ───────────────────

def test_the_standalone_claude_auth_section_is_gone():
    src = _read(_SETTINGS)
    assert 'section === "connections"' not in src, (
        "the standalone connections section must be removed once its cards "
        "live on the Claude connector card — otherwise Claude renders in two "
        "places again")


# ── AC-6: nothing else was taken with it ──────────────────────────────

def test_the_other_settings_entries_survive():
    src = _read(_SIDEBAR)
    for route in ("/settings/access-key", "/settings/projects",
                  "/settings/activity", "/settings/logs", "/settings/service"):
        assert route in src, f"{route} must remain in the settings nav"


# ── AC-7: this guard reads the affordance, not the constant ───────────

def test_the_guard_reads_the_nav_and_the_card():
    me = _read(_HERE)
    assert "Sidebar.tsx" in me and "ConnectorsSection" in me, (
        "a correct SectionId union must not be able to satisfy this suite "
        "while the screen still shows two Claudes")
