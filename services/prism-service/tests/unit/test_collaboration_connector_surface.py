"""The Collaboration connector card a person actually sees (epic 0784729f).

Before this, `/api/collaboration/surfaces` returned a bare name list -
"an API, not an affordance" (the exact correction tests/unit/
test_inbox_surface.py records for the sibling Inbox surface). Nothing in
Settings rendered it, so a registered adapter and a broken one looked
identical to anyone who was not curling the endpoint.

HONESTY REQUIREMENT (owner rule, task f4dd3687's whole point): a surface
renders ONLY when something real backs it - `connected` when production
code actually constructed the adapter, `unavailable` with its recorded
reason when construction failed, and nothing at all when nothing was ever
attempted. This machine has a real GitHub CLI credential, so `github` is
expected to come back `connected` for real, not injected.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


# ----------------------------------------------------------------------
# API: per-surface detail, through the ASSEMBLED app
# ----------------------------------------------------------------------

def test_surfaces_endpoint_reports_detail_through_the_assembled_app():
    """Mounted the way a browser reaches it - not a bare router in a test
    FastAPI(), which proves the handler works and nothing about whether
    api/__init__.py actually wires it in (the exact gap
    test_inbox_surface.py::test_the_real_app_exposes_the_inbox_route
    exists to catch on its own surface)."""
    from fastapi.testclient import TestClient

    from prism_service.main import app

    with TestClient(app) as client:
        r = client.get("/api/collaboration/surfaces")
    assert r.status_code != 404, (
        "the assembled app does not serve /api/collaboration/surfaces")
    body = r.json()
    assert "surfaces" in body and "errors" in body, (
        "existing keys must keep working - something already reads them")
    assert "detail" in body, "no per-surface detail in the response"
    detail = body["detail"]
    assert isinstance(detail, list) and detail, (
        "no collaboration surface registered by production code - the "
        "exact 0784729f failure (task f4dd3687)")
    for row in detail:
        assert set(row) >= {"surface", "state", "detail"}

    github_rows = [d for d in detail if d["surface"] == "github"]
    assert github_rows, f"github is not in the detail rows: {detail}"
    # Real credentials exist on this machine (verified via a clean import
    # before writing this test) - so this must be the real state, not a
    # fixture standing in for it.
    assert github_rows[0]["state"] == "connected", (
        f"github did not come back connected on this machine: {github_rows}")


def test_a_registration_error_surfaces_as_unavailable_with_reason():
    """The pure computation, isolated from whatever this machine happens to
    have installed: a surface that failed to construct must show up as
    `unavailable` carrying the SAME reason string the registry recorded,
    never silently dropped."""
    from prism_service.api import collaboration as collab_api

    snapshot = dict(collab_api.collaboration_registration_errors)
    try:
        collab_api.collaboration_registration_errors.clear()
        collab_api.collaboration_registration_errors["buzz"] = (
            "RuntimeError: no buzz-cli installed")
        rows = collab_api.collaboration_surface_detail()
    finally:
        collab_api.collaboration_registration_errors.clear()
        collab_api.collaboration_registration_errors.update(snapshot)

    buzz_rows = [r for r in rows if r["surface"] == "buzz"]
    assert buzz_rows, f"buzz did not surface at all: {rows}"
    assert buzz_rows[0]["state"] == "unavailable"
    assert buzz_rows[0]["detail"] == "RuntimeError: no buzz-cli installed"


def test_route_requires_auth_like_its_siblings():
    """Structural, not a live 401 round trip (which would have to fight the
    loopback-trust machinery this repo already has its own suite for) -
    checks the SAME dependency every sibling collaboration/integration
    router declares (api/collaboration.py:34, mirroring
    api/integrations.py:31)."""
    from prism_service.api.auth import current_principal
    from prism_service.api.collaboration import router

    assert any(d.dependency is current_principal for d in router.dependencies), (
        "the collaboration router dropped its current_principal dependency")


# ----------------------------------------------------------------------
# UI: the card a person actually clicks into, inside Settings > Connectors
# ----------------------------------------------------------------------

def _settings_src() -> str:
    return (_WEB / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")


def _connectors_section_body() -> str:
    src = _settings_src()
    start = src.index("function ConnectorsSection(")
    # Balance braces from the function's opening one, so the slice is the
    # WHOLE function body and nothing past its own closing brace.
    depth = 0
    i = src.index("{", start)
    body_start = i
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[body_start:j + 1]
    raise AssertionError("ConnectorsSection never closes")


def test_a_collaboration_card_renders_inside_connectors():
    """THE AFFORDANCE - reachable by a real click in Settings > Connectors,
    not a component that merely exists somewhere in the file (the e139295d
    lesson: SectionId/KNOWN_SECTIONS/SECTION_META were all correct and green
    while the surface stayed unreachable, because the entry point lived
    outside the slice)."""
    body = _connectors_section_body()
    # Match the actual rendered JSX tag, not a comment mentioning the name -
    # a bare substring search is satisfied by an explanatory comment.
    assert re.search(r"<Collaboration\w*Card\b", body), (
        "no Collaboration*Card component is rendered inside "
        "ConnectorsSection's own return JSX")


def test_the_collaboration_card_reads_live_registry_state():
    """Never a fixture. The card's own source must call the real endpoint -
    scoped to the component's definition, not the whole file, so an
    unrelated import elsewhere cannot satisfy it."""
    src = _settings_src()
    m = re.search(r"function Collaboration\w*Card\([^)]*\)\s*\{", src)
    assert m, "no CollaborationCard-shaped component is defined"
    depth = 0
    i = src.index("{", m.start())
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                comp_src = src[i:j + 1]
                break
    else:
        raise AssertionError("component body never closes")
    assert "listCollaborationSurfaces" in comp_src, (
        "the card does not call the live collaboration-surfaces API")
