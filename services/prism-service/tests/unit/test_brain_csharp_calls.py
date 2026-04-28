"""RED phase tests for siegeon#45 — C# CALLS edges regression.

Tracks GitHub issue siegeon/.prism#45 and PRISM task a319275e.

Scope: prove that on the existing tree-sitter pipeline, the Brain
graph accessors return non-empty results for a small C# fixture.
These tests MUST fail on `main` (RED) before T3 is applied and pass
after.

Story: docs/stories/csharp-call-edges-issue-45.story.md
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


_FIXTURE_DIR = _SERVICE_ROOT / "tests" / "fixtures" / "csharp"


def _ingest_fixture(brain) -> None:
    """Index every .cs file under the fixture dir into ``brain``."""
    for cs_file in sorted(_FIXTURE_DIR.glob("*.cs")):
        rel = cs_file.relative_to(_SERVICE_ROOT).as_posix()
        content = cs_file.read_text(encoding="utf-8")
        # Use the same chunk pipeline production uses; one indexed pass
        # is sufficient — we are not testing incremental drift here.
        chunks = brain._chunk_source_file(rel, content)
        for chunk in chunks:
            brain._ingest_single(
                chunk["doc_id"], chunk["content"],
                source_file=rel, domain="cs",
                entity_name=chunk["entity_name"],
                entity_kind=chunk["entity_kind"],
                line_start=chunk["line_start"],
                line_end=chunk["line_end"],
            )


@pytest.fixture
def brain(tmp_path):
    """Per-test Brain instance backed by tmp_path SQLite files."""
    from app.engines.brain_engine import Brain
    b = Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    _ingest_fixture(b)
    return b


def test_ac1_find_references_returns_cross_file_caller(brain):
    """
    AC-1: brain_find_references returns callers (fixture-bound).
    Requirement: A call to A.Foo() from B.Bar must be discoverable
    via find_references('Foo'); the response must include at least
    one row whose caller_name is Bar (or its containing entity) and
    whose caller_file is the fixture file holding class B.
    Expected: non-empty result; at least one row points back to B.Bar.
    """
    refs = brain.find_references("Foo")
    assert refs, (
        "find_references('Foo') returned [] — call edge from "
        "B.Bar -> A.Foo not present in graph"
    )
    callers = {r.get("caller_name") for r in refs}
    files = {r.get("caller_file") for r in refs}
    assert "Bar" in callers, (
        f"expected caller 'Bar' in {callers!r}"
    )
    assert any("ClassB.cs" in (f or "") for f in files), (
        f"expected ClassB.cs in caller files {files!r}"
    )


def test_ac2_call_chain_returns_direct_callee(brain):
    """
    AC-2: brain_call_chain returns callees (fixture-bound).
    Requirement: When B.Bar calls A.Foo() in the fixture, call_chain
    starting from 'Bar' must include an edge whose callee resolves
    to Foo (or A.Foo). At-least-one-direct-callee semantics.
    Expected: non-empty result with a row pointing at 'Foo'.
    """
    chain = brain.call_chain("Bar")
    assert chain, (
        "call_chain('Bar') returned [] — no outbound edges from B.Bar"
    )
    callees = set()
    for row in chain:
        for key in ("to", "callee", "callee_name", "target_name", "name"):
            if row.get(key):
                callees.add(row[key])
    assert "Foo" in callees or "A.Foo" in callees, (
        f"expected 'Foo' in callees {callees!r} from call_chain('Bar')"
    )


def test_ac3_find_symbol_class_kind(brain):
    """
    AC-3: brain_find_symbol matches C# class names (fixture-bound).
    Requirement: find_symbol('A', kind='class') must return the
    class entity from the fixture, with its source_file pointing
    at the fixture .cs file. This pins down hypothesis 4 — the
    'kind' value persisted for C# classes must equal 'class'.
    Expected: at least one row with entity_name=='A', entity_kind=='class'.
    """
    rows = brain.find_symbol("A", kind="class")
    assert rows, (
        "find_symbol('A', kind='class') returned [] — class entity "
        "either not stored or stored under a different 'kind' value"
    )
    assert any(
        r.get("entity_name") == "A"
        and r.get("entity_kind") == "class"
        and "ClassA.cs" in (r.get("source_file") or "")
        for r in rows
    ), f"expected class A in ClassA.cs, got {rows!r}"


def test_ac4_stoplist_does_not_drop_user_method(brain):
    """
    AC-4 / AC-6 (tracer-flagged gap): stoplist does not over-suppress.
    Requirement: ToStringShadow is a user-defined method whose name
    shadows a stoplist token. When B.CallShadowed invokes
    new A().ToStringShadow(), the extractor MUST emit a 'calls'
    edge — the stoplist filter must not drop user methods on name
    collision. This pins hypothesis 2 from the issue body.
    Expected: find_references('ToStringShadow') returns at least one
    row whose caller is CallShadowed.
    """
    refs = brain.find_references("ToStringShadow")
    assert refs, (
        "find_references('ToStringShadow') returned [] — stoplist "
        "filter is suppressing user-defined methods that share a "
        "name with a framework stoplist token (hypothesis 2)"
    )
    callers = {r.get("caller_name") for r in refs}
    assert "CallShadowed" in callers, (
        f"expected caller 'CallShadowed' in {callers!r}"
    )


def test_ac5_brain_outline_unchanged_for_fixture(brain):
    """
    AC-5: no regression in brain_outline for the fixture.
    Requirement: brain_outline must continue to return the structural
    members of ClassA.cs (class A, method Foo, method ToStringShadow,
    class Derived, method DerivedMethod). This is the regression-guard
    half of AC-5; full pytest suite is the other half (run separately).
    Expected: outline includes A, Foo, ToStringShadow, Derived,
    DerivedMethod.
    """
    rows = brain.outline("tests/fixtures/csharp/ClassA.cs")
    names = {r.get("entity_name") for r in rows}
    expected = {"A", "Foo", "ToStringShadow", "Derived", "DerivedMethod"}
    not_found = expected - names
    assert not not_found, (
        f"outline lost structural entries {not_found!r}; got {names!r}"
    )
