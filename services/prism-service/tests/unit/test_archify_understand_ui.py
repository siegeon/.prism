"""The archify maps must be REACHABLE, not merely implemented.

The convention here (no JS test runner in this SPA) is to assert the ACTUAL
TSX source. Every assertion below matches a RENDERED TAG or a literal a
browser acts on, never a comment: a comment naming a component has satisfied
this kind of check before and hid a surface nobody could open.
"""

from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
_MAPS = _WEB / "components" / "understand" / "ArchifyMaps.tsx"
_UNDERSTAND = _WEB / "pages" / "UnderstandPage.tsx"
_TASK_DETAIL = _WEB / "pages" / "TaskDetailPage.tsx"


def test_the_maps_component_exists():
    assert _MAPS.exists(), f"missing {_MAPS}"


def test_understand_renders_the_maps_component():
    src = _UNDERSTAND.read_text(encoding="utf-8")
    assert "<ArchifyMaps" in src
    assert 'from "@/components/understand/ArchifyMaps"' in src


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


def test_the_three_project_tabs_are_offered():
    src = _MAPS.read_text(encoding="utf-8")
    for label in ('label: "Code"', 'label: "Concepts"', 'label: "Language"'):
        assert label in src


def test_a_person_can_build_the_map():
    """The empty state must carry a control, not only an explanation."""
    src = _MAPS.read_text(encoding="utf-8")
    assert '"Build map"' in src
    assert '"Rebuild"' in src
    assert "onClick={build}" in src
