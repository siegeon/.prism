"""Integration-level write-side tests for the Ultimate Graph annotation
PULL loop (siegeon/.prism#50, task f7763ac8 write-side).

These pin the REAL SEAM, not the service class in isolation:
  1. graph_annotate_* verbs reachable THROUGH the MCP tool dispatcher
     (handle_tool -> _dispatch_tool), exactly as the MCP server invokes them.
  2. The queue is DURABLE in graph.db — a brief enqueued via one
     GraphAnnotateService survives into a SEPARATE instance over the same db.
  3. Idempotency — enqueuing the same (scope_kind, scope_id, input_hash)
     twice yields ONE dispensable job; escape-when-unchanged still holds.
  4. The SessionStart hook path injects a graph annotation brief as
     additionalContext when one is queued (same hook function that injects
     the janitor reflection brief).

They FAIL before the write-side is wired (no verbs, in-memory queue, no
ProjectContext property, no hook multiplex).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


_PID = "test-graph-annotate"

_SCOPES = [
    {"scope_id": "svc/a", "level": 1, "files": ["a.py", "b.py"],
     "symbols": ["Foo", "bar"], "input_hash": "hash-a"},
    {"scope_id": "svc/c", "level": 1, "files": ["c.py", "d.py"],
     "symbols": ["Baz"], "input_hash": "hash-c"},
]


def _isolated_project(tmp_path, pid=_PID):
    from prism_service import config as cfg
    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # get_project no longer creates on miss (d37193da):
    # seed the sandboxed project dir explicitly.
    cfg.project_data_dir(pid)
    from prism_service import project_context as pc
    pc._contexts.clear()
    yield pid
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


@pytest.fixture
def project(tmp_path):
    yield from _isolated_project(tmp_path)


def _call(tool_name, arguments=None, project_id=_PID):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id)
    )


def _text(result):
    assert len(result) == 1
    return result[0].text


def _graph(tmp_path):
    from prism_service.services.graph_service import GraphService
    return GraphService(str(tmp_path), str(tmp_path / "graph.db"))


# ----------------------------------------------------------------------
# Seam 1 — verbs reachable THROUGH the dispatcher
# ----------------------------------------------------------------------

def test_graph_annotate_verbs_registered():
    from prism_service.mcp.tools import TOOLS
    names = {t.name for t in TOOLS}
    for n in (
        "graph_annotate_enqueue", "graph_annotate_check",
        "graph_annotate_submit", "graph_annotate_abandon",
        "graph_annotate_status",
    ):
        assert n in names, f"{n} not in TOOLS"


def test_graph_annotate_check_round_trip_through_dispatcher(project, monkeypatch):
    """enqueue via MCP, then check via MCP dispenses ONE brief whose
    response_schema is constrained to {name, purpose}."""
    from prism_service.services import graph_enrich
    monkeypatch.setattr(graph_enrich, "hierarchy_scopes",
                        lambda project, **k: list(_SCOPES))
    monkeypatch.setattr(graph_enrich, "community_scopes",
                        lambda project, **k: [])

    enq = json.loads(_text(_call("graph_annotate_enqueue", {"project": _PID})))
    assert enq.get("enqueued", 0) >= 1

    chk = json.loads(_text(_call("graph_annotate_check", {"session_id": "S-1"})))
    assert chk["ready"] is True
    brief = chk["brief"]
    assert set(brief["response_schema"]) == {"name", "purpose"}
    assert brief["scope_id"] in {"svc/a", "svc/c"}
    assert isinstance(brief.get("prompt"), str) and brief["prompt"]


def test_graph_annotate_submit_round_trip_persists(project, monkeypatch):
    from prism_service.services import graph_enrich
    from prism_service.project_context import get_project
    monkeypatch.setattr(graph_enrich, "hierarchy_scopes",
                        lambda project, **k: list(_SCOPES[:1]))
    monkeypatch.setattr(graph_enrich, "community_scopes",
                        lambda project, **k: [])
    _call("graph_annotate_enqueue", {"project": _PID})
    chk = json.loads(_text(_call("graph_annotate_check", {"session_id": "S-1"})))
    bid = chk["brief"]["brief_id"]

    res = json.loads(_text(_call("graph_annotate_submit", {
        "brief_id": bid,
        "output": {"name": "Service A", "purpose": "Does A things."},
    })))
    assert res["accepted"] is True

    ctx = get_project(project)
    stored = ctx.graph_svc.annotations_for("hierarchy")
    assert stored["svc/a"]["name"] == "Service A"
    assert stored["svc/a"]["provenance"].startswith("claude @ ")


# ----------------------------------------------------------------------
# Seam 2 — DURABILITY: queue survives across instances over same graph.db
# ----------------------------------------------------------------------

def test_queue_is_durable_across_instances(tmp_path):
    """A brief enqueued via one GraphAnnotateService is dispensable from a
    SEPARATE instance backed by the same graph.db — proves not in-memory."""
    from prism_service.services import graph_annotate as ga
    graph_a = _graph(tmp_path)
    svc_a = ga.GraphAnnotateService(graph_a)
    n = svc_a.enqueue("hierarchy", list(_SCOPES))
    assert n == 2

    # Brand-new service over the SAME db file — no shared Python state.
    graph_b = _graph(tmp_path)
    svc_b = ga.GraphAnnotateService(graph_b)
    chk = svc_b.check("sess-durable")
    assert chk["ready"] is True, "queue must persist in graph.db, not memory"
    assert chk["brief"]["scope_id"] in {"svc/a", "svc/c"}


# ----------------------------------------------------------------------
# Seam 3 — IDEMPOTENCY + escape-when-unchanged
# ----------------------------------------------------------------------

def test_enqueue_idempotent_on_scope_input_hash(tmp_path):
    """Enqueuing the same (scope_kind, scope_id, input_hash) twice does not
    create a duplicate dispensable job (UNIQUE constraint)."""
    from prism_service.services import graph_annotate as ga
    graph = _graph(tmp_path)
    svc = ga.GraphAnnotateService(graph)
    svc.enqueue("hierarchy", list(_SCOPES))
    svc.enqueue("hierarchy", list(_SCOPES))  # second sweep, same inputs

    # Exactly two distinct dispensable jobs, not four.
    seen = set()
    for _ in range(6):
        chk = svc.check(f"s-{len(seen)}")
        if not chk["ready"]:
            break
        seen.add(chk["brief"]["scope_id"])
    assert seen == {"svc/a", "svc/c"}


def test_enqueue_escapes_unchanged_scopes(tmp_path):
    """A scope whose stored annotation input_hash matches the live hash is
    not enqueued (escape-when-unchanged)."""
    from prism_service.services import graph_annotate as ga
    graph = _graph(tmp_path)
    graph.upsert_annotation("hierarchy", "svc/a", "name",
                            "Service A", "Does A things.", "hash-a",
                            "claude @ 2026-05-29")
    svc = ga.GraphAnnotateService(graph)
    n = svc.enqueue("hierarchy", list(_SCOPES))
    assert n == 1, "only the CHANGED scope (svc/c) should be enqueued"
    chk = svc.check("s-1")
    assert chk["brief"]["scope_id"] == "svc/c"


# ----------------------------------------------------------------------
# Seam 4 — SessionStart hook injects a graph-annotate brief
# ----------------------------------------------------------------------

def _load_sync_hook():
    """Import the DEPLOYED .prism/hooks/prism-sync.py as a module so we
    exercise the same main() that runs at SessionStart."""
    import importlib.util
    hook_path = (_SERVICE_ROOT.parent.parent / ".prism" / "hooks"
                 / "prism-sync.py")
    spec = importlib.util.spec_from_file_location("prism_sync_hook", hook_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sessionstart_hook_injects_graph_annotate_brief(monkeypatch, capsys, tmp_path):
    """When a graph annotation brief is queued, the SessionStart hook
    emits it as additionalContext, clearly labeled [prism-graph-annotate].

    Drives the REAL main() to the SessionStart block: stub the MCP target +
    file collection, make prism_status report drift (so flow reaches the
    session check), then return a ready graph_annotate brief."""
    import io
    from pathlib import Path as _P
    mod = _load_sync_hook()

    fake_file = tmp_path / "x.py"
    fake_file.write_text("print(1)\n", encoding="utf-8")

    annotate_brief = {
        "brief_id": "bid-xyz", "scope_kind": "hierarchy", "scope_id": "svc/a",
        "prompt": "Name this scope.",
        "response_schema": {"name": "...", "purpose": "..."},
    }

    def fake_mcp_call(base, project, tool, args):
        if tool == "prism_status":
            return {"prism_version": "test", "drifted": [{"path": "x.py"}]}
        if tool == "graph_annotate_check":
            return {"ready": True, "brief": annotate_brief}
        if tool == "janitor_check":
            return {"ready": False, "brief": None}
        return {}

    monkeypatch.setattr(mod, "_mcp_url_and_project",
                        lambda root: ("http://localhost:0", "prism"))
    monkeypatch.setattr(mod, "_collect",
                        lambda root: {"x.py": ("sha", fake_file)})
    monkeypatch.setattr(mod, "_mcp_call", fake_mcp_call)
    monkeypatch.setattr(mod, "_parse_result", lambda resp: resp)
    monkeypatch.setattr(mod, "_project_root", lambda: _P(str(tmp_path)))
    monkeypatch.setattr("sys.stdin",
                        io.StringIO(json.dumps({"session_id": "S-HOOK"})))

    rc = mod.main()
    assert rc == 0  # advisory, never blocks
    out = capsys.readouterr().out
    assert "additionalContext" in out
    assert "[prism-graph-annotate]" in out
    assert "bid-xyz" in out
