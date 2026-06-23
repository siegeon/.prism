"""Red scaffold (integration) — GET /api/watchdog (GH #155).

Pins the USER-FACING seam, not a unit contract: the route must be reachable
through the real aggregated api_router (wired into api/__init__.py like
consolidation_router), return 200, and carry the required status fields.
A test that imported the watchdog router module directly would pass even if
it were never included in api_router — so we go through the mounted app.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from prism_service.api import api_router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_api_watchdog_returns_200_with_required_fields():
    resp = _client().get("/api/watchdog")
    assert resp.status_code == 200, (
        f"/api/watchdog not reachable through api_router (got {resp.status_code}) "
        "— router not wired into api/__init__.py"
    )
    body = resp.json()
    for field in ("last_probe_ok", "consecutive_failures", "dump_count"):
        assert field in body, f"/api/watchdog body missing {field!r}: {body}"


def test_watchdog_router_included_in_api_init():
    """The watchdog router must be aggregated into api_router (mirroring
    consolidation_router), so the SPA's Diagnostics card has a backend.

    Collect mounted paths via the OpenAPI schema, NOT raw api_router.routes:
    FastAPI >= 0.138 / Starlette >= 1.3 represent include_router() entries as
    internal _IncludedRouter objects with no .path attribute, so the old
    `getattr(r, "path", "")` silently yields "" for every mounted sub-router
    and the guard reads false-red even though /api/watchdog IS wired and
    reachable (the sibling 200-status test proves the route mounts). The
    OpenAPI schema is the version-agnostic contract for "which paths are
    actually mounted", which is exactly what this guard asserts. Mirrors the
    agent-runs guard fix (commit 59fcfb7)."""
    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert any(p.startswith("/api/watchdog") for p in paths), (
        "no /api/watchdog route registered on api_router — wire "
        f"watchdog_router into prism_service/api/__init__.py: {sorted(paths)}"
    )
