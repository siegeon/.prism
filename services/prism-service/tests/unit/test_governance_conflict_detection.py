"""Governance conflict detection must not over-flag benign memories.

Regression guard for v6.3.37. The old heuristic split on whitespace and
required only 2 shared tokens *including filler* ('the', 'prism', 'memory'),
so any two same-domain entries where one merely contained 'not'/'avoid' got
flipped to needs_review — it had buried 334 unrelated memories. The rule now
compares substantive tokens (len>=4, not a stop/negation word) and requires a
higher overlap, so it flags genuine contradictions only.
"""

from __future__ import annotations

from prism_service.services.governance import GovernanceEngine
from prism_service.services.memory_service import MemoryService


def _mem(tmp_path) -> MemoryService:
    return MemoryService(str(tmp_path / "mulch"))


def _gov(mem) -> GovernanceEngine:
    # _detect_conflicts only touches the memory service.
    return GovernanceEngine(mem, None, None)


def test_benign_memories_with_shared_filler_not_flagged(tmp_path):
    mem = _mem(tmp_path)
    # Both mention common filler ('the','prism','version'/'dev'); one carries a
    # negation. Under the OLD rule this pair was flagged. They share no
    # substantive topic, so both must stay active.
    mem.store(
        "project", "always-bump-version",
        "Always bump the prism version on every commit to the footer.",
        type="convention", classification="tactical",
    )
    mem.store(
        "project", "avoid-docker-dev",
        "Do not use docker for dev; never run the prism build that way.",
        type="convention", classification="tactical",
    )
    gov = _gov(mem)
    flagged = gov._detect_conflicts()
    assert flagged == 0
    statuses = {
        e.name: e.status
        for e in mem.list_entries("project", status_filter="active")
    }
    assert statuses == {"always-bump-version": "active", "avoid-docker-dev": "active"}


def test_genuine_contradiction_is_flagged(tmp_path):
    mem = _mem(tmp_path)
    # Same substantive topic (squash/merge/branches/trunk/commits), opposite
    # directive — exactly what the rule should catch.
    mem.store(
        "workflow", "squash-policy",
        "Squash merge feature branches into trunk and collapse their commits into one.",
        type="convention", classification="tactical",
    )
    mem.store(
        "workflow", "keep-history-policy",
        "Avoid squash merge for branches; keep every commit on trunk, never collapse commits.",
        type="convention", classification="tactical",
    )
    active_before = mem.list_entries("workflow", status_filter="active")
    assert len(active_before) == 2, "test setup: both should persist as active"
    gov = _gov(mem)
    assert gov._detect_conflicts() >= 1
