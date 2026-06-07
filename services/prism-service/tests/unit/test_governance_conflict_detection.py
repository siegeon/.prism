"""Governance conflict detection must never auto-bury memories.

Regression guard for v6.3.36-38. The keyword heuristic (one same-domain entry
carries a negation word + shared tokens) produced only false positives at
scale — on the real store it flipped ~390 valid memories to needs_review and
reported ~138 'conflicts' per run (1499 candidate pairs over 206 entries),
because dense technical memories trivially share terms and 'never/avoid/not'
is common in legitimate guidance. Keyword overlap cannot model contradiction,
so the rule was disabled. It must now no-op: return 0 and mutate nothing, even
for a blatant same-topic opposite-directive pair.
"""

from __future__ import annotations

from prism_service.services.governance import GovernanceEngine
from prism_service.services.memory_service import MemoryService


def _mem(tmp_path) -> MemoryService:
    return MemoryService(str(tmp_path / "mulch"))


def _gov(mem) -> GovernanceEngine:
    return GovernanceEngine(mem, None, None)


def test_detector_is_a_noop_returns_zero(tmp_path):
    mem = _mem(tmp_path)
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
    assert _gov(mem)._detect_conflicts() == 0


def test_detector_never_mutates_status_even_on_real_contradiction(tmp_path):
    mem = _mem(tmp_path)
    # Same topic, opposite directive — the kind of pair the old rule chased.
    # The detector must NOT touch status (no auto-burying); curation of true
    # contradictions is handled by supersession + verify_staleness instead.
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
    gov = _gov(mem)
    assert gov._detect_conflicts() == 0
    statuses = {e.name: e.status for e in mem.list_entries("workflow")}
    assert statuses == {"squash-policy": "active", "keep-history-policy": "active"}
