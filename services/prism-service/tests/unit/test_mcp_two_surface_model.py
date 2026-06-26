"""Agent-facing MCP surface teaches the 4->2 knowledge consolidation.

Two surfaces only: Brain = the code-graph VISUALIZATION + retrieval; Understand
= ONE unified wiki over curated memory projected as OKF concepts (reached via
okf_index / okf_get / okf_graph). This pins the GUIDE text + okf tool framing +
the new okf_graph read-through tool + the LEGACY marking of understand_*.

Conductor task: a4995b7a.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# A pending consolidation candidate would prefix the JSON result with a nudge
# header; disable it so okf_graph's payload stays parseable.
os.environ["PRISM_MCP_AUGMENT_NUDGES"] = "false"


def _call(tool_name, arguments=None, project_id="prism"):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool_name, arguments or {}, project_id=project_id))


def _text(result):
    assert len(result) >= 1
    return result[0].text


# ----------------------------------------------------------------------
# AC-1 — prism_guide teaches the 2-surface model, not "four pillars".
# ----------------------------------------------------------------------


def test_prism_guide_drops_four_pillars_for_two_surfaces():
    guide = _text(_call("prism_guide", {}))
    # Collapse whitespace so a line-wrapped "four tightly coupled\npillars"
    # is still caught.
    low = " ".join(guide.split()).lower()
    assert "understand" in low
    assert "wiki" in low
    assert "four pillars" not in low
    assert "four tightly coupled pillars" not in low


# ----------------------------------------------------------------------
# AC-2 — okf_graph dispatches and returns a concept graph.
# ----------------------------------------------------------------------


def test_okf_graph_returns_nodes_and_edges():
    result = json.loads(_text(_call("okf_graph", {})))
    assert "nodes" in result
    assert "edges" in result


# ----------------------------------------------------------------------
# AC-3 — okf_index / okf_get framed as the Understand wiki.
# ----------------------------------------------------------------------


def test_okf_tools_framed_as_understand_wiki():
    from prism_service.mcp.tools import TOOLS
    by_name = {t.name: t for t in TOOLS}
    assert "okf_graph" in by_name, "okf_graph tool must be registered"

    idx = by_name["okf_index"].description
    assert "Understand" in idx
    assert "wiki" in idx.lower()

    get = by_name["okf_get"].description
    assert "Understand" in get


# ----------------------------------------------------------------------
# AC-4 — every understand_* tool is marked LEGACY.
# ----------------------------------------------------------------------


def test_understand_tools_marked_legacy():
    from prism_service.mcp.tools import TOOLS
    legacy = [t for t in TOOLS if t.name.startswith("understand_")]
    assert legacy, "expected understand_* tools registered in TOOLS"
    for t in legacy:
        assert "LEGACY" in t.description, f"{t.name} not marked LEGACY"
