"""CORS smoke test for the Tauri shell splash path (closes issue #88).

The standalone Tauri shell loads its splash from a tauri:// origin and
fetches http://127.0.0.1:7778/api/version cross-origin. Without
CORSMiddleware the response carries no Access-Control-Allow-Origin
header — the browser silently drops the response body and the splash
hangs at "Starting backend…" until its 30s timeout fires.

These tests pin the server contract so that class of break can't slip
into a release again. They:

  1. assert /api/version returns an `access-control-allow-origin`
     header that matches the Origin: tauri://localhost the request
     was made with;
  2. assert the OPTIONS preflight returns 200 (not 405) for the same
     Origin — which is the second wall the splash hit after the GET;
  3. cover the Windows WebView2 origin `http(s)://tauri.localhost` too.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

httpx = pytest.importorskip("httpx")
testclient_mod = pytest.importorskip("fastapi.testclient")
TestClient = testclient_mod.TestClient

from prism_service.main import app


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.mark.parametrize(
    "origin",
    [
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
)
def test_api_version_returns_cors_allow_origin_for_tauri(client, origin):
    r = client.get("/api/version", headers={"Origin": origin})
    assert r.status_code == 200
    assert r.headers.get("access-control-allow-origin") == origin, (
        f"expected access-control-allow-origin: {origin}, "
        f"got: {dict(r.headers)!r}"
    )


@pytest.mark.parametrize(
    "origin",
    [
        "tauri://localhost",
        "http://tauri.localhost",
        "https://tauri.localhost",
    ],
)
def test_api_version_options_preflight_succeeds_for_tauri(client, origin):
    r = client.options(
        "/api/version",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert r.status_code == 200, (
        f"OPTIONS preflight should return 200, got {r.status_code}; "
        f"a 405 here is the symptom of missing CORSMiddleware"
    )
    assert r.headers.get("access-control-allow-origin") == origin
