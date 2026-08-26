"""The Ontology page is the four-tab document (task 6d470cea-e344-40ea-
b551-c43593ec7db9, epic e39027d3 -- owner: 'not quite there with ontology').

Rebuilds OntologyPage.tsx as the prototype's document: Structure / Rules /
Records / Terms, each bound to its own /api/okf/ontology/<tab> endpoint (a
sibling slice landing separately -- a 404 renders an honest "not available
yet" line, never a stub). The old rail+pills OntologyPanel layout is gone.

The SPA has no JS test runner, so every clause is pinned by reading the
ACTUAL TSX source (see test_tasks_page_unified_queue.py for the pattern).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_ONTOLOGY_PAGE = _SRC / "pages" / "OntologyPage.tsx"


def _page() -> str:
    return _ONTOLOGY_PAGE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The old rail/pill layout is gone.
# ---------------------------------------------------------------------------

def test_old_rail_layout_is_gone():
    page = _page()
    assert "OntologyPanel" not in page
    assert "ClassGroup" not in page
    assert "grid-cols-[220px" not in page


# ---------------------------------------------------------------------------
# Tab strip: four names, each bound to a count; need-a-decision pill; Refresh
# posts to /rebuild and refetches.
# ---------------------------------------------------------------------------

def test_tab_strip_has_the_four_names_bound_to_counts():
    page = _page()
    assert '{ key: "structure", label: "Structure" }' in page
    assert '{ key: "rules", label: "Rules" }' in page
    assert '{ key: "records", label: "Records" }' in page
    assert '{ key: "terms", label: "Terms" }' in page
    assert "counts[t.key]" in page


def test_need_decision_pill_and_refresh_bound_to_rebuild():
    page = _page()
    assert "rules need a decision" in page
    assert "/api/okf/ontology/rebuild" in page
    assert ">Refresh<" in page or "Refresh" in page
    # Refresh must refetch all four after posting the rebuild.
    assert "fetchAll" in page


def test_tab_choice_persists_in_localstorage_with_a_try_catch():
    page = _page()
    assert 'localStorage.getItem("prism.ontology.tab")' in page
    assert 'localStorage.setItem("prism.ontology.tab"' in page
    assert "catch" in page


# ---------------------------------------------------------------------------
# Structure tab: tree row with depth indent + count + comment, and a
# relation card with domain/property/range/count/example.
# ---------------------------------------------------------------------------

def test_structure_tree_row_has_depth_indent_count_and_comment():
    page = _page()
    assert "c.depth * 16" in page
    assert 'data-kind="class"' in page
    assert "data-abstract={c.abstract" in page
    assert "{c.count}" in page
    assert "{c.comment}" in page


def test_structure_relation_card_has_domain_property_range_count_example():
    page = _page()
    assert "{r.domain}" in page
    assert "{r.property}" in page
    assert "{r.range}" in page
    assert "{r.count}" in page
    assert "r.example" in page
    assert "from_label" in page
    assert "to_label" in page


# ---------------------------------------------------------------------------
# Rules tab: NEED A DECISION section first, then the quiet rules; focus
# chips capped with a "+N more" rollup; "N CHECKED · M FAILED" text.
# ---------------------------------------------------------------------------

def test_rules_need_a_decision_renders_before_quiet():
    page = _page()
    need_idx = page.index("needsDecision")
    quiet_idx = page.index("const quiet")
    assert need_idx < quiet_idx
    assert "Need a decision" in page


def test_rules_focus_chips_capped_and_checked_failed_text():
    page = _page()
    assert "rule.focus.slice(0, 8)" in page
    assert "overflow > 0" in page
    assert "rule.looked_at" in page
    assert "CHECKED" in page
    assert "rule.violations" in page
    assert "FAILED" in page


# ---------------------------------------------------------------------------
# Records tab: totals sentence, per-class samples, an INSTANCES expander
# over /instances, and the SPARQL query box moved to the bottom.
# ---------------------------------------------------------------------------

def test_records_totals_sentence_and_samples_and_instances_expander():
    page = _page()
    assert "things and" in page
    assert "connections between them, holding" in page
    assert "c.sample" in page
    assert "/api/okf/ontology/instances" in page
    assert "Instances" in page
    assert "onToggle" in page


def test_records_hosts_the_sparql_query_box():
    page = _page()
    assert "<textarea" in page
    assert "/api/okf/ontology/sparql" in page


# ---------------------------------------------------------------------------
# Terms tab: vocabularies with in-use vs unused chip styling, and a HELD
# BACK section for undeclared values.
# ---------------------------------------------------------------------------

def test_terms_vocabularies_in_use_styling_and_held_back():
    page = _page()
    assert "t.in_use" in page
    assert "v.terms" in page
    assert "Held back" in page
    assert "held_back" in page


# ---------------------------------------------------------------------------
# Honest empty/error states when a sibling endpoint 404s.
# ---------------------------------------------------------------------------

def test_a_404d_endpoint_renders_an_honest_not_available_line():
    page = _page()
    assert "Not available yet" in page
    assert "ApiError" in page


# ---------------------------------------------------------------------------
# index.css carries a data-abstract rule for the reused .ont-node class pill.
# ---------------------------------------------------------------------------

def test_ont_node_data_abstract_rule_exists():
    css = (_SRC / "index.css").read_text(encoding="utf-8")
    assert '.ont-node[data-abstract="true"]' in css
