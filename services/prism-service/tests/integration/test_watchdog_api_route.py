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
    consolidation_router), so the SPA's Diagnostics card has a backend."""
    routes = {getattr(r, "path", "") for r in api_router.routes}
    assert any(p.startswith("/api/watchdog") for p in routes), (
        "no /api/watchdog route registered on api_router — wire "
        "watchdog_router into prism_service/api/__init__.py"
    )
