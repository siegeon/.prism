"""PLAT-0042 RED — Failing tests for query-decomposition wiring in Brain.search.

Verifies AC-2 (fusion via env-var gate) and AC-3 (byte-identical when off).
These tests must FAIL until T2 ships.

[Source: services/prism-service/app/engines/brain_engine.py::Brain.search line 2355]
[Source: docs/stories/PLAT-0042-retrieval-query-decomposition.story.md]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _make_brain(tmp_path):
    """Build a Brain instance against fresh DBs and seed a tiny corpus."""
    from app.engines.brain_engine import Brain

    brain = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    docs = [
        ("a.py::foo", "a.py", "py", "definition of foo handles authentication failure paths"),
        ("b.py::bar", "b.py", "py", "definition of bar runs the migration plan for tenants"),
        ("c.py::baz", "c.py", "py", "unrelated content about graph rendering performance"),
    ]
    for doc_id, src, dom, content in docs:
        brain._brain.execute(
            "INSERT INTO docs(id, source_file, domain, content) VALUES (?, ?, ?, ?)",
            (doc_id, src, dom, content),
        )
    brain._brain.commit()
    return brain


@pytest.fixture
def clean_env(monkeypatch):
    """Strip PRISM_* env vars that affect the search path."""
    for k in [
        "PRISM_QUERY_DECOMP", "PRISM_SEARCH_MODE", "PRISM_RERANK",
        "PRISM_FEEDBACK_WEIGHT", "PRISM_CHUNK_AGG",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRISM_FEEDBACK_WEIGHT", "off")


def test_ac2_decomp_on_unions_subquery_hits(tmp_path, monkeypatch, clean_env):
    """
    AC-2: Brain.search fuses sub-query candidate pools.
    Requirement: With PRISM_QUERY_DECOMP=on, sub-query hits are gathered
                 through the same per-index helpers and unioned before RRF.
    Expected: A compound query whose sub-queries each match a different
              doc returns BOTH docs in the top-K.
    """
    monkeypatch.setenv("PRISM_QUERY_DECOMP", "on")
    brain = _make_brain(tmp_path)
    q = "authentication failure and migration plan"
    results = brain.search(q, limit=5)
    doc_ids = {r["doc_id"] for r in results}
    assert "a.py::foo" in doc_ids and "b.py::bar" in doc_ids, (
        f"decomposition should rescue both sub-query hits, got {doc_ids}"
    )


def test_ac3_decomp_off_is_byte_identical_to_unset(tmp_path, monkeypatch, clean_env):
    """
    AC-3: Off-by-default and byte-identical when disabled.
    Requirement: PRISM_QUERY_DECOMP unset/off/0 must produce identical
                 results to the pre-change behavior.
    Expected: Output equality across unset, "off", and "0".
    """
    brain = _make_brain(tmp_path)
    q = "authentication failure and migration plan"

    monkeypatch.delenv("PRISM_QUERY_DECOMP", raising=False)
    baseline = brain.search(q, limit=5)

    monkeypatch.setenv("PRISM_QUERY_DECOMP", "off")
    off_run = brain.search(q, limit=5)

    monkeypatch.setenv("PRISM_QUERY_DECOMP", "0")
    zero_run = brain.search(q, limit=5)

    base_ids = [r["doc_id"] for r in baseline]
    off_ids = [r["doc_id"] for r in off_run]
    zero_ids = [r["doc_id"] for r in zero_run]
    assert base_ids == off_ids == zero_ids, (
        f"unset/off/0 must produce identical results: "
        f"unset={base_ids} off={off_ids} 0={zero_ids}"
    )


def test_ac3_baseline_fixture_locks_off_path(tmp_path, monkeypatch, clean_env):
    """
    AC-3: A committed fixture pins the off-path output to detect drift.
    Requirement: The baseline fixture file documents the expected
                 single-query behavior; if T2 accidentally changes the
                 off-path, this test fails loudly.
    """
    fixture = (
        Path(__file__).parent / "fixtures" / "plat_0042_search_baseline.json"
    )
    assert fixture.exists(), (
        f"missing baseline fixture {fixture} — T2 must commit a recorded "
        "snapshot of the off-path output before merging"
    )
    import json
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    brain = _make_brain(tmp_path)
    q = expected["query"]
    actual = [r["doc_id"] for r in brain.search(q, limit=expected["limit"])]
    assert actual == expected["doc_ids"], (
        f"off-path output drifted from baseline fixture: "
        f"expected={expected['doc_ids']} actual={actual}"
    )
