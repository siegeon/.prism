"""Evidence-file serving resolves a task across projects — owner 2026-07-20.

A completion_proof image renders as a RAW <img src="/api/tasks/<id>/evidence/
x.png"> that bypasses the SPA api-client, so the browser requests it with NO
?project= param. The route defaults project to 'default'; when the task lives in
another project (e.g. dev's 'prism') the old code 404'd -> BROKEN visual
evidence. The evidence dir is keyed by the globally-unique task_id, not the
project, so the route now resolves the task across projects.

  * no project param + task in a non-default project -> 200 (file served)
  * a genuinely unknown task -> still 404 (no silent over-serving)
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TID = "d381f259abcd"
_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 200


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api
    from prism_service import config as cfg

    class _Svc:
        def __init__(self, tid):
            self._tid = tid

        def get(self, t):
            return object() if t == self._tid else None

    # The task exists ONLY in project 'prism'.
    def _get_project(p):
        return types.SimpleNamespace(
            task_svc=_Svc(_TID if p == "prism" else "__absent__"))

    monkeypatch.setattr(tasks_api, "get_project", _get_project)
    monkeypatch.setattr(cfg, "list_projects", lambda: ["default", "prism"])
    monkeypatch.setattr(tasks_api, "evidence_dir", lambda t: tmp_path / t)

    d = tmp_path / _TID
    d.mkdir(parents=True, exist_ok=True)
    (d / "browser_oracle.png").write_bytes(_PNG)

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_evidence_served_without_project_param(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    # Exactly how the raw <img> requests it: NO ?project= -> defaults to
    # 'default', where the task is absent -> must resolve to 'prism' and serve.
    r = client.get(f"/api/tasks/{_TID}/evidence/browser_oracle.png")
    assert r.status_code == 200, r.text
    assert r.content.startswith(b"\x89PNG")


def test_unknown_task_still_404s(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/tasks/nope123/evidence/browser_oracle.png")
    assert r.status_code == 404
