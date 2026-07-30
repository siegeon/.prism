"""Captured videos can actually be watched.

The evidence route `GET /api/tasks/{task_id}/evidence/{filename}` validates
`filename` with a whitelist that excludes '@'. PRISM's own browser-oracle
runner names every recording `page@<hash>.webm`, so every recording it
captures 400s at the guard before the disk is ever touched — measured 6 of 6
real on-disk recordings return HTTP 400 while a control `screenshot.png` in
the same directories returns 200. The fix widens the whitelist to admit '@'
without loosening the traversal guard, and confirms `.webm` serves with a
playable video content type.

Fixture filenames use a real hash captured from a live recording
(`7bdff5da352bb3730965b0899cdd195e`) so the test proves the fix against the
exact pattern the runner emits, not a relabeled stand-in (AC-4) — no rename
step anywhere in this file or in the implementation.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TID = "e7b7b7b7b7b7"
_REAL_HASH = "7bdff5da352bb3730965b0899cdd195e"
_WEBM_NAME = f"page@{_REAL_HASH}.webm"
_WEBM_BYTES = b"\x1aE\xdf\xa3" + b"0" * 200  # EBML/webm magic + filler


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import tasks as tasks_api

    class _Svc:
        def get(self, t):
            return object() if t == _TID else None

    monkeypatch.setattr(tasks_api, "_svc", lambda project: _Svc())
    monkeypatch.setattr(tasks_api, "evidence_dir", lambda t: tmp_path / t)

    d = tmp_path / _TID
    d.mkdir(parents=True, exist_ok=True)
    # The exact filename the browser-oracle runner already wrote to disk for
    # this recording — proving the fix without any rename (AC-4).
    (d / _WEBM_NAME).write_bytes(_WEBM_BYTES)

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_existing_at_recording_is_served_as_video(tmp_path, monkeypatch):
    # AC-1: a real page@<hash>.webm recording already on disk must resolve
    # past the filename guard and be served with a playable video type.
    client = _client(tmp_path, monkeypatch)
    r = client.get(f"/api/tasks/{_TID}/evidence/{_WEBM_NAME}")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("video/"), r.headers
    assert r.content == _WEBM_BYTES
    # Playable inline, not forced to download — no attachment disposition.
    assert "attachment" not in r.headers.get("content-disposition", "")


def test_traversal_guard_still_rejects_dotdot_and_separators(tmp_path, monkeypatch):
    # AC-2: widening the class to admit '@' must not open the traversal
    # guard back up. Both '..' and an embedded path separator still 400.
    client = _client(tmp_path, monkeypatch)
    r = client.get(f"/api/tasks/{_TID}/evidence/..%2F..%2Fetc%2Fpasswd")
    assert r.status_code in (400, 404), r.text
    r2 = client.get(f"/api/tasks/{_TID}/evidence/a%2Fb.webm")
    assert r2.status_code in (400, 404), r2.text
    # Neither traversal attempt may ever serve the real recording's bytes.
    assert r.content != _WEBM_BYTES
    assert r2.content != _WEBM_BYTES


def test_wellformed_but_absent_filename_still_404s(tmp_path, monkeypatch):
    # AC-3: a filename that clears the whitelist (including '@') but isn't
    # actually present in this task's evidence dir must still 404, not be
    # invented or served from elsewhere.
    client = _client(tmp_path, monkeypatch)
    absent = "page@deadbeef00000000000000000000.webm"
    r = client.get(f"/api/tasks/{_TID}/evidence/{absent}")
    assert r.status_code == 404, r.text
