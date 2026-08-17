"""The board polls at a human cadence (task fb163fcf).

The 5s /api/tasks poll re-transferred the full ~117KB projection on every
tick of an idle board (~468KB per 20s window). The fix is a CONDITIONAL
request, not a slower clock: the list endpoint stamps a content ETag +
Cache-Control: no-cache, so the browser revalidates every 5s and an
unchanged board costs a ~300-byte 304 — while ANY change hashes to a new
ETag and the very next poll carries the full fresh payload. Cadence is
untouched, so in-flight work stays exactly as visible as before (the
owner rejected staleness-for-bytes twice). TasksPage additionally skips
polls while the tab is hidden and refetches on visibilitychange.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_FIELDS = "id,title,status,priority,updated_at"


def _mk_task(**over):
    from prism_service.models.task import Task
    base = dict(
        id="t-1", title="A task", description="", status="pending",
        priority=5, assigned_agent="", updated_at="2026-08-07T00:00:00Z",
        workflow_step="implement", gate_state="none", parent_id="",
        tags=[],
    )
    base.update(over)
    return Task(**base)


class _Svc:
    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _client(svc, monkeypatch, data_dir):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    monkeypatch.setenv("PRISM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(tasks_api, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=svc))
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_first_get_carries_etag_and_no_cache(tmp_path, monkeypatch):
    client = _client(_Svc([_mk_task()]), monkeypatch, tmp_path / "data")
    r = client.get(f"/api/tasks?project=prism&fields={_FIELDS}")
    assert r.status_code == 200, r.text
    assert r.headers.get("etag"), "board list must stamp a content ETag"
    assert "no-cache" in (r.headers.get("cache-control") or ""), (
        "no-cache (revalidate every poll) is what keeps the board NEVER "
        "stale while unchanged polls stay ~300 bytes")


def test_unchanged_poll_is_a_304_with_no_body(tmp_path, monkeypatch):
    client = _client(_Svc([_mk_task()]), monkeypatch, tmp_path / "data")
    url = f"/api/tasks?project=prism&fields={_FIELDS}"
    first = client.get(url)
    etag = first.headers["etag"]
    again = client.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304, (
        "an unchanged board re-transferred the full payload — the whole "
        f"idle-cadence cost this task removes; got {again.status_code}")
    assert not again.content, "a 304 must carry no body"


def test_changed_board_returns_full_fresh_payload(tmp_path, monkeypatch):
    svc = _Svc([_mk_task()])
    client = _client(svc, monkeypatch, tmp_path / "data")
    url = f"/api/tasks?project=prism&fields={_FIELDS}"
    etag = client.get(url).headers["etag"]
    svc.tasks.append(_mk_task(id="t-2", title="Claimed while you watch"))
    r = client.get(url, headers={"If-None-Match": etag})
    assert r.status_code == 200, (
        "a CHANGED board must never 304 — that would be the staleness "
        "the owner rejected twice")
    ids = [t["id"] for t in r.json()["tasks"]]
    assert "t-2" in ids
    assert r.headers["etag"] != etag


def test_tasks_page_skips_hidden_tab_and_refetches_on_visibility():
    src = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
           / "TasksPage.tsx").read_text(encoding="utf-8")
    assert "document.hidden" in src, (
        "TasksPage must skip polls while the tab is hidden")
    assert "visibilitychange" in src, (
        "TasksPage must refetch the moment the tab becomes visible again")
