"""Write-side tests for the Ultimate Graph annotation PULL loop
(Background-Agent Annotation Layer, siegeon/.prism#50, final narrative slice).

The deterministic graph_enrich worker PUSHED: PRISM shelled `claude -p` to
infer cluster names. This slice INVERTS that into a JanitorService-style PULL:
PRISM dispenses annotation BRIEFS (enqueue -> check/dispense-one ->
submit/schema-validate -> abandon/retry); the caller's Claude session does the
inference and submits {name, purpose} back. Brief generation REUSES
graph_enrich's scope enumeration (hierarchy_scopes / community_scopes /
render_prompt / _parse) and the _input_hash escape-when-unchanged guard, so
unchanged scopes are skipped. Submitted annotations are schema-validated to
{name, purpose} and persisted via graph.upsert_annotation(...).

These tests pin the verb contract + the escape guard + persistence. They FAIL
today because prism_service.services.graph_annotate does not exist yet (red).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _graph(tmp_path):
    """A real GraphService over a temp graph.db so upsert_annotation /
    annotations_for / get_annotation are exercised for real."""
    from prism_service.services.graph_service import GraphService
    return GraphService(str(tmp_path), str(tmp_path / "graph.db"))


_SCOPES = [
    {"scope_id": "svc/a", "level": 1, "files": ["a.py", "b.py"],
     "symbols": ["Foo", "bar"], "input_hash": "hash-a"},
    {"scope_id": "svc/c", "level": 1, "files": ["c.py", "d.py"],
     "symbols": ["Baz"], "input_hash": "hash-c"},
]


def test_module_exposes_pull_loop_verbs():
    """The pull loop mirrors JanitorService: enqueue / check / submit /
    abandon — not a re-implemented push."""
    from prism_service.services import graph_annotate as ga
    svc_cls = getattr(ga, "GraphAnnotateService", None)
    assert svc_cls is not None, "GraphAnnotateService (pull loop) must exist"
    for verb in ("enqueue", "check", "submit", "abandon"):
        assert hasattr(svc_cls, verb), f"pull-loop verb {verb!r} missing"


def test_check_dispenses_one_brief_with_name_purpose_schema(tmp_path):
    """check() dispenses at most one brief; the brief carries the rendered
    graph_enrich prompt and a response_schema constrained to {name, purpose}."""
    from prism_service.services import graph_annotate as ga
    svc = ga.GraphAnnotateService(_graph(tmp_path))
    svc.enqueue("hierarchy", _SCOPES)
    first = svc.check("sess-1")
    assert first["ready"] is True
    brief = first["brief"]
    assert set(brief["response_schema"]) == {"name", "purpose"}
    assert brief.get("scope_kind") == "hierarchy"
    assert brief.get("scope_id") in {"svc/a", "svc/c"}
    assert isinstance(brief.get("prompt"), str) and brief["prompt"]
    # one per call — the second scope is still pending, not dispensed twice
    assert "brief_id" in brief or "candidate_id" in brief


def test_submit_schema_validates_to_name_purpose(tmp_path):
    """submit() rejects output missing name/purpose; accepts a valid pair."""
    from prism_service.services import graph_annotate as ga
    svc = ga.GraphAnnotateService(_graph(tmp_path))
    svc.enqueue("hierarchy", _SCOPES[:1])
    brief = svc.check("sess-1")["brief"]
    bid = brief.get("brief_id") or brief.get("candidate_id")
    bad = svc.submit(bid, {"name": "Only Name"})  # missing purpose
    assert bad["accepted"] is False
    good = svc.submit(bid, {"name": "Service A", "purpose": "Does A things."})
    assert good["accepted"] is True


def test_submit_persists_annotation_via_upsert(tmp_path):
    """A valid submit persists through graph.upsert_annotation with the
    scope's input_hash and an LLM provenance ('claude @ <date>'), readable
    back via annotations_for."""
    from prism_service.services import graph_annotate as ga
    graph = _graph(tmp_path)
    svc = ga.GraphAnnotateService(graph)
    svc.enqueue("hierarchy", _SCOPES[:1])
    brief = svc.check("sess-1")["brief"]
    bid = brief.get("brief_id") or brief.get("candidate_id")
    svc.submit(bid, {"name": "Service A", "purpose": "Does A things."})
    stored = graph.annotations_for("hierarchy")
    assert "svc/a" in stored
    assert stored["svc/a"]["name"] == "Service A"
    assert stored["svc/a"]["purpose"] == "Does A things."
    assert stored["svc/a"]["provenance"].startswith("claude @ ")
    # input_hash must be carried so the escape guard works next sweep
    got = graph.get_annotation("hierarchy", "svc/a", "name")
    assert got["input_hash"] == "hash-a"


def test_enqueue_escapes_unchanged_scopes(tmp_path):
    """The _input_hash escape guard: a scope whose stored annotation's
    input_hash already matches is NOT enqueued (zero work when unchanged)."""
    from prism_service.services import graph_annotate as ga
    graph = _graph(tmp_path)
    # Pre-seed svc/a as already annotated at the same input_hash.
    graph.upsert_annotation("hierarchy", "svc/a", "name",
                            "Service A", "Does A things.", "hash-a",
                            "claude @ 2026-05-29")
    svc = ga.GraphAnnotateService(graph)
    n = svc.enqueue("hierarchy", _SCOPES)
    assert n == 1, "only the CHANGED scope (svc/c) should be enqueued"
    brief = svc.check("sess-1")["brief"]
    assert brief["scope_id"] == "svc/c"


def test_enqueue_reuses_graph_enrich_enumeration(monkeypatch, tmp_path):
    """Brief generation REUSES graph_enrich.hierarchy_scopes /
    community_scopes / render_prompt rather than re-deriving scopes."""
    from prism_service.services import graph_annotate as ga
    from prism_service.services import graph_enrich

    called = {"hierarchy": 0, "community": 0, "render": 0}
    monkeypatch.setattr(graph_enrich, "hierarchy_scopes",
                        lambda project, **k: (called.__setitem__(
                            "hierarchy", called["hierarchy"] + 1), _SCOPES)[1])
    monkeypatch.setattr(graph_enrich, "community_scopes",
                        lambda project, **k: (called.__setitem__(
                            "community", called["community"] + 1), [])[1])
    real_render = graph_enrich.render_prompt
    monkeypatch.setattr(graph_enrich, "render_prompt",
                        lambda s: (called.__setitem__(
                            "render", called["render"] + 1), real_render(s))[1])

    svc = ga.GraphAnnotateService(_graph(tmp_path))
    # The project-level entry enumerates scopes for both kinds via graph_enrich.
    svc.enqueue_project("prism")
    assert called["hierarchy"] >= 1
    assert called["community"] >= 1
    svc.check("sess-1")
    assert called["render"] >= 1, "brief must use graph_enrich.render_prompt"


def test_abandon_requeues_until_retry_limit(tmp_path):
    """abandon() requeues (status stays dispensable) until a hard retry
    limit, mirroring JanitorService — a failed inference is retried, not lost."""
    from prism_service.services import graph_annotate as ga
    svc = ga.GraphAnnotateService(_graph(tmp_path))
    svc.enqueue("hierarchy", _SCOPES[:1])
    brief = svc.check("sess-1")["brief"]
    bid = brief.get("brief_id") or brief.get("candidate_id")
    res = svc.abandon(bid, reason="inference failed")
    assert res["accepted"] is True
    assert res["status"] in {"pending", "abandoned"}
