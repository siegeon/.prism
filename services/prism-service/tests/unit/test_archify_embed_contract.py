"""The archify embed must let the artifact's OWN affordances actually work.

Two things about the embed crippled the delivered artifact: the sandbox
blocked every export (Share Cards, Route Share Card, Reach Share Card all
export via an anchor `download` + createObjectURL, which `allow-downloads`
is what permits inside a sandboxed iframe), and no frontend surface ever
rendered the server-side Architecture Delta.

Convention (no JS test runner in this SPA, see test_archify_understand_ui.py):
assert the ACTUAL TSX SOURCE. Every assertion matches a RENDERED tag or a
literal a browser/runtime acts on, never a nearby explanatory comment.
"""

from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
_MAPS = _WEB / "components" / "maps" / "ArchifyMaps.tsx"


def _src() -> str:
    return _MAPS.read_text(encoding="utf-8")


def test_the_maps_component_exists():
    assert _MAPS.exists(), f"missing {_MAPS}"


def test_exports_are_unblocked_the_sandbox_permits_downloads():
    src = _src()
    # Match the RENDERED sandbox attribute on the <iframe>, not a comment.
    assert '<iframe' in src
    assert 'sandbox="allow-scripts allow-same-origin allow-downloads"' in src


def test_the_view_prop_offers_map_and_delta():
    src = _src()
    assert 'export type ArchifyView = "map" | "delta";' in src
    assert "view?: ArchifyView" in src
    # Additive: the default keeps existing callers (no view passed) on "map".
    assert 'view = "map"' in src


def test_the_delta_html_endpoint_is_reachable():
    src = _src()
    assert "/api/archify/maps/${kind}/delta/html?project=" in src


def test_the_delta_receipt_is_probed_for_availability():
    src = _src()
    assert "onDeltaAvailability?: (available: boolean) => void" in src
    # The probe hits the receipt (not the html) endpoint.
    assert "/api/archify/maps/${kind}/delta?project=" in src
    assert "onDeltaAvailability(true)" in src
    assert "onDeltaAvailability(false)" in src


def test_the_focus_deep_link_is_no_longer_gated_on_concepts():
    src = _src()
    # The old gate must be gone as a live expression.
    assert 'kind === "concepts" && focusId' not in src
    # The generalised guard: any kind, whenever focusId is supplied.
    assert "const slug = focusId ? slugForFocus(focusId) : null;" in src


def test_the_component_never_navigates_itself():
    """The page owns navigation; the component only reads the `view` prop."""
    src = _src()
    # No internal state that would let the component flip its own view.
    assert "setView" not in src
    assert "useState<ArchifyView>" not in src


def test_on_node_select_signature_is_unchanged():
    src = _src()
    assert "onNodeSelect?: (nodeId: string, kind: ArchifyKind, target?: string) => void;" in src
