"""RED scaffold — logs and diffs are first-class evidence (task 939756eb).

Today a task's evidence store accepts ONLY images/video (_EVIDENCE_MEDIA,
api/tasks.py), so a test log or a diff cannot be attached at all: the list route
skips it and the fetch route 400s it. The only way to show a passing test run
was to render a terminal to a PNG.

These pin: text artifacts are listed, served 200 with a NON-EXECUTABLE
text-family content type (never text/html), images/video keep working, and an
unknown or dangerous extension is still refused.

Imports live INSIDE the fixture/tests so the file collects and fails at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"

LOG_BODY = "============ 10 passed in 0.97s ============\nEXIT=0\n"


@pytest.fixture
def ev(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    from prism_service import config, project_context
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    project_context._contexts.clear()

    from prism_service.api import tasks as tasks_api
    from prism_service.data_dir import evidence_dir

    ctx = project_context.get_project("evproj")
    task = ctx.task_svc.create(title="evidence text task")

    d = evidence_dir(task.id)
    (d / "verify_pytest.txt").write_text(LOG_BODY, encoding="utf-8")
    (d / "server.log").write_text("boot ok\n", encoding="utf-8")
    (d / "change.diff").write_text("--- a\n+++ b\n", encoding="utf-8")
    (d / "notes.md").write_text("# heading\n", encoding="utf-8")
    (d / "payload.json").write_text('{"ok": true}\n', encoding="utf-8")
    (d / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
    # Must STAY refused — an executable/unknown type is not evidence.
    (d / "evil.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (d / "thing.exe").write_bytes(b"MZ")

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    with TestClient(app) as client:
        yield {"client": client, "task_id": task.id}
    project_context._contexts.clear()


def _get(ev, name):
    return ev["client"].get(
        f"/api/tasks/{ev['task_id']}/evidence/{name}", params={"project": "evproj"})


# ── text artifacts are served, and served SAFELY ───────────────────────

@pytest.mark.parametrize("name", ["verify_pytest.txt", "server.log",
                                  "change.diff", "notes.md", "payload.json"])
def test_text_evidence_is_served(ev, name):
    resp = _get(ev, name)
    assert resp.status_code == 200, f"{name} must be attachable evidence"
    ctype = resp.headers.get("content-type", "")
    assert ("text/" in ctype) or ("json" in ctype), ctype
    assert "text/html" not in ctype, (
        "never serve stored evidence as html — that is stored XSS")


def test_the_log_body_is_readable_verbatim(ev):
    resp = _get(ev, "verify_pytest.txt")
    assert "10 passed" in resp.text and "EXIT=0" in resp.text


# ── images/video keep working; dangerous types stay refused ────────────

def test_images_still_work(ev):
    resp = _get(ev, "shot.png")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("image/png")


@pytest.mark.parametrize("name", ["evil.html", "thing.exe"])
def test_executable_or_unknown_types_are_still_refused(ev, name):
    assert _get(ev, name).status_code == 400, (
        f"{name} must not be servable from the evidence store")


# ── the listing surfaces text so the SPA can render it ─────────────────

def test_listing_includes_text_artifacts_with_a_kind(ev):
    listed = ev["client"].get(
        f"/api/tasks/{ev['task_id']}/evidence", params={"project": "evproj"})
    assert listed.status_code == 200
    rows = {r["name"]: r for r in listed.json().get("files", [])}
    assert "verify_pytest.txt" in rows, "a log must appear in the evidence list"
    assert rows["verify_pytest.txt"].get("kind") == "text"
    assert rows["shot.png"].get("kind") == "image"
    assert "evil.html" not in rows and "thing.exe" not in rows


# ── the SPA renders it as readable text, not a download ────────────────

def test_evidence_view_renders_text_inline():
    src = (_WEB / "components" / "EvidenceView.tsx").read_text(encoding="utf-8")
    assert ".txt" in src or ".log" in src or "text" in src.lower(), (
        "EvidenceView must handle text evidence")
    assert "<pre" in src, "a log must render as readable preformatted text"
