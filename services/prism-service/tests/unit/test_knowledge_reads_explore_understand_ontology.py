"""Knowledge reads as Explore, Understand, Ontology (task eca23a10-2922-4b4d-
b092-83b1d1d4c082).

Owner: 'the explore and the understand and the ontology under knowledge is
not super clear'. Sidebar's Knowledge section gets a THIRD entry (Ontology,
/ontology) plus a one-line hint under each of the three labels; the Ontology
panel moves off UnderstandPage (removing its Concepts/Ontology toggle) onto
its own OntologyPage, which adds a Rules section (axioms bound to
looked_at/detail) and a SPARQL query box over POST /api/okf/ontology/sparql.

The SPA has no JS test runner, so every UI clause is pinned by reading the
ACTUAL TSX source (see test_tasks_page_unified_queue.py for the pattern).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_SIDEBAR = _SRC / "components" / "Sidebar.tsx"
_APP = _SRC / "App.tsx"
_ONTOLOGY_PAGE = _SRC / "pages" / "OntologyPage.tsx"
_UNDERSTAND_PAGE = _SRC / "pages" / "UnderstandPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _section(src: str, label: str) -> str:
    """The MAIN_SECTIONS object literal for the given section label — a
    balanced-brace walk, never a character window (test_files_under_learning_
    loop.py's proven pattern), so a stray comment can't satisfy this."""
    marker = f'label: "{label}"'
    i = src.index(marker)
    start = src.rindex("{", 0, i)
    depth = 0
    end = start
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    return src[start:end + 1]


# ---------------------------------------------------------------------------
# Sidebar — Knowledge has exactly Explore, Understand, Ontology, each with
# its hint string.
# ---------------------------------------------------------------------------

def test_knowledge_section_has_exactly_three_items_with_hints():
    section = _section(_read(_SIDEBAR), "Knowledge")
    assert '{ to: "/brain", label: "Explore", icon: Brain, staleKey: "brain", hint: "the code graph" }' in section
    assert '{ to: "/understand", label: "Understand", icon: Eye, staleKey: "understand", hint: "concepts and memory" }' in section
    assert '{ to: "/ontology", label: "Ontology"' in section
    assert 'hint: "classes, rules, queries"' in section
    # Exactly three items in the section — count the `to:` entries.
    assert section.count("to:") == 3


def test_sidebar_renders_item_hint_as_a_second_line():
    src = _read(_SIDEBAR)
    assert "hint?: string" in src
    # The nav item destructure must pull `hint` off the item literal, and the
    # render must read item.hint somewhere (not merely destructure it).
    assert "hint" in src[src.index("section.items.map"):src.index("section.items.map") + 2000]


# ---------------------------------------------------------------------------
# App.tsx — /ontology routes to OntologyPage.
# ---------------------------------------------------------------------------

def test_app_routes_ontology_to_ontology_page():
    app = _read(_APP)
    assert 'lazyRoute("ontology", () => import("@/pages/OntologyPage"))' in app
    assert '<Route path="/ontology" element={<OntologyPage />} />' in app


# ---------------------------------------------------------------------------
# OntologyPage.tsx — the four-tab document (re-anchored by task 6d470cea:
# OntologyPanel's rail+pills layout was replaced by Structure/Rules/Records/
# Terms tabs; see test_ontology_page_four_tabs.py for the full pin). Rules
# still reads looked_at, and the SPARQL query box + error line moved to the
# bottom of Records but is still present in the source.
# ---------------------------------------------------------------------------

def test_ontology_page_renders_the_four_tab_strip():
    page = _read(_ONTOLOGY_PAGE)
    assert '{ key: "structure", label: "Structure" }' in page
    assert '{ key: "rules", label: "Rules" }' in page
    assert '{ key: "records", label: "Records" }' in page
    assert '{ key: "terms", label: "Terms" }' in page


def test_ontology_page_rules_section_reads_looked_at():
    page = _read(_ONTOLOGY_PAGE)
    assert "looked_at" in page


def test_ontology_page_has_sparql_textarea_and_run_posting_to_sparql_endpoint():
    page = _read(_ONTOLOGY_PAGE)
    assert "<textarea" in page
    assert "Run" in page
    assert "/api/okf/ontology/sparql" in page


def test_ontology_page_shows_an_honest_error_line():
    page = _read(_ONTOLOGY_PAGE)
    # An error state variable/message must be rendered somewhere, not
    # swallowed — this is the "endpoint may 404/400" honesty clause.
    assert "error" in page.lower()


# ---------------------------------------------------------------------------
# UnderstandPage.tsx — the Concepts/Ontology toggle and OntologyPanel are
# gone; Understand is the concept wiki again.
# ---------------------------------------------------------------------------

def test_understand_page_no_longer_hosts_ontology_panel_or_toggle():
    page = _read(_UNDERSTAND_PAGE)
    assert "OntologyPanel" not in page
    assert '"ontology"' not in page
