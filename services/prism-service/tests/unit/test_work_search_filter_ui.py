"""UI-FIRST acceptance test — Find a task on Work by typing part of it
(task a4c1bf03-072b-40cd-9ebb-d21049cb5007).

Owner 2026-07-29: "i need a way to search work for e696d952 a task on this".
There is no search on Work (pages/TasksPage.tsx) at all today: no <input>
anywhere in the file. Add one always-visible filter input that narrows the
merged native+external `items` list as the owner types, matching id-prefix,
full uuid, title, and tags, case-insensitively.

The PRISM SPA ships no JS test runner, so — exactly like
test_conductor_page_animated_cleanup_ui.py and the other *_ui.py suites — the
UI acceptance criteria are pinned by asserting the ACTUAL TSX SOURCE: the
rendered element and its real wiring, never a comment near it, and never a
fixed character window (the enclosing JSX/expression is parsed instead).

FAILS today because TasksPage.tsx has no <input> element, no `query` state,
and the `items` useMemo's `.filter()` callback (~line 184-193) only checks
`assigneeFilter` and `view` — no id/title/tag match clause exists at all.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_TASKS = _SRC / "pages" / "TasksPage.tsx"


def _read() -> str:
    assert _TASKS.exists(), f"expected source missing: {_TASKS}"
    return _TASKS.read_text(encoding="utf-8")


def _items_memo_body(src: str) -> str:
    """The body of the `items` useMemo — the ONE place both native and
    external rows are merged and filtered. Scoping here (rather than the
    whole file) means a match elsewhere (e.g. the assignee <select>, or a
    comment) cannot satisfy these assertions."""
    start = src.index("const items = useMemo(")
    # The memo's dependency array line closes it: `}, [tasks, external, ...`.
    end = src.index("}, [tasks, external", start)
    return src[start:end]


def _input_tag(src: str) -> str:
    """The full <input data-work-search ...> JSX tag. A naive `[^>]*` regex
    stops at the FIRST literal '>' it meets, which an arrow-function attr
    value like `onChange={(e) => setQuery(...)}` contains well before the
    tag's real close — so this walks brace depth instead, only treating a
    bare '>' as the tag end while depth is 0 (i.e. outside any `{...}`
    JSX expression)."""
    i = src.index("<input")
    assert "data-work-search" in src[i:i + 400], (
        "expected data-work-search on the <input> immediately at this index")
    depth = 0
    j = i
    while j < len(src):
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return src[i:j + 1]
        j += 1
    raise AssertionError("could not find the end of the <input ...> tag")


def _filter_callback_body(memo_body: str) -> str:
    """Just the predicate passed to `.filter(...)` inside the items memo —
    the actual boolean logic a row must satisfy to be rendered, not the
    merge/build code above it."""
    i = memo_body.index("return merged.filter(")
    # Walk to the matching close paren of filter(...) by brace/paren depth so
    # we capture the whole callback body regardless of its internal shape.
    depth = 0
    started = False
    j = i + len("return merged.filter(")
    k = j
    while k < len(memo_body):
        ch = memo_body[k]
        if ch == "(":
            depth += 1
            started = True
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        k += 1
    assert started, "no .filter( callback found in the items memo"
    return memo_body[j:k]


# ---------------------------------------------------------------------------
# AC-9: the input is always visible, above the table, with a stable hook.
# ---------------------------------------------------------------------------

def test_search_input_is_always_visible_above_the_table():
    src = _read()
    tag = _input_tag(src)
    i = src.index(tag)
    # Unconditional: not gated behind a truthy JSX expression like
    # `{expanded && <input ...>}` immediately preceding it.
    preceding = src[max(0, i - 40):i]
    assert "&&" not in preceding, (
        f"the search input must render unconditionally; found a guard just "
        f"before it: {preceding!r}")
    table_idx = src.index("<table")
    assert i < table_idx, "the search input must sit above the <table>"


def test_search_input_is_wired_to_query_state():
    src = _read()
    tag = _input_tag(src)
    assert re.search(r"value=\{query\}", tag), (
        f"the search input must be a controlled input bound to `query`; got {tag}")
    assert re.search(r"onChange=\{[^}]*setQuery", tag, re.DOTALL), (
        f"the search input must call setQuery on change; got {tag}")


def test_query_state_defaults_to_empty_string():
    src = _read()
    m = re.search(r'const \[query, setQuery\] = useState(?:<string>)?\(("")\)', src)
    assert m, (
        'expected `const [query, setQuery] = useState("")` (empty-string '
        'default) in TasksPage.tsx')


# ---------------------------------------------------------------------------
# AC-7: the match clause lives in the SAME merged filter as assigneeFilter/
# view, so it runs over native AND external rows identically.
# ---------------------------------------------------------------------------

def test_filter_predicate_sits_in_the_same_merged_filter_as_existing_clauses():
    src = _read()
    memo = _items_memo_body(src)
    assert "...tasks.filter(" in memo and "map(nativeToWork)" in memo
    assert "...external.map(externalToWork)" in memo
    body = _filter_callback_body(memo)
    assert "assigneeFilter" in body, (
        "the new match clause must sit inside the SAME merged .filter() as "
        "assigneeFilter, not a second filter scoped to only one source array")


# ---------------------------------------------------------------------------
# AC-1 / AC-2: id prefix AND full uuid match against `it.id`.
# ---------------------------------------------------------------------------

def test_filter_matches_id_prefix_and_full_id():
    src = _read()
    body = _filter_callback_body(_items_memo_body(src))
    assert re.search(r"it\.id\??\.toLowerCase\(\)", body), (
        f"expected the predicate to lowercase it.id for a case-insensitive "
        f"match; got: {body}")
    assert re.search(r"it\.id\??\.toLowerCase\(\)\.(startsWith|includes)\(", body), (
        "expected a startsWith/includes check against the lowercased id "
        "(a prefix check also matches the full uuid, so no separate "
        "length-gated branch is needed)")


# ---------------------------------------------------------------------------
# AC-3: title substring match.
# ---------------------------------------------------------------------------

def test_filter_matches_title_substring():
    src = _read()
    body = _filter_callback_body(_items_memo_body(src))
    assert re.search(r"it\.title\.toLowerCase\(\)\.includes\(", body), (
        f"expected a title.toLowerCase().includes(...) clause; got: {body}")


# ---------------------------------------------------------------------------
# AC-4: tag match.
# ---------------------------------------------------------------------------

def test_filter_matches_tags():
    src = _read()
    body = _filter_callback_body(_items_memo_body(src))
    assert re.search(r"\(it\.tags\s*\?\?\s*\[\]\)\.some\(", body), (
        f"expected an (it.tags ?? []).some(...) clause; got: {body}")
    # the .some(...) callback itself must lowercase+include the query, not
    # just check truthiness of tags
    m = re.search(r"\(it\.tags\s*\?\?\s*\[\]\)\.some\(([^;]*?)\)\s*[)&|]", body)
    assert m, f"could not isolate the tags .some(...) callback in: {body}"
    assert "toLowerCase()" in m.group(1) and "includes(" in m.group(1), (
        f"the tag match must be case-insensitive substring, got: {m.group(1)}")


# ---------------------------------------------------------------------------
# AC-5: empty query is a no-op (restores the full list).
# ---------------------------------------------------------------------------

def test_empty_query_is_a_noop_guard():
    src = _read()
    body = _filter_callback_body(_items_memo_body(src))
    # A guard that short-circuits the whole match clause when the trimmed
    # query is empty, e.g. `if (q) { ... }` or `!q || (...)`.
    assert re.search(r"\.trim\(\)\.toLowerCase\(\)", src), (
        "expected the query to be trimmed+lowercased once before matching")
    assert ("if (q)" in body or "if (query" in body
            or re.search(r"!q\s*\|\|", body) or re.search(r"q\s*&&", body)), (
        f"expected an explicit empty-query no-op guard in the filter body; "
        f"got: {body}")


# ---------------------------------------------------------------------------
# AC-6: a query matching nothing reaches the EXISTING empty-state branch
# (not a flattened/new one) — i.e. items.length is what gates it, and that
# branch still renders unmodified.
# ---------------------------------------------------------------------------

def test_no_match_falls_through_to_existing_empty_state():
    src = _read()
    assert "items.length === 0" in src, (
        "the existing items.length === 0 empty-state gate must still exist "
        "and be what a non-matching filter falls through to")
    i = src.index("items.length === 0")
    tail = src[i:i + 200]
    assert "No work in this view." in tail or "No" in tail, (
        f"expected the empty-state row to still render a message; got: {tail}")


# ---------------------------------------------------------------------------
# AC-8: no new grouped/sectioned rendering path — WorkRow stays the sole
# per-item renderer, and rows keep their own Status/gate column untouched.
# ---------------------------------------------------------------------------

def test_workrow_remains_sole_renderer_no_new_grouping_introduced():
    src = _read()
    assert src.count("function WorkRow") == 1, (
        "WorkRow must remain the single row-rendering function; no new "
        "status-bucketed component introduced")
    assert re.search(r"items\.map\(\(it, i\)\s*=>\s*\(\s*<WorkRow", src), (
        "the filtered `items` array must still be rendered via a single "
        "items.map(...) over <WorkRow>, not grouped into per-status sections")
    # the per-row status column must be untouched
    assert 'style={{ color: "var(--text-secondary)" }}>{item.status}' in src, (
        "each row must still render its own status via item.status")
