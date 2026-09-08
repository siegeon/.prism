"""The archify maps must be REACHABLE, not merely implemented.

The convention here (no JS test runner in this SPA) is to assert the ACTUAL
TSX source. Every assertion below matches a RENDERED TAG or a literal a
browser acts on, never a comment: a comment naming a component has satisfied
this kind of check before and hid a surface nobody could open.
"""

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
_MAPS = _WEB / "components" / "maps" / "ArchifyMaps.tsx"
_UNDERSTAND = _WEB / "pages" / "UnderstandPage.tsx"
_TASK_DETAIL = _WEB / "pages" / "TaskDetailPage.tsx"


def test_the_maps_component_exists():
    assert _MAPS.exists(), f"missing {_MAPS}"


def test_understand_renders_the_maps_component():
    src = _UNDERSTAND.read_text(encoding="utf-8")
    assert "<ArchifyMaps" in src
    assert 'from "@/components/maps/ArchifyMaps"' in src


def test_task_page_renders_the_task_map():
    src = _TASK_DETAIL.read_text(encoding="utf-8")
    assert '<ArchifyMaps project={project} kind="task" taskId={id} />' in src


def test_the_map_is_an_iframe_with_a_sandbox():
    # RE-ANCHORED: this asserted the exact string
    # `sandbox="allow-scripts allow-same-origin"`, which pinned the sandbox
    # to a fixed set and broke the moment `allow-downloads` joined it
    # (task ce767f23 — without it the artifact's own export menu, Share Card
    # and Route/Reach cards fail silently, because archify exports through an
    # anchor `download` plus createObjectURL).
    #
    # The real invariant is not the exact string: it is that the frame stays
    # sandboxed AND keeps the two tokens the embed depends on — `allow-scripts`
    # so the viewer runs at all, and `allow-same-origin` so the parent can read
    # contentDocument and turn a node click into a selection. Assert those by
    # name and let the set grow.
    src = _MAPS.read_text(encoding="utf-8")
    assert "<iframe" in src
    match = re.search(r'sandbox="([^"]*)"', src)
    assert match, "the map iframe must declare a sandbox"
    tokens = set(match.group(1).split())
    assert "allow-scripts" in tokens, "the archify viewer cannot run without scripts"
    assert "allow-same-origin" in tokens, "node clicks read contentDocument"


def test_the_map_reads_the_archify_api():
    src = _MAPS.read_text(encoding="utf-8")
    assert "/api/archify/maps/" in src


def test_a_map_is_a_reading_of_a_surface_never_a_panel_stacked_on_it():
    """Owner: 'we are converging our intelligence, not sub dividing it.'

    Each map is mounted as ONE READING of the surface that already owns its
    subject, reached by that surface's own control — never as an extra card
    above the surface's existing content. Understand draws the concepts it
    already lists; Explore draws the same code graph its mesh and Sigma map
    read. An earlier cut rendered a tabbed Maps panel above the Understand
    domain grid, which put two views of one thing on one page.
    """
    understand = _UNDERSTAND.read_text(encoding="utf-8")
    explore = (_WEB / "pages" / "ExplorePage.tsx").read_text(encoding="utf-8")

    # Understand: drawn OR listed, behind one toggle — never both at once.
    assert "const [drawn, setDrawn] = useState(true);" in understand
    assert "{drawn ? (" in understand
    assert '<ArchifyMaps\n          project={project}\n          kind="concepts"' in understand

    # Explore: the map is not A reading of the surface any more, it IS the
    # surface (7.13.261). The wantArchitecture toggle was retired because
    # there is no longer a competing panel to toggle against: the page was
    # stripped to the code graph, so a bare visit draws the architecture
    # directly (owner: "the code arch is in the graph isnt it?"). That is
    # this test's own principle carried further, not abandoned -- one view
    # of one thing, with no control needed to choose between two.
    assert "const [wantArchitecture" not in explore, (
        "the architecture toggle should stay retired: the front door IS the "
        "architecture, so a toggle would put two views of one thing back on "
        "one page")
    assert 'kind="code"' in explore and "fill" in explore, (
        "Explore must still draw the code map, filling the surface")
    # And it must not have re-grown the brain-search furniture that made it
    # a second Understand: no domain pills, no ranked/communities strip.
    # Assert against CODE, not against rendered words: the strip's label read
    # "communities" in source and was uppercased by CSS, so matching the
    # uppercase form would never have caught it, and matching the lowercase
    # form hits any comment that mentions the word.
    for gone in ('"expertise"', "counts.communities", "<Stat ", "setResultsOpen"):
        assert gone not in explore, f"Explore re-grew brain-search chrome: {gone}"


def test_understand_never_draws_the_code_map():
    """Understand reads what the brain knows; the code graph is Explore's."""
    understand = _UNDERSTAND.read_text(encoding="utf-8")
    assert 'kind="code"' not in understand
    assert 'kind="language"' not in understand


def test_a_person_can_build_the_map():
    """The empty state must carry a control, not only an explanation."""
    src = _MAPS.read_text(encoding="utf-8")
    assert '"Build map"' in src
    assert '"Rebuild"' in src
    assert "onClick={build}" in src
