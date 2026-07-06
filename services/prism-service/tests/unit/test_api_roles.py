"""GET /api/roles must mirror the roles registry to the SPA.

The route is a thin read-through to prism_service.models.roles.registry()
(the ONLY place roles/tiers are declared). This pins the USER-FACING seam:
the SPA's tier/label lookups + the model-agnostic routing table read this.
Mirrors the api_router mounting pattern from test_api_agent_runs.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def test_route_registered_on_api_router():
    from fastapi import FastAPI
    from prism_service.api import api_router
    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/roles" in paths, (
        f"GET /api/roles not mounted on api_router: {sorted(paths)}"
    )


def test_get_roles_returns_200_and_registry_body():
    from prism_service.models import roles
    resp = _client().get("/api/roles")
    assert resp.status_code == 200, resp.text
    assert resp.json() == roles.registry()


def test_get_roles_body_has_canon_roles_and_tiers():
    resp = _client().get("/api/roles")
    body = resp.json()
    assert set(body) == {"tiers", "efforts", "roles", "step_roles"}
    assert set(body["roles"]) == {"sm", "qa", "dev"}
    for name in ("frontier", "balanced", "fast"):
        assert name in body["tiers"]


def test_get_roles_step_roles_gates_are_stewarded():
    """The registry the SPA reads maps every gate to the Steward (sm)."""
    from prism_service.models.workflow import WORKFLOW_STEPS
    body = _client().get("/api/roles").json()
    step_roles = body["step_roles"]
    for s in WORKFLOW_STEPS:
        if s["type"] == "gate":
            assert step_roles.get(s["id"]) == "sm"
