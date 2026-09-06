"""The archify maps must be REACHABLE, not merely implemented.

The convention here (no JS test runner in this SPA) is to assert the ACTUAL
TSX source. Every assertion below matches a RENDERED TAG or a literal a
browser acts on, never a comment: a comment naming a component has satisfied
this kind of check before and hid a surface nobody could open.
"""

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
    src = _MAPS.read_text(encoding="utf-8")
    assert "<iframe" in src
    assert 'sandbox="allow-scripts allow-same-origin"' in src


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

    # Explore: a third reading of the same graph, in the same viewer body.
    assert "const [wantArchitecture, setWantArchitecture] = useState(false);" in explore
    assert "{wantArchitecture ? (" in explore
    assert '<ArchifyMaps project={project} kind="code" />' in explore


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
