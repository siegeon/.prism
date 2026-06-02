"""RED scaffold — Phase 4: the consolidated maintenance clock is visible on
the /settings/activity BackgroundWorkersPanel (task b4712316).

No JS test runner in the web app, so the UI-FIRST acceptance criterion is
pinned by asserting SettingsPage.tsx SOURCE (same pattern as
test_adaptive_policy_ui.py / test_memory_page_activation_ui.py):

  * the panel still fetches GET /api/consolidation/workers;
  * the consolidated worker has a human label the panel renders — the
    source references a "Maintenance" clock label so the single folded
    worker is recognisable in the activity panel.

FAILS today: SettingsPage.tsx has no maintenance-clock label; the panel
still renders only the prior separate worker rows.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
        / "pages" / "SettingsPage.tsx")


def _src() -> str:
    return _WEB.read_text(encoding="utf-8")


def test_settings_page_fetches_workers_endpoint():
    src = _src()
    assert "/api/consolidation/workers" in src, (
        "SettingsPage must fetch GET /api/consolidation/workers"
    )


def test_settings_page_references_maintenance_clock():
    src = _src()
    assert "Maintenance" in src or "maintenance_clock" in src, (
        "the BackgroundWorkersPanel source must reference the consolidated "
        "Maintenance clock so the single folded worker is recognisable on "
        "/settings/activity (UI-first)"
    )
