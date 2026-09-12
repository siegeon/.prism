"""GET /api/workflows/live answers the tiny 1-second live channel (see
test_workflow_live_fast_channel.py for the function's own behaviour) --
this pins that the route is actually wired and answers the right shape."""
from __future__ import annotations

import types

from fastapi.testclient import TestClient

import prism_service.main as main_module
from prism_service.services import workflow_live


def test_the_live_route_answers_the_channel_shape(monkeypatch):
    proj = types.SimpleNamespace(task_svc=types.SimpleNamespace(
        list=lambda **kw: [], _db_path=""), _data_dir=None)
    monkeypatch.setattr(workflow_live, "get_project", lambda p: proj)

    client = TestClient(main_module.app)
    resp = client.get("/api/workflows/live?project=doesnotexist")

    assert resp.status_code == 200
    body = resp.json()
    assert "ts" in body
    assert "entries" in body
    assert "conductor" in body
    assert "occupancy" in body["conductor"]
    assert "live" in body["conductor"]
