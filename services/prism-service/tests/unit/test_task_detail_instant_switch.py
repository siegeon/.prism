"""RED - task-switch feels instant (task 93d6c6f3), AC-1/AC-2/AC-3/AC-7.

TODAY (confirmed by direct read of both files in this worktree):
- TaskDetailPage.tsx declares `const [task, setTask] = useState<Task | null>(null)`
  (:805) and `load()` (:939-970) is the ONLY function that ever calls
  `setTask` with a non-updater value - once with the fetched `d.task` at
  :949 (after `await api.get(...)` resolves) and once as an SSE merge
  updater `setTask((prev) => (prev ? { ...prev, ...fields } : prev))` at
  :988. `useEffect(() => { load(); }, [load])` (:972) re-fires on every
  `id` change but never resets `task` before the fetch resolves, so a
  switch renders the PREVIOUS task's title/status/step under the NEW
  task's URL for the whole 0.2-1.8s fetch (the stale frame AC-2 names).
  grep confirms exactly 2 setTask call sites today - a fix that adds a
  synchronous seed/reset is a THIRD, structurally distinct call site.
- TasksPage.tsx's board row Link (:374-381) already navigates with
  `state={{ from: "/tasks" }}` and nothing else - the row's own lean
  fields (id/title/status/priority/workflow_step/gate_state/parent_id/
  tags, fetched at :162) are computed and then thrown away at the exact
  moment they could seed the next page's first paint (AC-3).
- `load()`'s fetch (:943) requests `/api/tasks/${id}?project=${project}`
  with no `include_history` param, so once lever B (cherry-picking commit
  2e7c816 / task f77d3e94, which gates history/spend/tokens behind
  `include_history=false` server-side) lands, the client must explicitly
  ask for `include_history=true` wherever it renders those fields or
  AC-7/NFR-1 (no field silently disappears) breaks.

Source-reading against the TSX (the SPA has no JS test runner - the
established convention here, see
tests/unit/test_agent_runs_cannot_self_approve.py and 10+ other suites).
Assertions match the RENDERED tag / the actual call-site shape via
brace/paren balancing, never a bare identifier and never a fixed
character window - a comment can't satisfy them.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_DETAIL_TSX = _WEB_SRC / "pages" / "TaskDetailPage.tsx"
_TASKS_TSX = _WEB_SRC / "pages" / "TasksPage.tsx"


def _strip_js_comments(src: str) -> str:
    return re.sub(r"(?m)(?<!:)//.*$", "", src)


def _match_balanced(src: str, open_idx: int, open_ch: str, close_ch: str) -> str:
    """From src[open_idx] == open_ch, return the text through the matching
    close_ch by depth-counting - not a fixed window, so a comment or an
    unrelated later close can't end the match early (repo convention, see
    _jsx_expression_around in test_agent_runs_cannot_self_approve.py)."""
    assert src[open_idx] == open_ch
    depth = 0
    for i in range(open_idx, len(src)):
        c = src[i]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    raise AssertionError(f"unbalanced {open_ch}{close_ch} from index {open_idx}")


def _set_task_call_sites(src: str) -> list[str]:
    """Every `setTask(...)` call, full text through its matching paren."""
    sites = []
    for m in re.finditer(r"\bsetTask\(", src):
        sites.append(_match_balanced(src, m.end() - 1, "(", ")"))
    return sites


# ----------------------------------------------------------------------
# AC-3: the board row link carries the row payload as router state, so
# the FIRST-EVER open of a task is seeded too, not only a revisit.
# ----------------------------------------------------------------------

def test_board_row_link_carries_the_row_as_router_state():
    src = _strip_js_comments(_TASKS_TSX.read_text(encoding="utf-8"))
    anchor = "to={`/tasks/${item.id}`}"
    assert anchor in src, (
        "the board row's Link to the task detail route moved or was renamed"
    )
    start = src.index(anchor)
    tag_start = src.rindex("<Link", 0, start)
    close = src.index("</Link>", start)
    element = src[tag_start:close]

    state_m = re.search(r"\bstate=\{\{", element)
    assert state_m, "the row <Link> must carry a `state={{ ... }}` payload"
    state_obj = _match_balanced(element, state_m.end() - 1, "{", "}")
    assert "item" in state_obj, (
        "the Link's router state only carries `from` today - it must also "
        "carry the board row (`item`) itself so TaskDetailPage can seed its "
        "header from it on the very first open of a never-before-opened "
        f"task (AC-3). state object seen: {state_obj}"
    )


# ----------------------------------------------------------------------
# AC-2 + AC-1: a synchronous, non-fetch, non-SSE-merge setTask call must
# exist - the stale-frame reset/seed - distinct from today's exactly 2
# call sites (the post-await fetch assignment and the SSE merge updater).
# ----------------------------------------------------------------------

def test_task_state_has_a_synchronous_reset_distinct_from_fetch_and_sse():
    src = _strip_js_comments(_DETAIL_TSX.read_text(encoding="utf-8"))
    sites = _set_task_call_sites(src)

    def is_sse_merge(call: str) -> bool:
        head = call.split("=>", 1)[0]
        return "(prev)" in head

    def is_fetch_assignment(call: str) -> bool:
        return "d.task" in call

    other = [c for c in sites if not is_sse_merge(c) and not is_fetch_assignment(c)]
    assert other, (
        "TaskDetailPage.tsx must call setTask a THIRD, distinct way: a "
        "synchronous reset/seed on `id` change, separate from the SSE merge "
        "updater and the post-fetch assignment inside load(). Today there "
        f"are exactly {len(sites)} setTask call site(s), all fetch/SSE. "
        f"Sites seen: {sites}"
    )
    # The reset must not be gated behind load()'s await - it has to run
    # whether or not the fetch has resolved yet. Cheap proxy: the
    # call's OWN text may not itself reference an awaited value.
    for call in other:
        assert "await" not in call, (
            f"a synchronous reset/seed must not depend on an awaited value: {call}"
        )


# ----------------------------------------------------------------------
# AC-7 / NFR-1: once lever B (cherry-picked include_history opt-in) gates
# history/spend/tokens server-side, the client fetch that feeds the
# Timeline/Spend/Trace surfaces must keep asking for them explicitly -
# same views, not fewer.
# ----------------------------------------------------------------------

def test_load_requests_include_history_true():
    src = _strip_js_comments(_DETAIL_TSX.read_text(encoding="utf-8"))
    m = re.search(r"api\.get<[^>]*>\(\s*`/api/tasks/\$\{id\}([^`]*)`", src)
    assert m, "load()'s GET /api/tasks/{id} call moved or changed shape"
    query = m.group(1)
    assert "include_history=true" in query, (
        "load() must pass include_history=true so the history-gated fields "
        "(Timeline, per-turn tokens, Spend) keep arriving once lever B lands "
        f"server-side - AC-7/NFR-1. Query suffix seen: {query!r}"
    )
