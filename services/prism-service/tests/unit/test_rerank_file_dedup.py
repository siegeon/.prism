"""Rerank the FILES, not the same file five times (task 61666a4f).

Multi-granular chunking (mx-302e5e) gives one source file many docs -- a
``path::__file__`` coarse chunk, ``path::win_N`` sliding windows, and a chunk
per function. The cross-encoder is the single most expensive step in search
and it currently pays PER CHUNK, while the result set is collapsed PER FILE
immediately afterwards (``seen_files`` at brain_engine.py:2979-2989). So the
same file is scored three, four, five times for one row of output.

These tests pin the collapse at the ONE place the slice is allowed to live:
the rerank pool construction inside ``Brain.search``
(brain_engine.py:2917-2933). The observation point is the pair list actually
handed to ``reranker.predict`` (brain_engine.py:3240-3251), read off a
counting stub cross-encoder -- never wall-clock, never a mocked search.

WHY A COUNTING STUB AND NOT A SPY ON ``_rerank_candidates``: the neighbouring
suite (tests/unit/test_retrieval_flag_defaults.py:104-148) already spies on
``_rerank_candidates`` to size the pool. A spy there cannot see how many PAIRS
the model was asked to score, which is the cost this task is about, so the
stub goes one level deeper and ``Brain.search`` stays real end to end.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

QUERY = "authentication failure retries"

# 6 files x 4 chunks. Insertion order per file is __file__, win_1, win_2,
# zfunc_best -- so the LAST-inserted chunk also carries the lexicographically
# LARGEST chunk id, and the fixture makes that same chunk the best-ranked one.
# Any pick by document order or by chunk id therefore lands on __file__ and
# the by-rank assertion below fails. That is the whole point of the layout.
_FILES = [f"mod{i}.py" for i in range(6)]
_CHUNKS = ["__file__", "win_1", "win_2", "zfunc_best"]
_FILLER = " ".join(f"padding{i}" for i in range(60))


def _chunk_content(source_file: str, chunk: str) -> str:
    """Term frequency, not filler, decides rank -- and it is deterministic.

    ``mod0.py`` is the HOT file: all four of its chunks outrank every other
    file's, so the head of the fused list is four chunks of ONE file. That is
    what makes "collapse before the cap" observable (AC-1): capping first
    would hand the reranker four chunks of a single file.
    """
    hot = source_file == _FILES[0]
    reps = (4 if chunk == "zfunc_best" else 3) if hot else (
        2 if chunk == "zfunc_best" else 1)
    body = " ".join([QUERY] * reps)
    content = f"{body} {source_file} {chunk}"
    if not hot and chunk != "zfunc_best":
        content += " " + _FILLER
    return content


def _make_multichunk_brain(tmp_path):
    """A REAL Brain over tmp sqlite, several chunks per source_file (R9).

    Same shape as the ``_make_brain`` helper in
    tests/unit/test_retrieval_flag_defaults.py:32-49 (direct INSERT into
    ``docs``; the docs_fts triggers at brain_engine.py:902-906 index it), only
    with many doc ids sharing one source_file so a collapse is observable.
    """
    from prism_service.engines.brain_engine import Brain

    brain = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    for source_file in _FILES:
        for chunk in _CHUNKS:
            brain._brain.execute(
                "INSERT INTO docs(id, source_file, domain, content) "
                "VALUES (?, ?, ?, ?)",
                (f"{source_file}::{chunk}", source_file, "py",
                 _chunk_content(source_file, chunk)),
            )
    brain._brain.commit()
    return brain


def _doc_id_by_content() -> dict[str, str]:
    """Map the text handed to the cross-encoder back to its doc id.

    ``_rerank_candidates`` builds ``(query, content[:2048])`` pairs
    (brain_engine.py:3246), so the pair list identifies documents by CONTENT.
    Every fixture chunk's content is unique, so the mapping is exact.
    """
    return {
        _chunk_content(f, c): f"{f}::{c}"
        for f in _FILES for c in _CHUNKS
    }


def _rrf_order(brain, query: str, inner: int) -> list[str]:
    """The RRF RANK order, computed exactly as Brain.search computes it.

    Mirrors brain_engine.py:2898-2904 leg for leg, so "rank" in these tests
    means position in ``fused`` -- the (-score, doc_id) ordering produced by
    reciprocal_rank_fusion at brain_engine.py:634 -- and nothing else.
    """
    from prism_service.engines.brain_engine import reciprocal_rank_fusion

    bm25 = brain._fts5_search(query, None, inner)
    graph = brain._graph_search(query, inner)
    if brain.vector_enabled:
        vec = brain._vector_search(query, None, inner)
        lists = [bm25, vec, graph]
    else:
        lists = [bm25, graph]
    return [c["doc_id"] for c in reciprocal_rank_fusion(lists)]


def _best_ranked_per_file(order: list[str]) -> dict[str, str]:
    """First appearance in RRF rank order == that file's representative."""
    best: dict[str, str] = {}
    for doc_id in order:
        best.setdefault(doc_id.split("::")[0], doc_id)
    return best


class _CountingCrossEncoder:
    """Stands in for the CrossEncoder and RECORDS what it was asked to score.

    Scores descend with input position, so the rerank is an IDENTITY on the
    order it is given (``scored.sort(key=lambda x: -x["rerank_score"])`` at
    brain_engine.py:3260). That makes any change in the returned results
    attributable to the pool construction, not to the model.
    """

    def __init__(self) -> None:
        self.pair_batches: list[list[tuple[str, str]]] = []

    @property
    def pairs(self) -> list[tuple[str, str]]:
        return [p for batch in self.pair_batches for p in batch]

    def predict(self, pairs):
        self.pair_batches.append(list(pairs))
        return [float(len(pairs) - i) for i in range(len(pairs))]


@pytest.fixture
def counting_reranker(monkeypatch):
    """Install the stub at ``_load_reranker`` so Brain.search stays REAL.

    ``_rerank_candidates`` resolves the model through the module-level
    ``_load_reranker`` (brain_engine.py:3231), so patching it there leaves
    every line of the search path -- fusion, pool construction, the pair
    build, the splice -- running for real, and never downloads a model.
    """
    from prism_service.engines import brain_engine as be

    stub = _CountingCrossEncoder()
    monkeypatch.setattr(be, "_load_reranker", lambda preset: stub)
    return stub


@pytest.fixture
def clean_env(monkeypatch):
    """Deterministic and cheap (R10): every retrieval flag pinned explicitly.

    PRISM_FEEDBACK_WEIGHT=off keeps the feedback re-sort at
    brain_engine.py:2946-2960 from perturbing RRF order.
    """
    for k in ("PRISM_SEARCH_MODE", "PRISM_RERANK",
              "PRISM_RERANK_TOPN", "PRISM_CHUNK_AGG"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRISM_FEEDBACK_WEIGHT", "off")


# ── AC-0: the fixture can actually tell the three picks apart ─────────────
def test_ac0_fixture_separates_rank_from_document_order_and_chunk_id(
        tmp_path, clean_env):
    """An integrity check on the corpus, not on the change.

    Every by-rank assertion below is worthless if a file's best-ranked chunk
    happens to also be its first-inserted chunk or its lowest chunk id. This
    test fails loudly the day a content tweak makes them coincide, instead of
    letting the suite go quietly weak.
    """
    brain = _make_multichunk_brain(tmp_path)
    order = _rrf_order(brain, QUERY, inner=30)
    assert len(order) == len(_FILES) * len(_CHUNKS), (
        f"expected every chunk in the fused list, got {len(order)}")

    best = _best_ranked_per_file(order)
    assert set(best) == set(_FILES)
    for source_file, rep in best.items():
        chunks = [f"{source_file}::{c}" for c in _CHUNKS]
        assert rep != chunks[0], (
            f"{source_file}: best-ranked chunk is also the first-inserted one")
        assert rep != min(chunks), (
            f"{source_file}: best-ranked chunk is also the lowest chunk id")

    # The head of the fused list is four chunks of ONE file -- this is what
    # makes "collapse BEFORE the cap" (AC-1) an observable difference.
    assert len({d.split("::")[0] for d in order[:4]}) == 1


# ── AC-1: the collapse happens BEFORE PRISM_RERANK_TOPN is applied ────────
def test_ac1_collapse_runs_before_the_topn_cap(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """A cap over chunks spends the whole budget on one file.

    The fused head is four chunks of mod0.py, so with a cap of 4 the ORDER of
    the two operations is directly visible: collapse-then-cap scores four
    DIFFERENT files; cap-then-collapse scores mod0.py and stops. The point of
    the ticket is the second one never happens again.
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "4")
    monkeypatch.setenv("PRISM_CHUNK_AGG", "on")

    brain.search(QUERY, limit=5)

    by_content = _doc_id_by_content()
    scored = [by_content[text] for _q, text in counting_reranker.pairs]
    assert scored, "the reranker was never handed a pair"
    files = [d.split("::")[0] for d in scored]
    assert len(files) == 4, (
        f"cap of 4 should mean 4 pairs, got {len(files)}: {scored}")
    assert len(set(files)) == 4, (
        "the cap was spent on chunks of one file -- the collapse ran AFTER "
        f"the cap, or not at all: {scored}")


# ── AC-2: exactly one pair per distinct source_file ───────────────────────
def test_ac2_cross_encoder_scores_each_file_exactly_once(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """The claim of the ticket, read off the pair list itself.

    24 chunks, 6 files, a cap wide enough for all of them: the model must be
    asked 6 questions, not 24, and no file may appear twice.
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "50")
    monkeypatch.setenv("PRISM_CHUNK_AGG", "on")

    brain.search(QUERY, limit=5)

    by_content = _doc_id_by_content()
    scored = [by_content[text] for _q, text in counting_reranker.pairs]
    files = [d.split("::")[0] for d in scored]
    assert sorted(files) == sorted(_FILES), (
        f"expected one pair per distinct source_file, got {files}")
    assert len(files) == len(set(files)), f"a file was scored twice: {files}"
    assert all(q == QUERY for q, _t in counting_reranker.pairs)


# ── AC-3: the representative is the file's BEST-RRF-RANKED chunk ──────────
def test_ac3_representative_is_the_best_ranked_chunk_by_rank(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """Asserted BY RANK (R4), never "some chunk of that file was used".

    The expected pick is derived from the RRF order the engine itself
    produces, so this fails the moment the pick silently becomes document
    order or chunk id -- see AC-0 for why those three differ here.
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "50")
    monkeypatch.setenv("PRISM_CHUNK_AGG", "on")

    expected = _best_ranked_per_file(_rrf_order(brain, QUERY, inner=30))
    brain.search(QUERY, limit=5)

    by_content = _doc_id_by_content()
    scored = [by_content[text] for _q, text in counting_reranker.pairs]
    got = {d.split("::")[0]: d for d in scored}
    assert got == expected, (
        f"representative per file is not the best-RRF-ranked chunk\n"
        f"  expected (by rank): {expected}\n  submitted:          {got}")


# ── AC-4: the saving is measured from the pair count, not a stopwatch ─────
def test_ac4_pairs_scored_are_at_least_2x_below_the_chunk_level_count(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """Cost is linear in the pair count, so the pair count is the metric.

    Wall-clock on one machine measures the machine. The old path would have
    scored every chunk in the capped pool; the new one scores the distinct
    files in it, and the ratio has to be at least 2x on this corpus.
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "50")
    monkeypatch.setenv("PRISM_CHUNK_AGG", "on")

    chunk_level_count = min(50, len(_rrf_order(brain, QUERY, inner=30)))
    brain.search(QUERY, limit=5)

    observed = len(counting_reranker.pairs)
    assert observed * 2 <= chunk_level_count, (
        f"scored {observed} pairs; the chunk-level path scored "
        f"{chunk_level_count} on the same pool -- not a 2x reduction")


# ── AC-5: the top-5 FILE set is unchanged ─────────────────────────────────
def test_ac5_top5_file_set_matches_the_chunk_level_path(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """Cheaper must not mean different.

    The stub scores descending with input position, so it is an IDENTITY on
    the order it is handed: on this corpus the chunk-level path therefore
    returns exactly the RRF order, which is what PRISM_RERANK=off returns.
    Any divergence here is the collapse changing results, not the model.
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_CHUNK_AGG", "on")

    monkeypatch.setenv("PRISM_RERANK", "off")
    baseline = [r["source_file"] for r in brain.search(QUERY, limit=5)]

    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "50")
    deduped = [r["source_file"] for r in brain.search(QUERY, limit=5)]

    assert counting_reranker.pairs, "the reranker was never reached"
    assert set(deduped) == set(baseline), (
        f"top-5 file set changed: {baseline} -> {deduped}")
    assert deduped == baseline, (
        f"top-5 file ORDER changed under an identity reranker: "
        f"{baseline} -> {deduped}")


# ── AC-6: the representatives are expanded back, coherently ───────────────
def test_ac6_non_representative_chunks_keep_their_rrf_position(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """Collapsing the POOL must not collapse the RESULTS.

    With chunk aggregation off the caller sees chunks, so the whole splice is
    visible: under an identity reranker every doc id must come back in its
    RRF position -- nothing dropped, nothing duplicated, and candidates past
    the cap still in RRF order (brain_engine.py:2933).
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_CHUNK_AGG", "off")

    monkeypatch.setenv("PRISM_RERANK", "off")
    baseline = [r["doc_id"] for r in brain.search(QUERY, limit=12)]

    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "8")
    deduped = [r["doc_id"] for r in brain.search(QUERY, limit=12)]

    assert counting_reranker.pairs, "the reranker was never reached"
    assert len(deduped) == len(set(deduped)), f"duplicate rows: {deduped}"
    assert deduped == baseline, (
        f"result order diverged from RRF under an identity reranker\n"
        f"  RRF:     {baseline}\n  deduped: {deduped}")


# ── AC-7: the collapse lives in the rerank block and NOWHERE else ─────────
def test_ac7_collapse_does_not_leak_into_the_non_rerank_path(
        tmp_path, monkeypatch, clean_env):
    """Off still means off, at the FILE level too.

    If the collapse were done in fusion, or before the
    ``rerank_preset not in ("", "off", "none")`` guard at
    brain_engine.py:2917, a chunk-level search with the reranker off would
    silently return one chunk per file. It must still return siblings.
    """
    brain = _make_multichunk_brain(tmp_path)
    monkeypatch.setenv("PRISM_RERANK", "off")
    monkeypatch.setenv("PRISM_CHUNK_AGG", "off")

    files = [r["source_file"] for r in brain.search(QUERY, limit=12)]

    assert len(files) > len(set(files)), (
        "no source_file appeared twice with rerank off and aggregation off -- "
        f"the collapse leaked outside the rerank pool construction: {files}")


# ── AC-8: through the surface a person actually reaches ───────────────────
def test_ac8_the_saving_holds_through_the_brain_search_verb(
        tmp_path, monkeypatch, clean_env, counting_reranker):
    """Assert the affordance, not the private method.

    The MCP verb ``brain_search`` -- declared at mcp/tools.py:24-26, in the
    DEFAULT interactive profile (tools.py:1631), dispatched at tools.py:3272
    -- calls ``BrainService.search`` (services/brain_service.py:89-110), which
    delegates to ``Brain.search``. A win that only exists one level below the
    dispatcher is a win nobody can reach, so drive it from here too.
    """
    from prism_service.services.brain_service import BrainService

    svc = BrainService(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    assert svc._available, "BrainService did not come up"
    for source_file in _FILES:
        for chunk in _CHUNKS:
            svc._brain._brain.execute(
                "INSERT INTO docs(id, source_file, domain, content) "
                "VALUES (?, ?, ?, ?)",
                (f"{source_file}::{chunk}", source_file, "py",
                 _chunk_content(source_file, chunk)),
            )
    svc._brain._brain.commit()

    monkeypatch.setenv("PRISM_RERANK", "ms-marco-minilm")
    monkeypatch.setenv("PRISM_RERANK_TOPN", "50")
    monkeypatch.setenv("PRISM_CHUNK_AGG", "on")

    results = svc.search(QUERY, limit=5)

    assert results, "the brain_search verb returned nothing"
    by_content = _doc_id_by_content()
    scored = [by_content[text] for _q, text in counting_reranker.pairs]
    files = [d.split("::")[0] for d in scored]
    assert sorted(files) == sorted(_FILES), (
        f"through brain_search the model still saw {len(files)} pairs "
        f"for {len(set(files))} files: {files}")
