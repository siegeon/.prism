"""RED scaffold — /api/memory/claude-config endpoints + poller override
wiring (task b6650506).

Pins the USER-FACING integration end-to-end:

  * GET /api/memory/claude-config returns the persisted claude_project_dir
    (and distinguishes auto vs explicit) through the REAL memory router.
  * POST /api/memory/claude-config writes it via the SAME state writer and
    the value survives a SEPARATE GET.
  * import_unseen reads from the persisted override_dir and populates
    session_outcomes even when path_to_slug does NOT match the PRISM
    project source path (the auto-discovery miss).
  * the 60s poller resolves override_dir from the persisted value via
    claude_memory.configured_project_dir — today claude_memory is absent,
    so the poller's `from ... import claude_memory` fails -> cm=None ->
    override never fires.

All FAIL today: the route 404s, claude_memory.py is absent, and nothing
persists/reads claude_project_dir.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service import config
from prism_service.engines import understand_engine as ue
from prism_service.services import claude_transcripts as ct


def _make_scores_db(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE session_outcomes ("
        "session_id TEXT PRIMARY KEY, duration_s REAL, tokens_used INTEGER, "
        "files_read INTEGER, files_modified INTEGER, skills_invoked INTEGER, "
        "timestamp TEXT)"
    )
    conn.commit()
    conn.close()
    return str(path)


def _write_transcript(dir_: Path, name: str, session_id: str) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({
            "sessionId": session_id,
            "type": "user",
            "timestamp": "2026-06-01T10:00:00Z",
            "message": {"role": "user", "content": "hi"},
        }),
        json.dumps({
            "sessionId": session_id,
            "type": "assistant",
            "timestamp": "2026-06-01T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": "ok",
                "usage": {"output_tokens": 11},
            },
        }),
    ]
    (dir_ / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def isolated_projects(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    config.project_data_dir("proj-cc")
    return tmp_path


# ---------------------------------------------------------------------------
# import_unseen honors the override_dir on a NON-matching slug
# ---------------------------------------------------------------------------

def test_import_unseen_reads_override_dir_on_slug_mismatch(
    isolated_projects, tmp_path
):
    """The whole point of the escape hatch: the registered dir's slug does
    NOT match the PRISM project source path, yet the sessions import."""
    scores = _make_scores_db(tmp_path / "scores.db")
    override = tmp_path / "fake_claude" / "projects" / "C--Users-x-other"
    _write_transcript(override, "a.jsonl", "sess-override-1")
    _write_transcript(override, "b.jsonl", "sess-override-2")

    n = ct.import_unseen(
        scores,
        project_source_path="E:/totally/different/path",
        override_dir=str(override),
    )
    assert n == 2, "both override transcripts must import"

    conn = sqlite3.connect(scores)
    ids = {r[0] for r in conn.execute("SELECT session_id FROM session_outcomes")}
    conn.close()
    assert {"sess-override-1", "sess-override-2"} <= ids


# ---------------------------------------------------------------------------
# Poller passes the PERSISTED override_dir into import_unseen for EVERY proj
# ---------------------------------------------------------------------------

def test_poller_passes_persisted_override_into_import_unseen(
    isolated_projects, tmp_path, monkeypatch
):
    """One pass of the importer body must resolve override_dir from the
    persisted claude_project_dir (via claude_memory.configured_project_dir)
    and feed it to import_unseen. Today claude_memory is absent so the
    import fails -> cm=None -> override_dir stays None."""
    from prism_service.services import claude_memory as cm  # red: ModuleNotFound today

    # Persist a claude_project_dir for proj-cc via the same writer the tool uses.
    override = tmp_path / "reported_dir"
    override.mkdir()
    (override / "z.jsonl").write_text("{}\n", encoding="utf-8")
    state = ue._read_state("proj-cc")
    state["claude_project_dir"] = str(override)
    ue._write_state("proj-cc", state)

    captured: dict = {}

    def _spy_import_unseen(scores_db, source_path, claude_home=None, override_dir=None):
        captured["override_dir"] = override_dir
        return 0

    monkeypatch.setattr(ct, "import_unseen", _spy_import_unseen)

    # The reader the poller uses must return the persisted dir.
    assert cm.configured_project_dir("proj-cc") == str(override)


# ---------------------------------------------------------------------------
# GET/POST /api/memory/claude-config round trip (separate calls)
# ---------------------------------------------------------------------------

def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import memory as memory_api

    class _Ctx:
        memory_svc = None

    monkeypatch.setattr(memory_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(memory_api.router, prefix="/api/memory")
    return TestClient(app)


def test_post_then_get_claude_config_round_trips(isolated_projects, tmp_path, monkeypatch):
    client = _client(monkeypatch)
    target = str(tmp_path / "fake_claude" / "projects" / "C--x")

    post = client.post(
        "/api/memory/claude-config",
        params={"project": "proj-cc"},
        json={"claude_project_dir": target},
    )
    assert post.status_code == 200, post.text

    # Separate GET — value must have persisted to understand_state.json.
    get = client.get("/api/memory/claude-config", params={"project": "proj-cc"})
    assert get.status_code == 200, get.text
    body = get.json()
    assert body["claude_project_dir"] == target
    # Explicit (reported) vs auto (slug) is distinguished for the UI.
    assert body.get("source") == "explicit"

    # Confirm it landed via the canonical reader, not just an echo.
    assert ue._read_state("proj-cc").get("claude_project_dir") == target


def test_get_claude_config_unset_reports_auto(isolated_projects, monkeypatch):
    client = _client(monkeypatch)
    get = client.get("/api/memory/claude-config", params={"project": "proj-cc"})
    assert get.status_code == 200, get.text
    body = get.json()
    # Unconfigured -> auto (slug) discovery mode, no explicit dir.
    assert body.get("source") == "auto"
    assert not body.get("claude_project_dir")
