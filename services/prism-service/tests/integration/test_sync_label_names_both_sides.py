"""RED scaffold — say exactly what syncs with what (task fc6ec2c9).

Owner: "can we be explicit that the sync here is prism tasks with github
issues? its not super clear."

The switch shipped in 01118728 says "Sync work with GitHub" and its off-copy
says "nothing is synced until you turn this on". Neither end of the arrow is
named, so nothing on screen says what would actually move.

It also renders on the CLAUDE card, offering to sync work with a credential
that has no issues. That is a defect I shipped, not a copy problem.
"""

from __future__ import annotations

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


def _switch_body() -> str:
    """The SyncSwitch component itself."""
    src = _read(_SETTINGS)
    body = src[src.index("function SyncSwitch"):]
    return body[:body.index("function ConnectorsSection")]


def _connectors_body() -> str:
    src = _read(_SETTINGS)
    return src[src.index("function ConnectorsSection"):]


# ── AC-1 / AC-2: the label names BOTH sides ───────────────────────────

def test_the_label_names_prism_tasks_and_the_providers_items():
    body = _switch_body()
    assert "PRISM tasks" in body, (
        "the label must name the PRISM side explicitly; 'Sync work with "
        "GitHub' never says what would move")
    assert "Sync work with" not in body, (
        "the vague label must be gone, not merely accompanied")


def test_the_provider_noun_comes_from_the_provider():
    """One generic word for every provider is the thing being replaced."""
    connectors = _connectors_body()
    assert "WORK_NOUN" in connectors, (
        "the provider's own work noun must be looked up per provider, so "
        "GitHub reads issues and a future provider can read something else")
    i = connectors.index("WORK_NOUN")
    table = connectors[i:i + 260]
    assert "github" in table and "issues" in table, (
        f"github must map to its own noun; saw: {table.strip()[:200]}")
    assert "jira" in table, "jira must have a noun too"


# ── AC-3 / AC-4 / AC-5: the sentence people actually read ─────────────

def test_both_states_are_described_and_neither_says_only_work():
    body = _switch_body()
    # The copy branches live in the on/off ternary.
    assert "tasks" in body.lower(), "the copy must name PRISM's tasks"
    # The component is generic on purpose: it interpolates the provider's own
    # noun rather than hardcoding "issues", so a future provider that calls
    # them something else reads correctly. Assert the INTERPOLATION, in the
    # description branches and not only the heading.
    prose = body[body.index("<p "):]
    assert "{noun}" in prose or "${noun}" in prose, (
        "the description a person reads to decide must name the provider's "
        "items, not just the heading above it")
    assert prose.count("noun") >= 2, (
        "both the on and off copy must name them; one branch naming them and "
        "the other saying 'work' is the recorded misfire")
    assert "Sync work with" not in body and "sync data" not in body.lower(), (
        "the recorded misfire: renaming the heading while the description a "
        "person reads to decide still says work or data")


def test_the_off_state_reads_calm_and_says_prism_still_holds_the_work():
    body = _switch_body()
    assert "record" in body.lower() or "source of truth" in body.lower(), (
        "the copy must say PRISM's tasks stay the record, so turning sync "
        "off never reads as losing your work")


# ── AC-6 / AC-7: a provider with nothing to sync gets no switch ───────

def test_a_connector_with_no_work_gets_no_sync_switch():
    connectors = _connectors_body()
    i = connectors.index("<SyncSwitch")
    opener = connectors.rindex("&& (", 0, i)
    condition = connectors[connectors.rindex("{", 0, opener):opener]
    assert "WORK_NOUN" in condition, (
        "the SyncSwitch render must be guarded by whether the provider HAS "
        f"work to sync; the branch is guarded only by: {condition.strip()!r}")


def test_the_guard_is_capability_not_a_hardcoded_name():
    """Recorded second misfire: excluding Claude BY NAME puts the exception in
    the wrong place, and the next non-work connector inherits a nonsense
    switch."""
    connectors = _connectors_body()
    i = connectors.index("<SyncSwitch")
    opener = connectors.rindex("&& (", 0, i)
    condition = connectors[connectors.rindex("{", 0, opener):opener]
    assert "claude" not in condition.lower(), (
        "the guard must not name Claude; absence of a work noun is the "
        f"capability answer. Saw: {condition.strip()!r}")
