"""RED scaffold — the whole connector card opens (task 064fc09b).

Owner: "clicking anywhere in the panel in the connectors section should open
it". Today the only thing that opens a connector is a small Details button in
the corner of the Claude card; the card itself is inert.

Per the lesson from 7fff8ef0, these assert the AFFORDANCE a person uses — the
card element and its handlers — not a toggle helper that could be perfectly
correct while the panel stays dead to a click.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SETTINGS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
             / "SettingsPage.tsx")


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _connectors_body() -> str:
    src = _read(_SETTINGS)
    return src[src.index("function ConnectorsSection"):]


def _card_region() -> str:
    """The connector card element itself — from <Card key=… to the end of the
    card's own header, which is where a click must be caught."""
    body = _connectors_body()
    i = body.index("<Card key=")
    return body[i:i + 1400]


# ── AC-1: the card is the control ─────────────────────────────────────

def test_the_card_itself_toggles_its_detail():
    region = _card_region()
    assert "onClick=" in region, (
        "the connector card has no click handler of its own — only the small "
        "Details button opens it, which is the owner's complaint")
    assert re.search(r"onClick=\{[^}]*(setOpenDetail|toggleDetail)", region), (
        "the card's click must drive the open-state, not some unrelated action")


# ── AC-2: keyboard and assistive tech reach it too ────────────────────

def test_the_card_is_reachable_without_a_mouse():
    region = _card_region()
    assert 'role="button"' in region, "the clickable card must announce a role"
    assert "tabIndex=" in region, "the card must be focusable"
    assert "aria-expanded=" in region, (
        "the card must report whether it is open")
    assert "onKeyDown=" in region, "Enter/Space must activate the card"
    keys = region[region.index("onKeyDown="):]
    assert '"Enter"' in keys[:400] and ('" "' in keys[:400] or "'Space'" in keys[:400]), (
        "onKeyDown must handle BOTH Enter and Space, the two keys a button "
        "responds to")


# ── AC-3: it looks like something you can click ───────────────────────

def test_the_card_reads_as_clickable():
    region = _card_region()
    assert "cursor-pointer" in region, (
        "a click target with a default cursor is invisible as an affordance")
    body = _connectors_body()
    assert "ChevronDown" in body, "the card needs a chevron showing its state"
    chev = body[body.index("ChevronDown"):]
    assert "rotate" in chev[:300], (
        "the chevron must rotate with the open state so the card shows "
        "whether it is open")


# ── AC-4: the real buttons still do their own job ─────────────────────

def test_action_buttons_do_not_toggle_the_card():
    """The recorded likely_misfire: starting an OAuth round trip must not
    collapse the panel it was started from."""
    body = _connectors_body()
    handlers = re.findall(r"onClick=\{\([^)]*\)\s*=>\s*connect\([^}]*\}", body)
    assert handlers, "expected the Connect/Reconnect handlers to be present"
    for h in handlers:
        assert "stopPropagation" in h, (
            f"this action button would ALSO toggle the card: {h}")


# ── AC-5: every connector opens, not just Claude ──────────────────────

def test_any_connector_can_be_opened():
    body = _connectors_body()
    assert re.search(r"openDetail\s*===\s*c\.provider", body), (
        "the open-state must be compared against c.provider so GitHub and "
        "Jira open too — not hardcoded to the literal \"claude\"")
    assert re.search(r'c\.provider\s*!==\s*"claude"', body), (
        "a non-Claude detail branch must render something worth opening: what "
        "the connector is tracking, or the configuration it still needs")


# ── AC-6: widening the click target did not widen the mount ───────────

def test_the_detail_is_still_lazy():
    body = _connectors_body()
    assert "<ClaudeAuthCard" in body, "the Claude panels must still be here"
    i = body.index("<ClaudeAuthCard")
    opener = body.rindex("&& (", 0, i)
    condition = body[body.rindex("{", 0, opener):opener]
    assert re.search(r"open|expand|show", condition, re.IGNORECASE), (
        "the Claude detail must still be guarded by the open-state so no "
        "/api/claude-auth/status poll starts on page load; the branch is "
        f"guarded only by: {condition.strip()!r}")


# ── AC-7: the guard reads the affordance, not the constant ────────────

def test_the_guard_reads_the_card_element():
    me = _read(_HERE)
    assert "<Card key=" in me and "ConnectorsSection" in me, (
        "a correct toggle helper must not be able to satisfy this suite "
        "while the card stays inert to a click")
