"""UI-FIRST acceptance test — Ship unified team work view (task ae31c2c0).

The SPA ships no JS test runner, so — exactly like test_tasks_timeline_rich_
markdown_ui and the other *_ui.py tests — the UI-FIRST acceptance criteria are
pinned by asserting over the ACTUAL TSX SOURCE. `npm run build` (tsc -b) is the
companion typecheck in the task's verify list.

FAILS today because TasksPage is still the plain native-only board: no My Tasks/
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


# ── AC-1: My Tasks vs Team attention model ──────────────────────────────

def test_tasks_page_has_my_tasks_team_toggle():
    src = _read(_TASKS)
    assert '"mine" | "team"' in src or "'mine' | 'team'" in src, (
        "TasksPage must carry a My Tasks/Team view union")
    assert "My Tasks" in src and "Team" in src
    assert "data-work-view" in src, "the toggle must expose data-work-view for the demo"


# ── AC-2: source + assignee filters across providers ───────────────────

def test_tasks_page_has_source_and_assignee_filters():
    src = _read(_TASKS)
    # a provider/source filter spanning all three sources
    assert "native" in src and "github" in src and "jira" in src
    # The SOURCE filter is retired, not weakened (task feeec35e). Owner:
    # "everything is a PRISM task period since we worek it from here", so
    # slicing the list by provenance was the wrong control. Provenance now
    # shows as a LINK on the row, pinned by test_work_rows_link_out.py. The
    # assignee filter is untouched and still asserted.
    assert "assigneeFilter" in src or "data-assignee-filter" in src


# ── AC-3: provider badges, backlinks, remote-vs-local status ───────────

def test_external_rows_show_provider_badge_backlink_and_remote_status():
    src = _read(_TASKS)
    # ProviderBadge is retired (task feeec35e): it labelled EVERY row, so it
    # said nothing. The backlink claim survives and is what mattered, now as a
    # provider-named button rather than an unlinked lozenge.
    # SUPERSEDED by task 6fbbec35: mirrorOf (singular, at most one badge) was
    # replaced by the array-returning mirrorsOf, so a task linked to both
    # github and jira renders both badges instead of just the first.
    assert "mirrorsOf" in src, "a mirrored row must resolve its provider link(s)"
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
    """Re-pointed, not weakened (task 900a4fb9).

    This pinned `pullContainer` and a receipt link on IntegrationsCard, which
    rendered the repo picker behind a TEAM WORKSPACE. A local install never
    creates one, so that card only ever showed "No team workspace yet" and the
    owner could not reach any of it. RepoSync replaces it on the personal
    scope, so the claim is the same and the mechanism moved.
    """
    src = _read(_SETTINGS)
    assert "RepoSync" in src, "Settings needs a repository sync card"
    assert "runConnectorSync" in src, "the card triggers a manual sync"
    assert "trackConnectorRepo" in src, "the card chooses what to track"


def test_sidebar_labels_the_tasks_surface():
    """RE-ANCHORED for the Work -> Tasks nav rename: the /tasks nav item must
    be labelled Tasks, not merely contain the substring "Work" (which would
    still pass today via an unrelated historical comment)."""
    assert 'to: "/tasks", label: "Tasks"' in _read(_SIDEBAR)
