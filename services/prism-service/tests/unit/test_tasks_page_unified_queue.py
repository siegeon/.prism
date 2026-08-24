"""UI contract test for "Unify Work screen's two work queues into one"
(task d9f082fe).

The PRISM SPA has NO JS test runner, so UI-observable behavior is pinned by
asserting the ACTUAL web source (TSX) — same pattern as
tests/unit/test_conductor_page_animated_cleanup_ui.py.

Before the fix, TasksPage.tsx merged native PRISM tasks and external
GitHub/Jira entities via plain concatenation with no sort, so the rendered
table read as two stacked blocks (all native rows, then all external rows)
instead of one interleaved queue — and an external entity already imported
into a native task rendered a second time because nothing excluded it from
the external block.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASKS_PAGE = _SRC / "pages" / "TasksPage.tsx"
_API = _SRC / "lib" / "api.ts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_external_entity_type_carries_a_sortable_timestamp():
    api = _read(_API)
    assert "remote_updated_at?: string;" in api
    assert "last_seen_at?: string;" in api


def test_external_rows_get_a_common_sort_key():
    page = _read(_TASKS_PAGE)
    assert "updated_at: e.remote_updated_at || e.last_seen_at," in page


def test_merged_queue_is_sorted_by_one_common_key_not_two_blocks():
    page = _read(_TASKS_PAGE)
    assert "function workItemTimestamp(it: WorkItem): number" in page
    assert ".sort((a, b) => workItemTimestamp(b) - workItemTimestamp(a))" in page


def test_already_imported_external_entities_are_excluded_from_duplicate_render():
    page = _read(_TASKS_PAGE)
    assert "const renderedNativeIds = new Set(nativeRows.map((t) => t.id).filter(Boolean));" in page
    assert "!(e.task_id && renderedNativeIds.has(e.task_id))" in page
