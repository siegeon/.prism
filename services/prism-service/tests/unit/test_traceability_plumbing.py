"""Red scaffold for the traceability-plumbing slice (conductor task 961f273b).

Pins the user-facing seams of "every entity clickable" so a false-green
cannot ship dead plumbing:

  A. xref ladder grows from 3 resolvable kinds to 8 (adds task/session/
     gate/test/retrieval) and POST /api/xref/resolve_batch — the REAL
     FastAPI seam the SPA calls — serves the new kinds (oracle clause 4).
  B. SPA gains /sessions/:id; session ids on TaskDetailPage and
     SessionsPage become links, not dead <span> text (misfire guard).
  C. claude_transcripts stops collapsing files_read/files_modified sets
     to counts: the actual path lists survive parse_session_metrics AND
     a real import_unseen -> sqlite round-trip (separate calls, real db),
     while the legacy count ints keep working (oracle clause 2).
  D. searches table gains session_id/task_id attribution columns and
     _log_search persists them, read back via get_recent_searches
     (oracle clause 3).
  E. RetrievalsPage links the asking session/task and renders final_top
     doc ids through XrefLink.
  F. okf_host memory evidence deep-links to /artifact?focus=<path>.

The web app has no JS test runner, so UI-FIRST criteria are pinned by
asserting the page SOURCE (established pattern: test_explore_narrative_ui).
Every test here FAILS today by design — this is the red step of the drive.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"

TASK_ID = "11111111-1111-4111-8111-111111111111"
SESSION_ID = "22222222-2222-4222-8222-222222222222"

ALL_EIGHT_KINDS = {
    "concept", "symbol", "file",
    "task", "session", "gate", "test", "retrieval",
}


def _page(rel: str) -> str:
    return (_WEB_SRC / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# A. xref ladder: 3 kinds -> 8, served through POST /api/xref/resolve_batch
# ---------------------------------------------------------------------------

def test_xref_declares_eight_resolvable_kinds():
    """The resolver publishes its kind contract — the set the SPA renderer
    and the gate rubric key off. 3 kinds today; must become 8."""
    from prism_service.api import xref
    kinds = set(getattr(xref, "RESOLVABLE_KINDS"))
    assert kinds == ALL_EIGHT_KINDS


class _NoMemory:
    def get_entry(self, token):
        return None

    def list_domains(self):
        return []

    def list_entries(self, domain):
        return []


class _NoBrain:
    def find_symbol(self, leaf, limit=10):
        return []

    def resolve_indexed_file(self, token):
        return None


class _FakeTaskSvc:
    def get_task(self, tid):
        if tid == TASK_ID:
            return {"id": TASK_ID, "title": "Sample traceable task"}
        return None


class _FakeConductorSvc:
    def get_session_outcomes(self, limit=500):
        return [{"session_id": SESSION_ID}]


def _fake_project():
    return SimpleNamespace(
        memory_svc=_NoMemory(),
        brain_svc=_NoBrain(),
        graph_svc=None,
        task_svc=_FakeTaskSvc(),
        conductor_svc=_FakeConductorSvc(),
    )


def test_resolve_batch_serves_task_and_session_kinds(monkeypatch):
    """Integration seam: the SPA's ONE batch request must resolve a task
    uuid to /tasks/:id and a session uuid to /sessions/:id. Today both
    fall off the 3-rung ladder as kind=unresolved."""
    testclient_mod = pytest.importorskip("fastapi.testclient")
    from prism_service.api import xref
    from prism_service.main import app

    monkeypatch.setattr(xref, "get_project", lambda p: _fake_project())
    client = testclient_mod.TestClient(app)
    r = client.post(
        "/api/xref/resolve_batch",
        json={"project": "prism", "tokens": [TASK_ID, SESSION_ID]},
    )
    assert r.status_code == 200
    res = r.json()["results"]
    assert res[TASK_ID]["kind"] == "task", res[TASK_ID]
    assert f"/tasks/{TASK_ID}" in (res[TASK_ID]["href"] or "")
    assert res[SESSION_ID]["kind"] == "session", res[SESSION_ID]
    assert f"/sessions/{SESSION_ID}" in (res[SESSION_ID]["href"] or "")


# ---------------------------------------------------------------------------
# B. /sessions/:id route + session ids become links (SPA source contract)
# ---------------------------------------------------------------------------

def test_app_routes_session_detail_page():
    """App.tsx must register /sessions/:id (pattern: /tasks/:id at :76)."""
    assert "/sessions/:id" in _page("App.tsx")


def test_session_detail_api_route_exists():
    """The SPA detail page needs a server seam: GET /api/sessions/{id}
    must exist on the REAL app (route table), not just a page stub."""
    from prism_service.main import app
    paths = {getattr(r, "path", "") for r in app.routes}
    assert any(
        p.startswith("/api/sessions/{") for p in paths
    ), f"no /api/sessions/{{session_id}} route; have: {sorted(paths)[:40]}"


def test_task_detail_session_id_is_a_link():
    """TaskDetailPage renders session_id as dead <span> text today
    (TaskDetailPage.tsx:890-892). It must become a /sessions/:id link."""
    assert "/sessions/${" in _page("pages/TaskDetailPage.tsx")


def test_sessions_page_session_id_links_to_detail():
    assert "/sessions/${" in _page("pages/SessionsPage.tsx")


# ---------------------------------------------------------------------------
# C. transcript file paths persisted, not collapsed to counts
# ---------------------------------------------------------------------------

def _write_transcript(dirpath: Path, session_id: str) -> Path:
    evts = [
        {
            "sessionId": session_id,
            "timestamp": "2026-07-13T00:00:00Z",
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read",
                 "input": {"file_path": "src/alpha.py"}},
                {"type": "tool_use", "name": "Edit",
                 "input": {"file_path": "src/beta.py"}},
            ]},
        },
        {
            "sessionId": session_id,
            "timestamp": "2026-07-13T00:05:00Z",
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                        "usage": {"output_tokens": 5}},
        },
    ]
    p = dirpath / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in evts), encoding="utf-8")
    return p


def test_parse_session_metrics_carries_path_lists(tmp_path):
    """parse_session_metrics collapses the path sets to len() today
    (claude_transcripts.py:395-396). The actual paths must survive the
    parse (field name free), while the legacy count stays an int so
    pre-existing session_outcomes consumers keep working."""
    from prism_service.services import claude_transcripts as ct
    m = ct.parse_session_metrics(_write_transcript(tmp_path, "sess-parse-1"))
    assert m is not None
    assert isinstance(m["files_read"], int)      # legacy count survives
    assert isinstance(m["files_modified"], int)  # legacy count survives
    blob = json.dumps(m, default=str)
    assert "src/alpha.py" in blob, "read paths collapsed to a count"
    assert "src/beta.py" in blob, "modified paths collapsed to a count"


def _brain(tmp_path):
    from prism_service.engines.brain_engine import Brain
    return Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )


def test_import_unseen_persists_file_paths(tmp_path):
    """Store seam across SEPARATE calls: import_unseen writes a fresh
    session into a real scores.db (schema from Brain() init) and the
    path strings must be findable in the db afterwards — any table/column
    layout is fine, silently dropping them is not."""
    from prism_service.services import claude_transcripts as ct
    _brain(tmp_path)  # creates session_outcomes schema in scores.db
    scores = tmp_path / "scores.db"
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    _write_transcript(tdir, "sess-import-1")
    n = ct.import_unseen(str(scores), "unused-source", override_dir=str(tdir))
    assert n == 1
    conn = sqlite3.connect(str(scores))
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        blob = "".join(
            repr(row)
            for t in tables
            for row in conn.execute(f'SELECT * FROM "{t}"')
        )
    finally:
        conn.close()
    assert "src/alpha.py" in blob, \
        "session file paths were discarded on import (counts only)"


# ---------------------------------------------------------------------------
# D. searches table: session/task attribution for every retrieval
# ---------------------------------------------------------------------------

def test_searches_table_has_attribution_columns(tmp_path):
    """brain_engine.py:892 CREATE TABLE searches has no session_id/task_id;
    the ALTER-if-missing migration pattern at :922+ must add them."""
    b = _brain(tmp_path)
    cols = {r[1] for r in b._brain.execute(
        "PRAGMA table_info(searches)").fetchall()}
    assert {"session_id", "task_id"} <= cols, f"searches cols: {sorted(cols)}"


def test_log_search_persists_asking_session_and_task(tmp_path):
    """_log_search must accept + persist the asking session/task, readable
    back through get_recent_searches (what /api/retrievals serves)."""
    b = _brain(tmp_path)
    sid = b._log_search(
        query="who asked", domain=None, domains=None, mode="hybrid",
        rerank="off", context_prefix=False, chunk_agg=False,
        limit_requested=5, results=[], latency_ms=1,
        session_id="sess-ask-1", task_id="task-ask-1",
    )
    assert sid is not None
    rows = b.get_recent_searches(limit=5)
    assert rows and rows[0].get("session_id") == "sess-ask-1"
    assert rows[0].get("task_id") == "task-ask-1"


# ---------------------------------------------------------------------------
# E. RetrievalsPage: link the asking session/task; doc ids -> XrefLink
# ---------------------------------------------------------------------------

def test_retrievals_page_links_session_task_and_docs():
    src = _page("pages/RetrievalsPage.tsx")
    assert "/sessions/${" in src, "retrieval row must link its asking session"
    assert "/tasks/${" in src, "retrieval row must link its asking task"
    assert "XrefLink" in src, \
        "final_top doc ids are plain <span> today; render through XrefLink"


# ---------------------------------------------------------------------------
# F. memory evidence deep-links to the artifact page
# ---------------------------------------------------------------------------

def test_memory_evidence_deep_links_to_artifact():
    """okf_host.py:48-51 renders every evidence path as a generic
    (/understand) link; it must deep-link /artifact?focus=<path> — the
    same surface the xref file rung resolves to (xref.py:154)."""
    from prism_service.services.okf_host import _memory_body
    entry = SimpleNamespace(summary="sum", description="desc")
    body = _memory_body(entry, {"file_paths": ["src/alpha.py"]})
    assert "/artifact?focus=src/alpha.py" in body
    assert "](/understand)" not in body, "generic /understand link remains"
