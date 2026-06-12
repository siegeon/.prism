"""RED scaffold — principles + ADR structure in memory (task 8579d49e).

Pins a1/a2 of 'Architecture governance enforced through conductor gates':

  * a1 — architecture principles (machine-checkable layer rules) are
    stored as MEMORY DATA, seeded via arc_governance.seed_prism_principles
    and read back via arc_governance.load_principles(memory_svc) — not
    static docs/architecture files;
  * a2 — decision memories carry ADR structure: adr_status + supersedes
    fields that round-trip through MemoryService persistence ACROSS
    SEPARATE instances (JSONL on disk, not one in-memory object) and are
    exposed on the MCP memory tool schemas.

FAILS today: arc_governance does not exist, ExpertiseEntry has no
adr_status/supersedes fields (models/memory.py:9-39), MemoryService.store
rejects the kwargs, and mcp/tools.py has no "adr_status" schema property.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _mem(tmp_path):
    from prism_service.services.memory_service import MemoryService
    return MemoryService(str(tmp_path / "mulch"))


# ── a1: principles seeded + loadable as memory data ────────────────────

def test_seed_prism_principles_then_load(tmp_path):
    from prism_service.services.arc_governance import (
        load_principles, seed_prism_principles)
    svc = _mem(tmp_path)
    seed_prism_principles(svc)
    rules = load_principles(svc)
    assert rules, "PRISM's own principles must be seeded as first data"
    layer_rules = [r for r in rules if r.get("kind") == "layer_rule"]
    assert layer_rules, "at least one machine-checkable layer rule expected"
    for r in layer_rules:
        assert r.get("from") and r.get("must_not_depend_on"), r


def test_load_principles_empty_store_returns_empty(tmp_path):
    from prism_service.services.arc_governance import load_principles
    assert load_principles(_mem(tmp_path)) == []


# ── a2: ADR structure on decision memories ─────────────────────────────

def test_decision_memory_carries_adr_status_and_supersedes(tmp_path):
    svc = _mem(tmp_path)
    old = svc.store(domain="architecture", name="ADR-1 layering v1",
                    description="domain may not import infrastructure",
                    type="decision", classification="foundational")
    new = svc.store(domain="architecture", name="ADR-2 layering v2",
                    description="supersedes ADR-1 with port adapters",
                    type="decision", classification="foundational",
                    adr_status="accepted", supersedes=old.id)
    assert new.adr_status == "accepted"
    assert new.supersedes == old.id


def test_adr_fields_survive_reload_across_instances(tmp_path):
    svc = _mem(tmp_path)
    entry = svc.store(domain="architecture", name="ADR-3 persisted",
                      description="adr fields must persist to JSONL",
                      type="decision", classification="foundational",
                      adr_status="proposed", supersedes="mx-000000")
    fresh = _mem(tmp_path)  # separate instance, same mulch dir
    got = fresh.get_entry(entry.id)
    assert got is not None
    assert got.adr_status == "proposed"
    assert got.supersedes == "mx-000000"


def test_mcp_memory_tools_expose_adr_fields():
    src = (_SERVICE_ROOT / "prism_service" / "mcp" / "tools.py").read_text(
        encoding="utf-8")
    assert '"adr_status"' in src, (
        "memory_store MCP schema must expose adr_status")
    assert '"supersedes"' in src, (
        "memory_store MCP schema must expose supersedes")
