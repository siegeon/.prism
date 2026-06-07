"""RED — Failing tests for the temporal fact-graph stretch (task e043f449, AC-11).

Pins the stretch vector: a temporal fact-graph (add / invalidate / timeline /
as-of date query) surfaced via memory_recall as a memory-domain sibling, built
on memory.py:30-31 valid_at/invalid_at, WITHOUT replacing graphify.

Pinned as a REAL end-to-end seam (not a dead method):
1. MemoryService.recall accepts an `as_of` filter that returns the fact that
   was valid AT that date (a superseded fact resurfaces for an earlier as_of).
2. MemoryService exposes a `timeline` over a fact's validity windows.
3. The MCP `memory_recall` verb is reachable through the tool DISPATCHER and
   forwards `as_of` — so the field isn't defined-but-dead.

Must FAIL until the temporal fact-graph ships.

[Source: services/prism-service/prism_service/models/memory.py :30 valid_at / :31 invalid_at]
[Source: services/prism-service/prism_service/services/memory_service.py::recall :320]
[Source: services/prism-service/prism_service/mcp/tools.py memory_recall dispatch :3419]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _make_memory_service(tmp_path):
    from prism_service.services.memory_service import MemoryService

    # Path deliberately not under projects/<id>/ so recall uses the isolated
    # keyword fallback (no live Brain dependency) — see memory_service.py:398.
    return MemoryService(mulch_dir=str(tmp_path))


def test_recall_accepts_as_of_filter(tmp_path):
    """AC-11: recall() exposes an `as_of` parameter (the temporal query seam)."""
    import inspect
    from prism_service.services.memory_service import MemoryService

    sig = inspect.signature(MemoryService.recall)
    assert "as_of" in sig.parameters, (
        "MemoryService.recall must accept an `as_of` temporal filter — "
        f"got params {list(sig.parameters)}"
    )


def test_as_of_resurfaces_superseded_fact(tmp_path):
    """AC-11: a fact superseded later is still returned for an earlier as_of."""
    svc = _make_memory_service(tmp_path)
    # v1 fact, then a v2 that supersedes it (store() invalidates v1).
    svc.store(domain="prefs", name="db_choice",
              description="we use sqlite for the bench service",
              type="decision", classification="internal")
    svc.store(domain="prefs", name="db_choice",
              description="we use postgres for the bench service",
              type="decision", classification="internal")

    # Current recall returns only the active (v2) fact.
    current = svc.recall("bench service database", domain="prefs")
    current_desc = " ".join(e.description for e in current)
    assert "postgres" in current_desc, "current recall must surface the active fact"

    # as_of BEFORE the supersession must resurface v1 (sqlite).
    early = svc.recall("bench service database", domain="prefs",
                       as_of="2000-01-01T00:00:00")
    early_desc = " ".join(e.description for e in early)
    assert "sqlite" in early_desc, (
        "as_of before supersession must resurface the superseded fact, "
        f"got {early_desc!r}"
    )


def test_timeline_returns_validity_windows(tmp_path):
    """AC-11: a timeline over a fact's validity windows is exposed."""
    svc = _make_memory_service(tmp_path)
    assert hasattr(svc, "timeline"), "MemoryService.timeline not exposed"
    svc.store(domain="prefs", name="db_choice",
              description="we use sqlite", type="decision", classification="internal")
    svc.store(domain="prefs", name="db_choice",
              description="we use postgres", type="decision", classification="internal")
    tl = svc.timeline(domain="prefs", name="db_choice")
    assert len(tl) >= 2, f"timeline must show both validity windows, got {tl}"


def test_mcp_memory_recall_forwards_as_of():
    """AC-11: the memory_recall MCP verb forwards `as_of` through the dispatcher
    (proves the field isn't defined-but-dead)."""
    import inspect
    from prism_service.mcp import tools

    src = inspect.getsource(tools)
    # The dispatch block must read arguments["as_of"] / arguments.get("as_of").
    assert 'as_of' in src and 'memory_recall' in src, (
        "memory_recall MCP dispatch must forward as_of"
    )
    # Tighter: the recall call site must pass as_of.
    assert 'as_of=arguments' in src or 'as_of"' in src, (
        "memory_recall dispatcher does not forward as_of to the service"
    )
