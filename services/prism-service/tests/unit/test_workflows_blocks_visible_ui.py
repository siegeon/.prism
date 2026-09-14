"""UI contract: the registered worker-seat blocks (prism_service/blocks/)
must actually be VISIBLE on /workflows, not just served by the API.

Owner, 2026-09-14 01:12Z: "we spent all weekend on making it work so it's
visible in the workflows view and now you're saying it's not visible in the
workflows view?" GET /api/workflows?project=prism already returns
task_count/node_count/block_count and a top-level "worker_seat_blocks"
catalog entry (api/workflows.py) -- the backend was never the gap. The gap
was display: the entry landed after triage/align_language/quickfix/
promote_to_law/knowledge_health in the directory instead of "directly under
CONDUCTOR, first screen", and the counts the front page reads out loud
(the "N tasks · M at gates" banner) never mentioned nodes or blocks at all,
so a viewer had no way to see the number rise from 7 to 12 without opening
the row.

Convention (test_conductor_page_animated_cleanup_ui.py, test_workflows_
live_run_banner_ui.py): the PRISM SPA has no JS test runner, so UI ACs are
pinned by asserting the ACTUAL TSX/TS source, comment-stripped, never a
fixed character window.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_PAGE = _WEB / "pages" / "WorkflowsPage.tsx"
_DEF_TYPES = _WEB / "lib" / "useWorkflowDef.ts"


def _strip_comments(src: str) -> str:
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _read(path: Path) -> str:
    assert path.exists(), f"expected {path} to exist"
    return _strip_comments(path.read_text(encoding="utf-8"))


def _function_body(src: str, signature: str) -> str:
    idx = src.find(signature)
    assert idx != -1, f"{signature!r} not found in source"
    brace_start = src.find("{", idx)
    depth = 0
    for i in range(brace_start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[brace_start:i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


def test_worker_seat_blocks_row_is_spliced_directly_under_conductor():
    src = _read(_PAGE)
    body = _function_body(
        src, "function reorderWorkerSeatBlocksAfterConductor(catalog: WorkflowCatalogEntry[])")
    assert '"worker_seat_blocks"' in body
    assert '"conductor"' in body
    assert "insertAt" in body and "+ 1" in body, (
        "the blocks row must be spliced to sit right after conductor's "
        "own index, not appended or left at the server's own position")
    # connectWorkflowCatalog actually applies the reorder to what
    # setWorkflows() stores, so the directory's existing generic
    # workflows.filter(!parent_id).map(...) render (untouched -- it walks
    # `workflows` in array order) reorders for free.
    assert "return reorderWorkerSeatBlocksAfterConductor(connected);" in src
    assert "{workflows.filter((workflow) => !workflow.parent_id).map((workflow) => {" in src


def test_banner_names_the_live_node_and_block_counts():
    src = _read(_PAGE)
    banner_idx = src.index("const bannerText = dataLoaded")
    banner_expr = src[banner_idx:banner_idx + 500]
    assert "data?.node_count" in banner_expr
    assert "data?.block_count" in banner_expr
    assert "nodes" in banner_expr and "blocks" in banner_expr
    # Still the same live-counts contract the earlier fixer pinned: the
    # tasks/gates counts and the wrapped statusLineText survive untouched.
    assert "conductorManaged.length" in banner_expr
    assert "conductorTaskWaitingAtGate" in banner_expr
    assert "${statusLineText}" in banner_expr


def test_workflow_def_state_is_actually_read_not_discarded():
    src = _read(_PAGE)
    assert "const [data, setData] = useState<WorkflowDef | null>(null);" in src, (
        "bannerText's node_count/block_count read requires the def+occupancy "
        "poll's own response to be bound to a real variable, not thrown away "
        "as `const [, setData]`")


def test_worker_seat_blocks_canvas_groups_nodes_by_prefix():
    src = _read(_PAGE)
    body = _function_body(src, "function workflowForGraph(workflow: WorkflowCatalogEntry)")
    assert '"worker_seat_blocks"' in body
    assert "localeCompare" in body, (
        "worker_seat_blocks' own steps must be sorted by id so same-prefix "
        "blocks (red.*, adjudicator.*, ...) land adjacent on the canvas's "
        "default array-order grid layout")


def test_workflow_def_type_carries_the_catalog_wide_counts():
    src = _read(_DEF_TYPES)
    body = _function_body(src, "export type WorkflowDef = {")
    assert "task_count?: number" in body
    assert "node_count?: number" in body
    assert "block_count?: number" in body
