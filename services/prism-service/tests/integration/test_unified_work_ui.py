"""UI-FIRST acceptance test — Ship unified team work view (task ae31c2c0).

The SPA ships no JS test runner, so — exactly like test_tasks_timeline_rich_
markdown_ui and the other *_ui.py tests — the UI-FIRST acceptance criteria are
pinned by asserting over the ACTUAL TSX SOURCE. `npm run build` (tsc -b) is the
companion typecheck in the task's verify list.

FAILS today because TasksPage is still the plain native-only board: no My Work/
Team model, no source/assignee filters, no provider badges/backlinks, no remote-
vs-local status, no Start/Restricted, no keyboard nav; lib/api.ts has no
integration helpers; SettingsPage has no integrations sync card.
"""

from __future__ import annotations

from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_TASKS = _WEB / "pages" / "TasksPage.tsx"
_DETAIL = _WEB / "pages" / "TaskDetailPage.tsx"
_SETTINGS = _WEB / "pages" / "SettingsPage.tsx"
_SIDEBAR = _WEB / "components" / "Sidebar.tsx"
_API = _WEB / "lib" / "api.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


# ── AC-4 (deps first): lib/api.ts exposes the integration helpers ──────

def test_api_exposes_integration_helpers():
    src = _read(_API)
    for fn in ("listWorkspaces", "listIntegrationEntities", "listConnections",
               "pullContainer"):
        assert f"export async function {fn}" in src, f"lib/api.ts must export {fn}"
    # helpers hit the real integration endpoints, not a client-side stub
    assert "/integrations/entities" in src
    assert "/integrations/containers/" in src and "/pull" in src


# ── AC-1: My Work vs Team attention model ──────────────────────────────

def test_tasks_page_has_my_work_team_toggle():
    src = _read(_TASKS)
    assert '"mine" | "team"' in src or "'mine' | 'team'" in src, (
        "TasksPage must carry a My Work/Team view union")
    assert "My Work" in src and "Team" in src
    assert "data-work-view" in src, "the toggle must expose data-work-view for the demo"


# ── AC-2: source + assignee filters across providers ───────────────────

def test_tasks_page_has_source_and_assignee_filters():
    src = _read(_TASKS)
    # a provider/source filter spanning all three sources
    assert "native" in src and "github" in src and "jira" in src
    assert "sourceFilter" in src or "data-source-filter" in src
    assert "assigneeFilter" in src or "data-assignee-filter" in src


# ── AC-3: provider badges, backlinks, remote-vs-local status ───────────

def test_external_rows_show_provider_badge_backlink_and_remote_status():
    src = _read(_TASKS)
    assert "ProviderBadge" in src, "external rows need a provider badge component"
    # a real backlink to the provider (anchor with href), opening the source
    assert "href={" in src and "target=\"_blank\"" in src
    # remote status is rendered with a label DISTINCT from the local gate/step
    assert "Remote" in src, "external remote status must be labelled Remote"


# ── AC-5: intake/start action + restricted placeholder ─────────────────

def test_start_action_and_restricted_placeholder_exist():
    tasks = _read(_TASKS)
    detail = _read(_DETAIL)
    assert "Start" in tasks, "an imported pending item needs a Start action"
    # restricted external context is API-driven, shown as a placeholder
    assert "Restricted" in tasks or "Restricted" in detail
    assert "Restricted" in detail, "task detail must show a restricted placeholder"


# ── AC-6: keyboard navigation ──────────────────────────────────────────

def test_tasks_page_installs_keyboard_navigation():
    src = _read(_TASKS)
    assert 'addEventListener("keydown"' in src or "onKeyDown" in src, (
        "the Work surface must support keyboard navigation between rows")


# ── AC-4: SettingsPage integrations setup + manual sync + receipt ──────

def test_settings_has_integrations_sync_card():
    src = _read(_SETTINGS)
    assert "Integrations" in src, "Settings needs an Integrations card"
    assert "pullContainer" in src, "the card triggers a manual pull"
    assert "receipt" in src.lower(), "the card links the durable sync receipt"


def test_sidebar_labels_the_work_surface():
    assert "Work" in _read(_SIDEBAR)
