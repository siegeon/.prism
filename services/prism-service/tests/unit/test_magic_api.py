"""RED suite for the /api/magic router (task 0a5607a4).

Drives the REAL router through a FastAPI TestClient with the
magic_client service monkeypatched at its seams. Also pins the
router's registration on the shared api_router and the
no-secret-in-any-response contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_service import config


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("PRISM_MAGIC_URL", raising=False)
    monkeypatch.delenv("PRISM_MAGIC_USER", raising=False)
    monkeypatch.delenv("PRISM_MAGIC_PASSWORD", raising=False)
    import importlib
    from prism_service.services import magic_client as mc_mod
    importlib.reload(mc_mod)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import magic as magic_api
    app = FastAPI()
    app.include_router(magic_api.router, prefix="/api/magic")
    yield TestClient(app)
    importlib.reload(mc_mod)


def test_registered_on_shared_api_router():
    # Probe via the OpenAPI schema — FastAPI >= 0.138 defers
    # include_router() into _IncludedRouter objects with no .path, so
    # the schema is the version-agnostic "actually mounted" contract
    # (same idiom as test_api_agent_runs).
    from fastapi import FastAPI
    from prism_service.api import api_router
    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/magic/status" in paths


def test_status_unconfigured(client):
    r = client.get("/api/magic/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["fingerprint"] == ""


def test_configure_validates_then_persists(client, monkeypatch):
    from prism_service.services import magic_client as mc
    monkeypatch.setattr(mc, "authenticate", lambda url, user, pw: "jwt-ok")
    r = client.post("/api/magic/configure", json={
        "url": "http://magic:4444", "user": "root", "password": "supersecret99"})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert "•••" in body["fingerprint"]
    assert "supersecret99" not in r.text


def test_configure_rejects_bad_credentials(client, monkeypatch):
    from prism_service.services import magic_client as mc

    def boom(url, user, pw):
        raise mc.MagicError("magic http 401: bad credentials", status=401)

    monkeypatch.setattr(mc, "authenticate", boom)
    r = client.post("/api/magic/configure", json={
        "url": "http://magic:4444", "user": "root", "password": "bad"})
    assert r.status_code in (400, 502)
    assert "401" in str(r.json().get("detail", ""))


def test_clear_removes_connection(client, monkeypatch):
    from prism_service.services import magic_client as mc
    monkeypatch.setattr(mc, "authenticate", lambda *a: "jwt-ok")
    client.post("/api/magic/configure", json={
        "url": "http://magic:4444", "user": "root", "password": "pw"})
    r = client.post("/api/magic/clear")
    assert r.status_code == 200
    assert r.json()["configured"] is False
    assert client.get("/api/magic/status").json()["configured"] is False


def test_execute_proxies_to_client(client, monkeypatch):
    from prism_service.services import magic_client as mc
    seen = {}

    def fake_execute(hl):
        seen["hl"] = hl
        return {"result": "ok!"}

    monkeypatch.setattr(mc, "execute", fake_execute)
    r = client.post("/api/magic/execute", json={"hyperlambda": 'log.info:"x"'})
    assert r.status_code == 200
    assert r.json() == {"result": "ok!"}
    assert seen["hl"] == 'log.info:"x"'


def test_execute_maps_magic_error_to_502(client, monkeypatch):
    from prism_service.services import magic_client as mc

    def boom(hl):
        raise mc.MagicError("magic http 500: kaboom", status=500)

    monkeypatch.setattr(mc, "execute", boom)
    r = client.post("/api/magic/execute", json={"hyperlambda": "x"})
    assert r.status_code == 502


def test_endpoints_proxies(client, monkeypatch):
    from prism_service.services import magic_client as mc
    monkeypatch.setattr(mc, "endpoints", lambda: [{"path": "magic/x", "verb": "get"}])
    r = client.get("/api/magic/endpoints")
    assert r.status_code == 200
    assert r.json()[0]["path"] == "magic/x"


def test_setup_bootstraps(client, monkeypatch):
    from prism_service.services import magic_client as mc
    seen = {}

    def fake_bootstrap(url, password, name="", email=""):
        seen.update(url=url, password=password)
        return {"configured": True, "already_configured": False}

    monkeypatch.setattr(mc, "bootstrap", fake_bootstrap)
    r = client.post("/api/magic/setup", json={
        "url": "http://magic:4444", "password": "npw", "name": "op", "email": "e@x.io"})
    assert r.status_code == 200
    assert r.json()["configured"] is True
    assert seen == {"url": "http://magic:4444", "password": "npw"}
