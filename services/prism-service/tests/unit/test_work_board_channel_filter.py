"""UI contract test for "The Work board filters by channel"
(task 153fdf19-fe11-4e55-a702-4e988b9b88a4).

The PRISM SPA has NO JS test runner, so UI-observable behavior is pinned by
asserting the ACTUAL web source (TSX) — same pattern as
tests/unit/test_tasks_page_unified_queue.py.

Before the fix, TasksPage.tsx had no way to narrow the Work board by channel
at all: WorkItem.channel already carried the persisted provenance for native
rows and the provider for external rows (commit 571dcd27), but nothing read
it to filter, nothing rendered a pill row, and no choice survived a reload.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASKS_PAGE = _SRC / "pages" / "TasksPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_channel_pills_are_derived_from_distinct_row_channels():
    page = _read(_TASKS_PAGE)
    assert "const channels = useMemo(() => {" in page
    assert "if (it.channel) s.add(it.channel);" in page


def test_filter_predicate_reads_items_channel_field():
    page = _read(_TASKS_PAGE)
    assert "if (channelFilter && it.channel !== channelFilter) return false;" in page


def test_channel_choice_persists_under_the_documented_localstorage_key():
    page = _read(_TASKS_PAGE)
    assert "function channelStorageKey(project: string): string {" in page
    assert "return `prism.work.channel.${project}`;" in page
    assert "localStorage.getItem(channelStorageKey(project))" in page
    assert "localStorage.setItem(channelStorageKey(project), channelFilter);" in page


def test_localstorage_access_is_guarded_by_try_catch():
    page = _read(_TASKS_PAGE)
    assert (
        "try {\n      const saved = localStorage.getItem(channelStorageKey(project));"
        in page
    )
    assert (
        "try {\n      localStorage.setItem(channelStorageKey(project), channelFilter);"
        in page
    )


def test_all_pill_and_channel_pills_share_one_click_handler():
    page = _read(_TASKS_PAGE)
    assert '["", ...channels].map((c) => (' in page
    assert "onClick={() => setChannelFilter(c)}" in page
    assert '{c || "All"}' in page


def test_channel_pill_row_reuses_the_my_work_team_toggle_component_and_tokens():
    page = _read(_TASKS_PAGE)
    assert 'role="tablist" aria-label="Filter by channel"' in page
    assert 'aria-selected={channelFilter === c}' in page
    assert (
        'background: channelFilter === c ? "var(--surface-2)" : "var(--surface-1)",'
        in page
    )


def test_stored_channel_no_longer_present_resets_to_all():
    page = _read(_TASKS_PAGE)
    assert (
        "if (channelFilter && channels.length > 0 && !channels.includes(channelFilter)) {"
        in page
    )
    assert 'setChannelFilter("");' in page
