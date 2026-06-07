"""RED — Failing tests for the ranking-stage temporal/recency boost (task e043f449).

Pins the vector-2 trial: an env-gated (PRISM_TEMPORAL_BOOST) ranking-stage
recency boost in Brain.search that rewards newer documents, evaluated on
LoCoMo's temporal split — WITHOUT regressing the determinism invariant.

CRITICAL contract pinned here (matching the existing experiment discipline,
cf. test_brain_search_decomp.py AC-3):
- OFF / unset / 0 must be byte-identical to the pre-change ranking (off-path
  determinism, provenance="deterministic" intact).
- ON must change ranking in favor of the newer doc when content is otherwise
  tied — proving the boost actually wires into the ranking stage (not dead).

Must FAIL until the temporal boost ships in Brain.search.

[Source: services/prism-service/prism_service/engines/brain_engine.py::Brain.search :2511]
[Source: services/prism-service/tests/unit/test_brain_search_decomp.py AC-3 :76,:104]
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _make_brain_with_dates(tmp_path):
    """Two docs with near-identical content but different created_at dates."""
    from prism_service.engines.brain_engine import Brain

    brain = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    # The OLD doc is the cleaner/stronger match (exact short content) so the
    # default ranking puts it first; the NEW doc is the recent-but-diluted
    # match. Only a recency boost can flip the newer doc to the top — this
    # makes the ON test genuinely depend on the boost, verified below: the
    # off-baseline ranks old.py first (see test docstring).
    docs = [
        ("old.py::topic", "old.py", "py",
         "migration plan discussed",
         "2020-01-01T00:00:00"),
        ("new.py::topic", "new.py", "py",
         "migration plan discussed but also lots of unrelated rendering graph "
         "performance text padding here to dilute",
         "2026-06-01T00:00:00"),
    ]
    for doc_id, src, dom, content, indexed in docs:
        brain._brain.execute(
            "INSERT INTO docs(id, source_file, domain, content, indexed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (doc_id, src, dom, content, indexed),
        )
    brain._brain.commit()
    return brain


@pytest.fixture
def clean_env(monkeypatch):
    for k in [
        "PRISM_QUERY_DECOMP", "PRISM_SEARCH_MODE", "PRISM_RERANK",
        "PRISM_FEEDBACK_WEIGHT", "PRISM_CHUNK_AGG", "PRISM_TEMPORAL_BOOST",
    ]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRISM_FEEDBACK_WEIGHT", "off")


def test_temporal_boost_off_is_byte_identical(tmp_path, monkeypatch, clean_env):
    """AC-8: OFF / unset / 0 produce byte-identical ranking (determinism)."""
    q = "migration plan discussed"

    brain1 = _make_brain_with_dates(tmp_path / "unset")
    monkeypatch.delenv("PRISM_TEMPORAL_BOOST", raising=False)
    unset_ids = [r["doc_id"] for r in brain1.search(q, limit=5)]

    brain2 = _make_brain_with_dates(tmp_path / "off")
    monkeypatch.setenv("PRISM_TEMPORAL_BOOST", "off")
    off_ids = [r["doc_id"] for r in brain2.search(q, limit=5)]

    brain3 = _make_brain_with_dates(tmp_path / "zero")
    monkeypatch.setenv("PRISM_TEMPORAL_BOOST", "0")
    zero_ids = [r["doc_id"] for r in brain3.search(q, limit=5)]

    assert unset_ids == off_ids == zero_ids, (
        f"off-path must be byte-identical: unset={unset_ids} off={off_ids} 0={zero_ids}"
    )


def test_temporal_boost_on_prefers_newer_doc(tmp_path, monkeypatch, clean_env):
    """vector-2: ON flips the newer doc above the older, proving the boost
    wires into the ranking stage (not dead code).

    Guard against a false-green: first confirm the OFF baseline ranks the
    older (cleaner-match) doc first, so the ON assertion can only pass if the
    boost actually reordered the list.
    """
    q = "migration plan discussed"

    brain_off = _make_brain_with_dates(tmp_path / "off")
    monkeypatch.setenv("PRISM_TEMPORAL_BOOST", "off")
    off_ranked = [r["doc_id"] for r in brain_off.search(q, limit=5)]
    assert off_ranked and off_ranked[0] == "old.py::topic", (
        f"precondition: off-baseline must rank old.py first, got {off_ranked}"
    )

    brain_on = _make_brain_with_dates(tmp_path / "on")
    monkeypatch.setenv("PRISM_TEMPORAL_BOOST", "on")
    on_ranked = [r["doc_id"] for r in brain_on.search(q, limit=5)]
    assert on_ranked and on_ranked[0] == "new.py::topic", (
        f"temporal boost must rank the newer doc first, got order {on_ranked}"
    )
