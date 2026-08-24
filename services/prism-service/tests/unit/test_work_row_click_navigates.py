"""UI contract test — clicking anywhere on a Work-screen row navigates to
the task (task d9f082fe follow-up, owner live, 2026-08-24: "when i click on
a task in the work view, it will navigate to the itme").

Before this fix, only the title text inside each row was a <Link> — the
rest of the row (status/who/prio/updated cells, empty padding) was inert,
same precedent LiveBar's own rows already set (owner 2026-07-16: "each one
of the items on the line is a task and needs to be able to be clicked
through").

The PRISM SPA has NO JS test runner, so this is pinned by asserting the
ACTUAL TSX source — same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_TASKS_PAGE = _HERE.parent.parent.parent / "prism_service" / "web" / "src" / "pages" / "TasksPage.tsx"


def _read() -> str:
    return _TASKS_PAGE.read_text(encoding="utf-8")


def _work_row_body(src: str) -> str:
    start = src.index("function WorkRow(")
    end = src.index("\n}\n", start)
    return src[start:end]


def test_the_whole_row_navigates_on_click():
    src = _read()
    row = _work_row_body(src)
    assert "const openable = !!item.id && !item.restricted;" in row
    assert 'const open = () => { if (openable) navigate(`/tasks/${item.id}`, { state: { from: "/tasks" } }); };' in row
    assert "onClick={open}" in row


def test_the_start_button_stops_propagation_so_it_does_not_also_navigate():
    src = _read()
    row = _work_row_body(src)
    assert "onClick={(e) => { e.stopPropagation(); onStart(); }}" in row


def test_the_provider_mirror_link_stops_propagation():
    src = _read()
    row = _work_row_body(src)
    assert 'onClick={(e) => e.stopPropagation()}' in row
