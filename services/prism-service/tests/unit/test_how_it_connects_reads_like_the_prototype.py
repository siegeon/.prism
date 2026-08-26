"""How it connects reads like the prototype's cards (task c37bc70e-e124-4085-
b85e-e9f5cc5a2a76).

Owner on the live Structure tab: 'looking real good, but we are not quite
right on the ontology view - look at the right hand side (How it connects)
vs the original - and the sub title row does not belong'.

FIX 1: each relation card in the "How it connects" column becomes a bordered
card -- domain / property+arrow / range on one row (range right-aligned,
property in a dedicated ochre mono class), a count badge + comment row, and
an optional from_label -> to_label example row -- sorted by count desc.

FIX 2: the page's own duplicate 'Ontology' title/subtitle row is gone -- the
tab strip and the need-a-decision pill + Refresh button now render in ONE
row under the app PageHeader.

The SPA has no JS test runner, so every clause is pinned by reading the
ACTUAL TSX/CSS source (see test_ontology_page_four_tabs.py for the pattern).
Balanced-brace slicing (never a character window) isolates the real
function body so a stray comment can't satisfy an assertion.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_ONTOLOGY_PAGE = _SRC / "pages" / "OntologyPage.tsx"
_CSS = _SRC / "index.css"


def _page() -> str:
    return _ONTOLOGY_PAGE.read_text(encoding="utf-8")


def _css() -> str:
    return _CSS.read_text(encoding="utf-8")


def _function_body(src: str, signature: str) -> str:
    """Balanced-brace slice of a top-level `function <signature>(...)` body,
    so a comment (or the parameter destructure's own braces) can't satisfy
    an assertion. Skips the balanced parameter-list parens first, then
    balances the braces of the function body itself."""
    i = src.index(signature)
    paren_start = src.index("(", i)
    depth = 0
    j = paren_start
    while True:
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                break
        j += 1
    start = src.index("{", j)
    depth = 0
    end = start
    for k in range(start, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                end = k
                break
    return src[start:end + 1]


def _css_rule(css: str, selector: str) -> str:
    i = css.index(selector)
    return css[i:css.index("}", i) + 1]


# ---------------------------------------------------------------------------
# FIX 1 -- the relation card in StructureRelations.
# ---------------------------------------------------------------------------

def test_relation_card_is_a_bordered_card_element():
    body = _function_body(_page(), "function StructureRelations")
    assert "ont-edge-card" in body
    css_rule = _css_rule(_css(), ".ont-edge-card")
    assert "border:" in css_rule


def test_relation_card_row1_domain_property_range_with_range_right_aligned():
    body = _function_body(_page(), "function StructureRelations")
    assert "{r.domain}" in body
    assert "ont-edge-property" in body
    assert "{r.property} →" in body
    range_idx = body.index("{r.range}")
    right_span = body[body.rindex("<span", 0, range_idx):range_idx]
    assert "text-right" in right_span


def test_relation_card_property_is_the_ochre_mono_class():
    rule = _css_rule(_css(), ".ont-edge-property")
    assert "var(--social)" in rule
    assert "var(--font-mono)" in rule


def test_relation_card_row2_count_badge_then_comment():
    body = _function_body(_page(), "function StructureRelations")
    badge_idx = body.index("ont-edge-count")
    comment_idx = body.index("{r.comment}")
    assert badge_idx < comment_idx
    rule = _css_rule(_css(), ".ont-edge-count")
    assert "var(--social)" in rule
    assert "border-radius:999px" in rule


def test_relation_card_row3_example_bound_to_from_and_to_label():
    body = _function_body(_page(), "function StructureRelations")
    assert "r.example.from_label" in body
    assert "r.example.to_label" in body
    # Omitted when there's no example; a count-0 card still renders rows 1-2.
    assert "r.example &&" in body


def test_relation_cards_sorted_by_count_desc():
    body = _function_body(_page(), "function StructureRelations")
    assert "b.count - a.count" in body


# ---------------------------------------------------------------------------
# FIX 2 -- one header row: tabs left, pill + Refresh right; no duplicate
# in-page title/subtitle.
# ---------------------------------------------------------------------------

def test_no_duplicate_in_page_title_or_subtitle():
    page = _page()
    assert '{"Ontology"}' not in page
    assert "Classes, rules, and queries over the projected ontology." not in page


def test_tab_strip_and_pill_and_refresh_share_one_row():
    body = _function_body(_page(), "function OntologyPage")
    tabs_idx = body.index("TABS.map")
    pill_idx = body.index("rules need a decision")
    # Search for the real Refresh button text AFTER the pill, not an
    # earlier explanatory comment that happens to say the same word.
    refresh_idx = body.index("Refresh", pill_idx)
    assert tabs_idx < pill_idx < refresh_idx
    # All three live inside the same still-open header <div> -- the div
    # opening right before TABS.map must not have closed by the time we
    # reach the Refresh button.
    row_open = body.rindex("<div", 0, tabs_idx)
    between = body[row_open:refresh_idx]
    assert between.count("</div>") < between.count("<div")
