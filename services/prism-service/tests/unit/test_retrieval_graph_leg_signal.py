"""The graph leg of hybrid search must carry a real signal, or carry nothing.

Task 763ee039. Three facts measured in THIS worktree before a line was written:

* ``Brain._graph_search`` (brain_engine.py:2763-2786) emits
  ``{"doc_id": <entities.file>, "score": 1.0}`` -- a FILE PATH at a uniform
  score. ``reciprocal_rank_fusion`` keys on ``item["doc_id"]``
  (brain_engine.py:627), the other two legs return ``docs.id``, and the
  hydration pass runs ``if not row: continue`` (brain_engine.py:2981-2983).
  A file path is not a docs.id, so every graph hit is silently DISCARDED.
  Probed on a real Brain: the leg returned ``[{'doc_id': 'src/mod3.py',
  'score': 1.0}]`` and ``brain.search("QuarkResolver")`` returned ``[]``.
* ``Brain._traverse_graph`` (brain_engine.py:2812) carries the identical
  shape, so repairing one half leaves the other broken (memory mx-58339a).
* Naive id activation LOSES -- pooled McNemar 66/38 favouring the shipped
  arm, p=0.0078 over 739 cases (mx-58339a). The uniform 1.0 on a
  ``LIKE '%token%'`` name match emits a full ``limit``-length list that RRF
  treats as a third full-strength ranking. So "make the ids match" is NOT
  the ask: the leg must RANK (exact-over-substring, then entity centrality)
  and CAP, and the harness must be honest enough to tell a win from noise.

These pin the affordance a person uses, not the constant behind it: the
end-to-end assertions go through ``brain.search`` -- what the ``brain_search``
MCP verb (mcp/tools.py) and the Explore page (Sidebar.tsx:53 -> /brain ->
POST /api/brain/understand -> understand_view._focus) both land on.
"""

from __future__ import annotations

import importlib.util as _ilu
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_REPO_ROOT = _SERVICE_ROOT.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_ENGINE_SRC = _SERVICE_ROOT / "prism_service" / "engines" / "brain_engine.py"
_HARNESS_SRC = _REPO_ROOT / "benchmarks" / "graft_parity" / "ab_retrieval.py"


def _live(path: Path) -> str:
    """Source with comment-only lines stripped.

    A comment above the code has satisfied a source-reading assertion three
    separate times in this repo; a tombstone explaining what was removed must
    never be able to pass for the removal.
    """
    src = path.read_text(encoding="utf-8")
    return "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))


def _harness():
    """Import benchmarks/graft_parity/ab_retrieval.py by path (not a package)."""
    spec = _ilu.spec_from_file_location("ab_retrieval_under_test", _HARNESS_SRC)
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clean_env(monkeypatch):
    """No env var may be needed to reach the ranked leg (owner rule mx-71dc57)."""
    for k in ("PRISM_SEARCH_MODE", "PRISM_RERANK", "PRISM_RERANK_TOPN",
              "PRISM_CHUNK_AGG", "PRISM_MULTIGRAN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PRISM_FEEDBACK_WEIGHT", "off")


def _make_brain(tmp_path):
    from prism_service.engines.brain_engine import Brain

    return Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )


def _add_doc(brain, source_file: str, content: str) -> str:
    """One docs row. Its id is deliberately NOT the file path."""
    doc_id = f"{source_file}::chunk0"
    brain._brain.execute(
        "INSERT INTO docs(id, source_file, domain, content) VALUES (?,?,?,?)",
        (doc_id, source_file, "py", content),
    )
    brain._brain.commit()
    return doc_id


def _add_entity(brain, name: str, source_file: str, centrality: float = 0.0):
    brain._graph.execute(
        "INSERT INTO entities(name, kind, file, line, centrality) "
        "VALUES (?,?,?,?,?)",
        (name, "function", source_file, 1, centrality),
    )
    brain._graph.commit()


def _doc_ids(brain) -> set[str]:
    return {r["id"] for r in brain._brain.execute("SELECT id FROM docs")}


# ── AC-5: the graph leg reaches the person who searched ───────────────────
def test_ac5_a_graph_only_match_is_reachable_from_brain_search(
        tmp_path, clean_env):
    """THE affordance: ``brain.search`` is what ``brain_search`` and Explore call.

    ``QuarkResolver`` appears in the entity graph and in NO document body, so
    BM25 and the vector leg cannot find it. Only the graph leg knows the file.
    Today it emits the file PATH, hydration drops it at brain_engine.py:2982,
    and the person gets zero results for a symbol PRISM has indexed.
    """
    brain = _make_brain(tmp_path)
    for i in range(5):
        _add_doc(brain, f"src/other{i}.py", "unrelated helper for zebra paths")
    _add_doc(brain, "src/quark_resolver.py",
             "def _dispatch(payload): return payload  # no query token here")
    _add_entity(brain, "QuarkResolver", "src/quark_resolver.py", centrality=0.7)

    hits = brain.search("QuarkResolver", limit=10)

    assert hits, (
        "search returned NOTHING for a symbol that is in the entity graph -- "
        "the graph leg's file-path doc_id was discarded at hydration")
    assert any(h["source_file"] == "src/quark_resolver.py" for h in hits), (
        "the only file containing the searched entity never surfaced; got "
        f"{[h['source_file'] for h in hits]}")


def test_ac5_b_the_leg_emits_ids_the_hydrator_can_actually_resolve(tmp_path):
    """RRF fuses on ``doc_id`` and hydration looks that id up in ``docs``.

    An id that is not a ``docs.id`` is not a weak hit, it is a DELETED hit:
    brain_engine.py:2981-2983 drops it without a trace, which is why the leg
    has measured as inert (0/437 overlap, memory mx-60d462).
    """
    brain = _make_brain(tmp_path)
    _add_doc(brain, "src/quark_resolver.py", "body text")
    _add_entity(brain, "QuarkResolver", "src/quark_resolver.py", centrality=0.5)

    leg = brain._graph_search("QuarkResolver", 20)

    assert leg, "the graph leg found no entity it plainly has"
    known = _doc_ids(brain)
    unresolvable = [h["doc_id"] for h in leg if h["doc_id"] not in known]
    assert not unresolvable, (
        f"graph leg emitted ids no docs row can resolve: {unresolvable}; "
        f"docs ids are {sorted(known)}")


def test_ac5_c_the_leg_ranks_by_match_quality_then_centrality(tmp_path):
    """Uniform ``score: 1.0`` is WHY the naive id fix lost (mx-58339a).

    RRF ignores ``score`` entirely -- it adds a flat ``1.0/(k+rank)``
    (brain_engine.py:628) -- so the leg's ORDER is the whole signal. Rows are
    inserted here in exactly the wrong order, so an unranked leg (rowid order,
    no ORDER BY at brain_engine.py:2775) cannot pass by luck.
    """
    brain = _make_brain(tmp_path)
    slow = _add_doc(brain, "src/slow.py", "b")
    hub = _add_doc(brain, "src/hub.py", "b")
    exact = _add_doc(brain, "src/exact.py", "b")
    _add_entity(brain, "resolveEverythingSlowly", "src/slow.py", centrality=0.01)
    _add_entity(brain, "Resolver", "src/hub.py", centrality=0.90)
    _add_entity(brain, "resolve", "src/exact.py", centrality=0.20)

    order = [h["doc_id"] for h in brain._graph_search("resolve", 20)]

    assert order[:1] == [exact], (
        f"an EXACT entity-name match must outrank a substring one; got {order}")
    assert order.index(hub) < order.index(slow), (
        "among substring matches the higher-centrality entity must rank "
        f"first (centrality 0.90 vs 0.01); got {order}")


def test_ac5_d_the_leg_is_capped_far_below_the_limit_it_is_handed(tmp_path):
    """``search`` hands the leg ``inner = limit * 6`` (brain_engine.py:2868).

    At a user ``limit=20`` that is 120. A leg allowed to fill 120 slots on a
    ``LIKE '%token%'`` name match is a third full-strength ranking built from
    the weakest evidence in the system -- the mechanism behind the pooled
    p=0.0078 loss. The cap must be a small ABSOLUTE constant, not a fraction
    of a limit that is already multiplied by six.
    """
    from prism_service.engines import brain_engine as be

    brain = _make_brain(tmp_path)
    for i in range(40):
        _add_doc(brain, f"src/resolver_{i:02d}.py", "b")
        _add_entity(brain, f"resolveThing{i:02d}", f"src/resolver_{i:02d}.py",
                    centrality=i / 100.0)

    leg = brain._graph_search("resolve", 120)

    cap = getattr(be, "_GRAPH_LEG_CAP", None)
    assert cap is not None, (
        "no module-level _GRAPH_LEG_CAP in brain_engine -- the cap must be a "
        "named constant, not a magic number buried in the leg")
    assert cap <= 12, f"_GRAPH_LEG_CAP={cap} is not 'well below' limit*6"
    assert len(leg) <= cap, (
        f"leg emitted {len(leg)} hits from a 40-entity match; cap is {cap}")


# ── AC-6: the structural half is fixed in the SAME commit ─────────────────
def test_ac6_the_structural_branch_shares_the_id_space_and_the_cap(tmp_path):
    """``_traverse_graph`` carries the IDENTICAL bug at brain_engine.py:2812.

    It is reached from the same entry point (brain_engine.py:2766) whenever a
    query matches _STRUCTURAL_PATTERNS, so repairing only the token branch
    leaves half the leg emitting file paths at uniform score -- the half-fix
    memory mx-58339a warns about.
    """
    from prism_service.engines import brain_engine as be

    brain = _make_brain(tmp_path)
    _add_doc(brain, "src/quark_resolver.py", "b")
    _add_entity(brain, "QuarkResolver", "src/quark_resolver.py", centrality=0.9)
    src_id = brain._graph.execute(
        "SELECT id FROM entities WHERE name = 'QuarkResolver'").fetchone()["id"]
    for i in range(20):
        _add_doc(brain, f"src/caller_{i:02d}.py", "b")
        _add_entity(brain, f"CallerOf{i:02d}", f"src/caller_{i:02d}.py",
                    centrality=i / 100.0)
        tgt = brain._graph.execute(
            "SELECT id FROM entities WHERE name = ?", (f"CallerOf{i:02d}",)
        ).fetchone()["id"]
        brain._graph.execute(
            "INSERT INTO relationships(source_id, target_id, relation) "
            "VALUES (?,?,?)", (src_id, tgt, "called_by"))
    brain._graph.commit()

    leg = brain._graph_search("what calls QuarkResolver", 120)

    assert leg, "the structural branch returned nothing for a real relation"
    known = _doc_ids(brain)
    bad = [h["doc_id"] for h in leg if h["doc_id"] not in known]
    assert not bad, f"_traverse_graph emitted unresolvable ids: {bad}"
    assert len(leg) <= getattr(be, "_GRAPH_LEG_CAP", 12), (
        f"the structural branch is uncapped: {len(leg)} hits")


# ── AC-7: the hit shape the Explore rail consumes is unchanged ────────────
def test_ac7_a_graph_sourced_hit_still_carries_the_ui_score_key(
        tmp_path, clean_env):
    """``understand_view._hit_score`` reads ``rrf_score`` (understand_view.py:75).

    ExplorePage.tsx:158 posts /api/brain/understand, which ranks the rail by
    that key. A graph hit that arrives without it sorts to the bottom of the
    page a human is looking at, however well the leg ranked it.
    """
    brain = _make_brain(tmp_path)
    _add_doc(brain, "src/quark_resolver.py", "no query token in this body")
    _add_entity(brain, "QuarkResolver", "src/quark_resolver.py", centrality=0.7)

    hits = brain.search("QuarkResolver", limit=10)
    graph_hit = next(
        (h for h in hits if h["source_file"] == "src/quark_resolver.py"), None)

    assert graph_hit is not None, "the graph-only hit never reached search()"
    assert "doc_id" in graph_hit and "source_file" in graph_hit
    assert isinstance(graph_hit.get("rrf_score"), (int, float)), (
        f"hit has no numeric rrf_score for the Explore rail: {graph_hit!r}")


def test_ac5_e_the_measured_arm_and_the_shipped_arm_are_one_function(tmp_path):
    """``ab_retrieval.py:291`` swaps ``Brain._graph_search`` via ``load_candidate``.

    ``load_candidate`` resolves ``module:function`` (ab_retrieval.py:217) and
    cannot address a bound method, so the ranked leg has to exist as a
    MODULE-LEVEL function. If the shipped default is a different code object
    from the one the A/B measured, the benchmark result is about nothing.
    """
    fn = _harness().load_candidate(
        "prism_service.engines.brain_engine:graph_search_ranked")
    assert callable(fn), "candidate spec did not resolve to a callable"

    brain = _make_brain(tmp_path)
    _add_doc(brain, "src/quark_resolver.py", "b")
    _add_entity(brain, "QuarkResolver", "src/quark_resolver.py", centrality=0.4)
    assert brain._graph_search("QuarkResolver", 20) == fn(brain, "QuarkResolver", 20), (
        "Brain._graph_search does not delegate to the function the harness "
        "measures -- measure-one-thing-ship-another")


# ── AC-8: on by default or not shipped (owner rule mx-71dc57) ─────────────
def test_ac8_the_ranked_leg_needs_no_environment_variable(tmp_path, clean_env):
    """A capability behind a variable name nobody knows is not shipped.

    ``clean_env`` deletes every PRISM_* search flag, so this only passes if
    the ranked leg is the DEFAULT path -- and the engine may not grow a new
    ``environ.get("PRISM_...", "off")`` to gate it.
    """
    brain = _make_brain(tmp_path)
    _add_doc(brain, "src/quark_resolver.py", "b")
    _add_entity(brain, "QuarkResolver", "src/quark_resolver.py", centrality=0.6)

    known = _doc_ids(brain)
    assert all(h["doc_id"] in known
               for h in brain._graph_search("QuarkResolver", 20)), (
        "with a clean env the leg still emits ids hydration cannot resolve")

    live = _live(_ENGINE_SRC)
    assert 'environ.get("PRISM_GRAPH' not in live, (
        "the ranked graph leg must not be gated behind a new env flag")
    for bad in ('", "off")', "', 'off')"):
        assert bad not in live, (
            f"a new off-by-default flag appeared in the search path: {bad}")


# ── harness honesty: a delta without a p-value is not a result ────────────
_K = (1, 3, 5, 10, 20)


def _arm_rec(query: str, first):
    """One per-case record in the shape ``Harness.arm`` already returns."""
    return {"query": query, "gold": [f"{query}.py"], "first": first,
            "recall": {k: (1.0 if first and first <= k else 0.0) for k in _K}}


def _pair(query: str, base_first, cand_first):
    """One per-case record carrying BOTH arms, so pooling stays possible."""
    return {"query": query,
            "base_first": base_first, "cand_first": cand_first,
            "base_recall": _arm_rec(query, base_first)["recall"],
            "cand_recall": _arm_rec(query, cand_first)["recall"]}


def _payload(repo, per_case, b5, c5, b10, c10, lost=0):
    return {"repo": repo, "cases": len(per_case), "per_case": per_case,
            "baseline": {"recall": {"r@5": b5, "r@10": b10}},
            "candidate": {"recall": {"r@5": c5, "r@10": c10}},
            "tail": {"lost_entirely": lost, "rank_regressions": 0}}


def _corpora():
    a = _payload("pocketbase",
                 [_pair("a1", None, 2), _pair("a2", 9, 3), _pair("a3", 1, 1)],
                 0.50, 0.55, 0.60, 0.66)
    b = _payload("fullstackhero",
                 [_pair("b1", 2, None), _pair("b2", None, None)],
                 0.46, 0.49, 0.55, 0.58)
    c = _payload("jellyfin", [_pair("c1", 4, 4)], 0.28, 0.28, 0.35, 0.35)
    return [a, b, c]


def test_ac3_a_pooled_mcnemar_combines_every_corpus(tmp_path):
    """739 cases across three corpora, or it is a single-corpus anecdote.

    The task's declared misfire: a 5-case run once DOUBLED r@1 for a candidate
    that lost significantly at 739. The harness has no pooled path at all
    today -- ``mcnemar`` (ab_retrieval.py:186) is only ever called on one
    corpus, at one cut-off (ab_retrieval.py:308-311).
    """
    h = _harness()
    assert hasattr(h, "pool_mcnemar"), (
        "no pool_mcnemar in ab_retrieval.py -- a per-corpus p-value cannot "
        "answer 'did this help overall?'")

    pooled = h.pool_mcnemar(_corpora(), k=5)

    assert pooled["discordant"] == 3, pooled
    assert pooled["favours_baseline"] == 1, pooled
    assert pooled["favours_candidate"] == 2, pooled
    assert pooled["p"] == 1.0 and pooled["significant"] is False, pooled


def test_ac3_b_every_cutoff_can_carry_its_own_p_value():
    """"Report the p-value next to every recall delta" means every cut-off."""
    h = _harness()
    for k in h.K_VALUES:
        got = h.pool_mcnemar(_corpora(), k=k)
        assert {"discordant", "favours_baseline", "favours_candidate",
                "p", "significant"} <= set(got), f"r@{k} -> {got}"


def test_ac4_a_tail_guard_separates_unreachable_from_merely_lower_ranked():
    """The rejected graph patch raised r@1 while making two golds UNREACHABLE.

    Those are different harms and must be counted separately: any reordering
    can push a gold from rank 20 to 21, but nothing may make a file the prior
    arm found impossible to reach at any depth.
    """
    h = _harness()
    assert hasattr(h, "tail_guard"), "no tail_guard in ab_retrieval.py"

    per_base = [_arm_rec("q1", 7), _arm_rec("q2", 3), _arm_rec("q3", None)]
    per_cand = [_arm_rec("q1", None), _arm_rec("q2", 12), _arm_rec("q3", 5)]

    got = h.tail_guard(per_base, per_cand)

    assert got["lost_entirely"] == 1, (
        f"q1 was found at rank 7 and is now unreachable; got {got}")
    assert got["rank_regressions"] == 1, (
        f"q2 moved 3 -> 12 but is still reachable; got {got}")


def test_ac4_b_the_verdict_is_strict_pooled_and_tail_guarded():
    """``candidate_is_better`` (ab_retrieval.py:322-325) is weaker than the bar.

    It uses ``>=`` so a TIE prints "NOT worse on the headline metrics", tests
    one cut-off, and has no pooled path. The oracle wants a STRICT win on both
    r@5 and r@10 on two of three corpora, a pooled test that does not favour
    the prior arm, and zero unreachable golds. Recall numbers and per-case
    records are set independently here on purpose: this pins the DECISION
    rule, not the arithmetic that feeds it.
    """
    h = _harness()
    assert hasattr(h, "verdict"), "no verdict() in ab_retrieval.py"

    tie = [_payload("p1", [_pair("x", 3, 3)], 0.50, 0.50, 0.60, 0.60)] * 3
    assert h.verdict(tie)["ship"] is False, "a tie must not read as a win"
    assert h.verdict(tie)["reason"], "a refusal with no reason cannot be acted on"

    win = [_payload("p1", [_pair("x", None, 2)], 0.50, 0.56, 0.60, 0.67),
           _payload("p2", [_pair("y", None, 1)], 0.46, 0.51, 0.55, 0.60),
           _payload("p3", [_pair("z", 4, 4)], 0.28, 0.28, 0.35, 0.35)]
    assert h.verdict(win)["ship"] is True, h.verdict(win)

    pooled_loss = [dict(win[0]), dict(win[1]),
                   _payload("p3", [_pair(f"z{i}", 3, None) for i in range(8)],
                            0.28, 0.28, 0.35, 0.35)]
    assert h.verdict(pooled_loss)["ship"] is False, (
        "two corpora improved but the pooled paired test favours the prior "
        "arm -- that is the single-corpus misfire this task names")

    tail_loss = [dict(win[0]), dict(win[1]),
                 _payload("p3", [_pair("z", 4, 4)], 0.28, 0.28, 0.35, 0.35,
                          lost=1)]
    assert h.verdict(tail_loss)["ship"] is False, (
        "a gold file became unreachable; r@5 gains do not buy that back")


def test_ac4_c_the_permissive_headline_check_is_retired():
    """A contradiction left standing is how this task's own oracle went stale.

    The ``>=`` comparisons must be DELETED, not merely bypassed: two verdicts
    in one file, one of which prints PASS on a result the oracle rejects, is
    the next driver's trap. Comment lines are stripped, so a tombstone
    explaining the removal cannot pass for it.
    """
    live = _live(_HARNESS_SRC)
    for gone in ('>= base["recall"]["r@5"]',
                 '>= base["recall"]["r@10"]',
                 '>= base["any_gold_top5"]'):
        assert gone not in live, (
            f"the permissive headline check survives in the harness: {gone}")
    assert "def verdict(" in live, "verdict() must supersede it in this slice"


def test_ac9_the_superseded_rerank_decision_is_retired_in_place():
    """EXPERIMENTS.md still declares a default the engine no longer has.

    "**Decision: PRISM_RERANK=off is the permanent default.**" is directly
    contradicted by ``environ.get("PRISM_RERANK", "auto")`` at
    brain_engine.py:2913. An append-only log that keeps a refuted conclusion
    unmarked is exactly how this task's own oracle came to cite pre-rerank
    numbers, so it is retired IN PLACE, not appended past.
    """
    doc = (_REPO_ROOT / "benchmarks" / "EXPERIMENTS.md").read_text(
        encoding="utf-8")
    stale = "**Decision: PRISM_RERANK=off is the permanent default.**"
    if stale in doc:
        head = doc.split(stale)[1][:400]
        assert "SUPERSEDED" in head.upper(), (
            "the refuted 2026-04-21 decision is still standing unmarked; mark "
            "it superseded and cite brain_engine.py:2913")
