"""RED scaffold — Work rows link out, they do not label themselves (task feeec35e).

Owner 2026-07-28, with a screenshot of the Work table: "it should have a GITHUB
button next to it if its synced from GITHUB, everything is a PRISM task period
since we worek it from here".

A label that is true of every row tells you nothing. The ALL/PRISM/GITHUB/JIRA
filter makes the same mistake as a control, treating where a task came from as
a way to slice the list. Provenance is not a category; a badge earns its place
only by taking you somewhere.

My Work / Team is a DIFFERENT question (mine vs not mine) and must survive
untouched, which the owner said explicitly.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TASKS_PAGE = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
               / "TasksPage.tsx")


def _read() -> str:
    assert _TASKS_PAGE.exists(), f"expected source missing: {_TASKS_PAGE}"
    return _TASKS_PAGE.read_text(encoding="utf-8")


def _row_body() -> str:
    """ONLY the WorkRow rendering. Asserting against the whole file let an
    incidental match elsewhere satisfy a claim about the row."""
    src = _read()
    i = src.index("function WorkRow")
    return src[i:]


# ── AC-1: nothing labels a row PRISM ──────────────────────────────────

def test_no_row_is_badged_prism():
    src = _read()
    assert 'label: "PRISM"' not in src, (
        "the source list still names PRISM as a row label; every row on this "
        "page IS a PRISM task, so the label says nothing")
    assert "ProviderBadge" not in src, (
        "the unlinked provider badge must be gone; a badge earns its place by "
        "taking you somewhere")


# ── AC-2 / AC-3: a mirrored row offers a real outbound link ───────────

def test_a_mirrored_row_links_to_the_real_thing():
    src = _row_body()
    assert re.search(r"<a\s[^>]*href=\{", src), (
        "a task mirrored from GitHub needs an actual anchor to its issue, not "
        "a styled span")
    m = re.search(r"<a\s", src)
    assert m, "no anchor element in the row at all"
    head = src[m.start():m.start() + 400]
    assert 'target="_blank"' in head and "rel=" in head, (
        "an outbound link must open out and carry rel; got: "
        f"{head.strip()[:160]}")


def test_the_link_is_named_for_the_provider():
    """It should read GITHUB, so a person knows where it goes before clicking.

    The label is computed alongside the href rather than hardcoded in the row,
    so read the whole module: what matters is that a label naming the provider
    reaches the anchor, not where the string literal lives."""
    src = _read()
    assert re.search(r"toUpperCase\(\)|>GITHUB<|\"GITHUB\"", src), (
        "the link must name the provider it goes to")
    assert "{mirror.label}" in _row_body(), (
        "the anchor must RENDER that label, not just compute it")


# ── AC-4: only mirrored tasks get one ─────────────────────────────────

def _mirrors_of_body() -> str:
    src = _read()
    i = src.index("function mirrorsOf")
    j = src.index("\nfunction ", i + 1)
    return src[i:j]


def test_a_task_with_no_counterpart_gets_no_button():
    """Recorded first misfire: a link on every row, so a native task shows a
    button that goes nowhere.

    SUPERSEDED assertion retired in place (task 1ab8fd0f): this used to look
    for a single `{mirror && <a ...>}` boolean guard immediately before the
    anchor. Task 6fbbec35 (commit c742492) replaced that with
    `mirrorsOf(item).map(...)` so a task can render one badge PER active
    mirror instead of at most one — a strictly better behaviour, not a
    regression (see mirrorsOf, TasksPage.tsx:101-111). "No counterpart gets
    no button" now holds because mirrorsOf returns an EMPTY array when
    item.url is unset and item.mirrors is empty/absent, so .map() renders
    nothing; assert that guard shape instead of the retired `&&` token.
    """
    src = _row_body()
    m = re.search(r"<a\s", src)
    assert m, "no anchor element to judge; the link does not exist yet"
    i = m.start()
    opener = src.rindex("mirrorsOf(item).map(", 0, i)
    assert i - opener < 200, (
        "the anchor must be rendered directly inside mirrorsOf(item).map(...); "
        f"got {i - opener} chars between the guard and the anchor")

    mirrors_fn = _mirrors_of_body()
    assert "if (item.url)" in mirrors_fn and "item.mirrors ?? []" in mirrors_fn, (
        "mirrorsOf must return an EMPTY array, not a fallback element, when a "
        "task has neither item.url nor active mirrors, so a task with no "
        f"counterpart truly renders no button; got: {mirrors_fn.strip()[:200]!r}")


# ── AC-5: the source filter is gone, control AND behaviour ────────────

def test_the_source_filter_is_gone_completely():
    """Recorded second misfire: deleting the buttons while leaving the state
    and the predicate means the list is still filtered by something nobody
    can see."""
    src = _read()
    assert "sourceFilter" not in src, (
        "the sourceFilter state must go with the buttons, or the list stays "
        "filtered by an invisible control")
    assert "data-source-filter" not in src, (
        "the ALL/PRISM/GITHUB/JIRA button row must be gone")


# ── AC-6: the ownership filter survives ───────────────────────────────

def test_my_work_and_team_survive():
    """Recorded third misfire. It sits inches away in the same toolbar and is
    the easy thing to delete by accident, and the owner asked to keep it."""
    src = _read()
    assert "My Work" in src and "Team" in src, (
        "My Work / Team answers a different question, mine vs not mine, and "
        "must survive")
    assert re.search(r"view\s*===\s*[\"']mine[\"']", src), (
        "the ownership predicate must still run, not just the labels")


# ── AC-7: the guard reads the row ─────────────────────────────────────

def test_the_guard_reads_the_row_rendering():
    me = _HERE.read_text(encoding="utf-8")
    assert "TasksPage.tsx" in me and "<a " in me, (
        "a correct lookup table must not satisfy this suite while the row "
        "itself is unchanged")


# ── Layout: the pill leads the line, long titles truncate ─────────────

def test_the_provider_pill_is_the_first_thing_on_the_summary_line():
    """Owner 2026-07-28: "i want the pill on the left hand side of the summary
    line not the right".

    SUPERSEDED assertion retired in place (task 1ab8fd0f): this used to look
    for the literal substring "{mirror &&". Task 6fbbec35 (commit c742492)
    replaced that single boolean-guarded badge with `mirrorsOf(item).map(...)`
    (TasksPage.tsx:369), so locate the pill via that render call instead; the
    ordering requirement (pill before id before title) is unchanged.
    """
    body = _row_body()
    cell = body[body.index('<td className="px-3 py-1.5">'):]
    pill = cell.index("mirrorsOf(item).map(")
    ident = cell.index("item.displayKey")
    title = cell.index("item.title")
    assert pill < ident < title, (
        "the provider pill must lead the summary line, before the id and the "
        f"title; got pill@{pill}, id@{ident}, title@{title}")


def test_a_long_task_name_is_truncated():
    """Owner: "some of the rows are two long we should truncate task names"."""
    body = _row_body()
    i = body.index("item.title")
    opening = body.rindex("className=", 0, i)
    cls = body[opening:i]
    assert "truncate" in cls, "a long title must truncate rather than stretch the row"
    assert "min-w-0" in cls, (
        "truncate does nothing to a flex item that cannot shrink; the title "
        "needs min-w-0 too, which is why the rows were still running long")


def test_the_table_uses_fixed_layout_so_columns_share_its_width():
    """Owner 2026-07-28: "the horizontal scroll still happens, maybe the
    truncate needs to dynamically calculate the % of space it can use based on
    its total table width".

    That is table-layout: fixed. Under the default `auto`, the Summary column
    sizes to its CONTENT, so a long title grows the cell and `truncate` can
    never engage no matter what classes the title carries.
    """
    src = _read()
    i = src.index("<table")
    tag = src[i:src.index(">", i)]
    assert "table-fixed" in tag, (
        f"the work table must use fixed layout or a long title widens it past "
        f"the viewport; got: {tag}")
