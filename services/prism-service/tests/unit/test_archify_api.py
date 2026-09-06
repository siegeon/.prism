"""Tests for archify API endpoints."""

from __future__ import annotations

import json
import shutil
import sys
import types
from pathlib import Path

import pytest

# Add service root to path for imports
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# The tests below reload prism_service.config to repoint DATA_DIR at a tmp
# dir. A reload mutates the module for the WHOLE session, and monkeypatch
# cannot undo it — that leaked the tmp dir into every later test and turned
# test_data_dir_isolation and the packaging/promotion suites red. This runs
# first, so its teardown runs LAST: after monkeypatch restores the ambient
# PRISM_DATA_DIR the conftest pinned, reload config once more so the module
# agrees with the environment again.
@pytest.fixture(autouse=True)
def _restore_config_module_after_reload():
    yield
    import importlib
    from prism_service import config as _cfg
    importlib.reload(_cfg)


def _make_client(tmp_path, monkeypatch):
    """Create a TestClient with archify router and mocked project context."""
    if shutil.which("node") is None:
        return None

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import archify as archify_api
    from prism_service import config as cfg

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    # Force config re-import
    import importlib
    importlib.reload(cfg)

    class _Project:
        pass

    def _get_project(p):
        if p != "default":
            raise ValueError(f"unknown project: {p}")
        return _Project()

    monkeypatch.setattr(archify_api, "get_project", _get_project)

    app = FastAPI()
    app.include_router(archify_api.router, prefix="/api/archify")
    return TestClient(app)


def test_get_maps_empty(tmp_path, monkeypatch):
    """Test GET /api/archify/maps returns empty list."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps?project=default")
    assert r.status_code == 200
    data = r.json()
    assert "maps" in data
    assert data["maps"] == []


def test_get_maps_unknown_project_404(tmp_path, monkeypatch):
    """Test GET /api/archify/maps returns 404 for unknown project."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps?project=unknown")
    assert r.status_code == 404


def test_doctor(tmp_path, monkeypatch):
    """Test GET /api/archify/doctor."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/doctor?project=default")
    assert r.status_code == 200
    data = r.json()
    assert "ok" in data
    assert "node" in data
    assert "output" in data


def test_build_unknown_kind_400(tmp_path, monkeypatch):
    """Test POST /api/archify/maps/{kind}/build returns 400 for unknown kind."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.post("/api/archify/maps/invalid/build?project=default")
    assert r.status_code == 400


def test_build_task_without_task_id_400(tmp_path, monkeypatch):
    """Test POST /api/archify/maps/task/build requires task_id."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.post("/api/archify/maps/task/build?project=default")
    assert r.status_code == 400


def test_get_map_unknown_kind_400(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/{kind} returns 400 for unknown kind."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/invalid?project=default")
    assert r.status_code == 400


def test_get_map_task_without_task_id_400(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/task requires task_id."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/task?project=default")
    assert r.status_code == 400


def test_get_map_not_found_404(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/{kind} returns 404 when map not built."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/code?project=default")
    assert r.status_code == 404


def test_get_map_html_not_found_404(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/{kind}/html returns 404 when map not built."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/code/html?project=default")
    assert r.status_code == 404


def test_get_map_html_iframe_safe(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/{kind}/html returns iframe-safe response."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    # Write a test html file
    from prism_service import config
    import importlib
    cfg = sys.modules["prism_service.config"]
    importlib.reload(cfg)

    from prism_service.services.archify_service import ArchifyService
    svc = ArchifyService("default")
    code_dir = svc.map_dir("code")
    (code_dir / "map.html").write_text("<svg></svg>")
    (code_dir / "meta.json").write_text(json.dumps({
        "kind": "code",
        "ok": True,
        "diagram_type": "architecture"
    }))

    r = client.get("/api/archify/maps/code/html?project=default")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "text/html; charset=utf-8"
    assert "no-cache" in r.headers.get("cache-control", "")
    # Should NOT have X-Frame-Options header
    assert "x-frame-options" not in r.headers
    assert "<svg>" in r.text


def test_get_map_ir_not_found_404(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/{kind}/ir returns 404 when map not built."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/code/ir?project=default")
    assert r.status_code == 404


def test_get_map_receipt_not_found_404(tmp_path, monkeypatch):
    """Test GET /api/archify/maps/{kind}/receipt returns 404 when not built."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/code/receipt?project=default")
    assert r.status_code == 404


def test_get_map_unknown_project_404(tmp_path, monkeypatch):
    """Test endpoints return 404 for unknown project."""
    client = _make_client(tmp_path, monkeypatch)
    if client is None:
        return

    r = client.get("/api/archify/maps/code?project=unknown")
    assert r.status_code == 404

    r = client.get("/api/archify/maps/code/html?project=unknown")
    assert r.status_code == 404

    r = client.get("/api/archify/maps/code/ir?project=unknown")
    assert r.status_code == 404

    r = client.get("/api/archify/maps/code/receipt?project=unknown")
    assert r.status_code == 404

    r = client.post("/api/archify/maps/code/build?project=unknown")
    assert r.status_code == 404
