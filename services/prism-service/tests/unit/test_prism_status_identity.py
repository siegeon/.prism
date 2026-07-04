"""RED tests for daemon-identity preflight (task 1e67253c).

This session's whole ordeal: the MCP tools were silently bound to a FORK
daemon (v8.3.6, 93 docs) while the conductor HTTP endpoint was the MAIN
store (v6.7.27). The reachability preflight passed (daemon answered 200)
but the drive would have mutated the wrong store. These tests pin the fix:

- sync_status exposes `data_dir` so the store answering is identifiable
  (AC-1) and distinguishes two stores (AC-2);
- implement.js (AC-3) and prototype.js (AC-4) preflights perform an
  MCP-vs-conductor version-identity match and halt on mismatch.

AC-1..AC-4 FAIL on main: sync_status has no `data_dir` key and neither
workflow references a prism_status/api-version identity check.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from prism_service.services.graph_service import GraphService

# tests/unit/<file> -> tests -> prism-service -> services -> repo root
_REPO = Path(__file__).resolve().parents[4]
_WORKFLOWS = _REPO / ".claude" / "workflows"


def _make_store(d: Path) -> GraphService:
    """A minimal on-disk project store GraphService can report on."""
    d.mkdir(parents=True, exist_ok=True)
    (d / "graphify-src").mkdir(exist_ok=True)
    brain = d / "brain.db"
    b = sqlite3.connect(brain)
    b.execute("CREATE TABLE IF NOT EXISTS docs "
              "(doc_id TEXT, source_file TEXT, content_hash TEXT)")
    b.commit()
    b.close()
    graph = d / "graph.db"
    return GraphService(project_data_dir=str(d), graph_db_path=str(graph))


# --- AC-1: sync_status exposes the store's data_dir -------------------------

def test_sync_status_exposes_data_dir(tmp_path):
    d = tmp_path / "proj"
    svc = _make_store(d)
    out = svc.sync_status(brain_db_path=str(d / "brain.db"))
    assert "data_dir" in out, "sync_status must expose data_dir for identity"
    assert Path(out["data_dir"]) == d, (
        f"data_dir should be the project dir {d}, got {out.get('data_dir')}"
    )


# --- AC-2: data_dir distinguishes two different stores ----------------------

def test_data_dir_distinguishes_two_stores(tmp_path):
    a = _make_store(tmp_path / "main")
    b = _make_store(tmp_path / "fork")
    da = a.sync_status(brain_db_path=str(tmp_path / "main" / "brain.db"))["data_dir"]
    db = b.sync_status(brain_db_path=str(tmp_path / "fork" / "brain.db"))["data_dir"]
    assert da != db, "a fork store and the main store must be distinguishable by data_dir"


# --- AC-3: implement.js preflight does an MCP-vs-conductor identity match ----

def test_implement_preflight_has_identity_match():
    src = (_WORKFLOWS / "implement.js").read_text(encoding="utf-8")
    assert "prism_status" in src, "preflight must query prism_status (the MCP daemon)"
    assert "api/version" in src, "preflight must query /api/version (the conductor daemon)"
    assert "identity_ok" in src, "preflight must gate on an identity_ok flag"
    # the flag must actually be folded into the ok= decision, not dangling.
    ok_line = next((ln for ln in src.splitlines()
                    if "ok=true ONLY if" in ln or "ok = true only if" in ln.lower()), "")
    assert "identity_ok" in ok_line, (
        "identity_ok must be part of the ok= gate expression, not merely mentioned"
    )


# --- AC-4: prototype.js carries the same identity guard ---------------------

def test_prototype_has_identity_guard():
    src = (_WORKFLOWS / "prototype.js").read_text(encoding="utf-8")
    assert "prism_status" in src, "prototype preflight must query prism_status"
    assert "api/version" in src, "prototype preflight must query /api/version"
    assert "identity" in src.lower(), (
        "prototype must perform an MCP-vs-conductor identity/version match"
    )


# --- port-safety: the shipped workflows must not bake in DEV ports -----------
# These scripts ship to every customer, whose canonical daemon is 7778 (web) /
# 7777 (MCP). The dev instance runs 8888/8887; those must never be a shipped
# default or appear in agent-executed instructions — only, at most, in a
# maintainer comment documenting the api_base override.

@pytest.mark.parametrize("script", ["implement.js", "prototype.js"])
def test_workflows_do_not_ship_dev_ports(script):
    lines = (_WORKFLOWS / script).read_text(encoding="utf-8").splitlines()
    code = [ln for ln in lines if not ln.lstrip().startswith("//")]
    offenders = [ln.strip() for ln in code
                 if ":8888" in ln or ":8887" in ln or ":9998" in ln or ":9999" in ln]
    assert not offenders, (
        f"{script} ships DEV ports in executable/agent-facing code (customers "
        f"run 7778/7777): {offenders}"
    )
    # The canonical default must be the release web port.
    assert "127.0.0.1:7778" in "\n".join(code), (
        f"{script} must default the conductor probe to the canonical 7778"
    )
