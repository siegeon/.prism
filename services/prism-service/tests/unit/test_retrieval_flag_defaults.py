"""The retrieval flags that used to ship disabled (task 19e4e7f7).

Owner doctrine mx-71dc57: a capability that ships disabled by default is not
shipped. Every user runs the defaults, so the default IS the product. A flag
may exist so a user can turn something OFF; it must never exist so a user has
to discover an environment variable to turn something ON.

PRISM_RERANK was the clearest violation in the codebase and had never been
measured at all. It has now been, on two independent corpora built from real
git history, and it is the largest retrieval win available:

    PocketBase     r@5 0.5217 -> 0.6275  (+0.1058)  McNemar p=0.0075
    FullStackHero  r@5 0.4874 -> 0.6401  (+0.1527)  McNemar p=0.0009

So the default moves from "off" to "auto": on wherever it can actually run,
off where it cannot, never a capability gated behind knowing a variable name.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _make_brain(tmp_path, n_docs: int = 40):
    """A corpus big enough that a rerank pool can be smaller than it."""
    from prism_service.engines.brain_engine import Brain

    brain = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    for i in range(n_docs):
        brain._brain.execute(
            "INSERT INTO docs(id, source_file, domain, content) "
            "VALUES (?, ?, ?, ?)",
            (f"f{i}.py::sym", f"f{i}.py", "py",
             f"module {i} handles authentication failure paths and retries"),
        )
    brain._brain.commit()
    return brain


@pytest.fixture
def clean_env(monkeypatch):
    for k in ("PRISM_SEARCH_MODE", "PRISM_RERANK",
              "PRISM_RERANK_TOPN", "PRISM_CHUNK_AGG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRISM_FEEDBACK_WEIGHT", "off")


# ── AC-1: the default is "auto", never "off" ──────────────────────────────
def test_ac1_rerank_defaults_to_auto_not_off():
    """Read the DEFAULT out of the search path itself.

    A comment claiming the default changed is not the default changing, and
    this exact flag spent its whole life defaulting to a string that meant
    "do nothing".
    """
    source = Path(
        _SERVICE_ROOT / "prism_service" / "engines" / "brain_engine.py"
    ).read_text(encoding="utf-8")
    assert 'environ.get("PRISM_RERANK", "auto")' in source
    assert 'environ.get("PRISM_RERANK", "off")' not in source


def test_ac1b_auto_is_on_where_it_can_run_and_off_where_it_cannot(monkeypatch):
    """auto must resolve by CAPABILITY, not by wishful thinking.

    The default install has no sentence-transformers on purpose (torch brings
    a second OpenMP runtime, GH #162), so auto has to degrade to off there --
    otherwise the new default is a warning on every search.
    """
    import importlib.util as ilu

    from prism_service.engines import brain_engine as be

    real = ilu.find_spec

    monkeypatch.setattr(be, "_AUTO_RERANK_PRESET", None, raising=False)
    monkeypatch.setattr(
        ilu, "find_spec",
        lambda name, *a, **k: None if name == "sentence_transformers"
        else real(name, *a, **k))
    assert be._auto_rerank_preset() == "off"

    monkeypatch.setattr(be, "_AUTO_RERANK_PRESET", None, raising=False)
    monkeypatch.setattr(
        ilu, "find_spec",
        lambda name, *a, **k: (object() if name == "sentence_transformers"
                               else real(name, *a, **k)))
    assert be._auto_rerank_preset() == be._AUTO_RERANK_MODEL


# ── AC-2: PRISM_RERANK_TOPN is a CAP, not a floor ─────────────────────────
def test_ac2_topn_caps_the_pool_instead_of_being_raised_to_inner(
        tmp_path, monkeypatch, clean_env):
    """The one knob on the most expensive step in search used to do nothing.

    ``pool_n = max(inner, pool_n)`` raised any requested cap up to limit*6, so
    asking for 8 reranked 120 at limit=20. Cost is linear in the pool, so an
    un-capped rerank is exactly what made "on by default" indefensible.
    """
    from prism_service.engines.brain_engine import Brain

    brain = _make_brain(tmp_path)
    seen: list[int] = []

    def _spy(self, query, candidates, preset):
        seen.append(len(candidates))
        return None  # fall back to RRF order; we only care about the pool

    monkeypatch.setattr(Brain, "_rerank_candidates", _spy)
    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "8")

    brain.search("authentication failure", limit=20)

    assert seen, "the reranker was never reached"
    assert max(seen) <= 8, (
        f"asked to rerank 8 candidates, was handed {max(seen)}")


def test_ac2b_topn_never_exceeds_the_candidates_that_exist(
        tmp_path, monkeypatch, clean_env):
    """A cap larger than the pool must not pad, slice oddly, or crash."""
    from prism_service.engines.brain_engine import Brain

    brain = _make_brain(tmp_path, n_docs=5)
    seen: list[int] = []
    monkeypatch.setattr(
        Brain, "_rerank_candidates",
        lambda self, q, c, p: (seen.append(len(c)), None)[1])
    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "500")

    results = brain.search("authentication failure", limit=10)

    assert seen and max(seen) <= 5
    assert isinstance(results, list)


# ── AC-4: query decomposition is gone, not merely switched off ────────────
def test_ac4_query_decomposition_is_deleted_not_left_off():
    """"Staying off" was not an allowed outcome for this flag.

    PRISM_QUERY_DECOMP was measured on three independent corpora and lost or
    tied on every one (PocketBase n=115 r@5 -0.0014 p=1.0; FullStackHero n=119
    +0.0042 p=1.0; LongMemEval n=120 -0.0167 p=0.7266), so the code path was
    removed rather than left as a switch nobody should flip. A flag that
    survives "off forever" is the exact thing the rule forbids, and a deleted
    module that something still imports is a crash waiting for a caller.
    """
    import importlib.util as ilu

    assert ilu.find_spec("prism_service.engines.query_decomposer") is None, (
        "query_decomposer still importable; the flag was switched off rather "
        "than the code path removed")

    source = Path(
        _SERVICE_ROOT / "prism_service" / "engines" / "brain_engine.py"
    ).read_text(encoding="utf-8")
    live = "\n".join(line for line in source.splitlines()
                     if not line.lstrip().startswith("#"))
    for gone in ("decompose_query", "_union_by_best_rank", "sub_queries",
                 'environ.get("PRISM_QUERY_DECOMP"'):
        assert gone not in live, f"decomposition leftover in search path: {gone}"


def test_ac4b_search_still_returns_results_after_the_removal(
        tmp_path, clean_env):
    """The single-query path is now the ONLY path — prove it still searches."""
    brain = _make_brain(tmp_path)
    hits = brain.search("authentication failure", limit=5)
    assert hits, "search returned nothing after the decomposition removal"
    assert all("doc_id" in h for h in hits)


# ── AC-3: off still means off ─────────────────────────────────────────────
def test_ac3_explicit_off_never_touches_the_reranker(
        tmp_path, monkeypatch, clean_env):
    """The flag keeps existing for exactly the reason a flag may exist: so a
    user can turn the thing OFF."""
    from prism_service.engines.brain_engine import Brain

    brain = _make_brain(tmp_path)
    called: list[int] = []
    monkeypatch.setattr(
        Brain, "_rerank_candidates",
        lambda self, q, c, p: (called.append(1), None)[1])
    monkeypatch.setenv("PRISM_RERANK", "off")

    brain.search("authentication failure", limit=20)

    assert called == []
