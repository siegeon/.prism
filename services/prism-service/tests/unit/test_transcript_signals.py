"""v6.0.5 — claude_transcripts signal extraction + consolidation bridge.

Covers:
  * parse_session_metrics() extracts pushbacks, background-protocol
    lines, tool failures, and memory-store call sites from a transcript
    JSONL — not just the count metrics it already had.
  * format_transcript_excerpt() renders signals into a bounded string
    safe to drop into a <untrusted>…</untrusted> wrap downstream.
  * import_unseen() goes through the consolidation bridge for every
    new session_outcome it inserts, passing the excerpt in scope_json
    so the reflection sub-agent gets real content instead of just
    aggregate counts.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import claude_transcripts as ct  # noqa: E402


def _evt(ts: str, role: str, content) -> dict:
    return {
        "sessionId": "test-session-1",
        "timestamp": ts,
        "message": {"role": role, "content": content},
    }


def _write_transcript(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "transcript.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return p


def test_parse_extracts_pushback_from_user_turn(tmp_path):
    events = [
        _evt("2026-05-25T12:00:00Z", "user", "no don't grep first, just read it"),
        _evt("2026-05-25T12:00:01Z", "assistant", [{"type": "text", "text": "ok"}]),
    ]
    out = ct.parse_session_metrics(_write_transcript(tmp_path, events))
    assert out is not None
    pushbacks = out["signals"]["pushbacks"]
    assert len(pushbacks) == 1
    assert "don" in pushbacks[0]["text"].lower()


def test_parse_extracts_background_protocol_lines(tmp_path):
    asst_text = (
        "doing the work now\n"
        "result: created the file at /tmp/x\n"
        "failed: build broke at step 3\n"
        "needs input: which branch should I push to?"
    )
    events = [
        _evt("2026-05-25T12:00:00Z", "user", "go"),
        _evt("2026-05-25T12:00:01Z", "assistant", [{"type": "text", "text": asst_text}]),
    ]
    out = ct.parse_session_metrics(_write_transcript(tmp_path, events))
    kinds = sorted(s["kind"] for s in out["signals"]["bg_signals"])
    assert kinds == ["failed", "needs_input", "result"]


def test_parse_extracts_tool_failures(tmp_path):
    events = [
        _evt("2026-05-25T12:00:00Z", "assistant", [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "git status"}},
        ]),
        _evt("2026-05-25T12:00:01Z", "user", [
            {"type": "tool_result", "tool_use_id": "t1",
             "is_error": True, "content": "fatal: not a git repository"},
        ]),
    ]
    out = ct.parse_session_metrics(_write_transcript(tmp_path, events))
    failures = out["signals"]["tool_failures"]
    assert len(failures) == 1
    assert "not a git" in failures[0]["error"]


def test_parse_extracts_memory_store_with_context(tmp_path):
    events = [
        _evt("2026-05-25T12:00:00Z", "user", "stop mocking the database in tests"),
        _evt("2026-05-25T12:00:01Z", "assistant", [
            {"type": "text", "text": "saving feedback memory"},
            {"type": "tool_use", "id": "t1",
             "name": "mcp__prism__memory_store",
             "input": {"domain": "feedback", "name": "never-mock-db",
                       "description": "Integration tests must hit a real DB."}},
        ]),
    ]
    out = ct.parse_session_metrics(_write_transcript(tmp_path, events))
    writes = out["signals"]["memory_writes"]
    assert len(writes) == 1
    assert writes[0]["domain"] == "feedback"
    assert writes[0]["name"] == "never-mock-db"
    roles = [c["role"] for c in writes[0]["context"]]
    assert "user" in roles


def test_format_transcript_excerpt_bounds_size(tmp_path):
    signals = {
        "pushbacks": [{"ts": f"t{i}", "text": "x" * 600} for i in range(50)],
        "bg_signals": [],
        "tool_failures": [],
        "memory_writes": [],
    }
    out = ct.format_transcript_excerpt(signals)
    assert len(out) <= ct._EXCERPT_MAX_CHARS + 32  # +truncation marker slack
    assert "(truncated)" in out


def test_format_transcript_excerpt_empty_signals():
    assert ct.format_transcript_excerpt({}) == ""


def test_import_unseen_enqueues_consolidation_with_excerpt(tmp_path, monkeypatch):
    """The disk-reader path must reach the consolidation bridge — earlier
    versions inserted session_outcomes via raw SQL and silently bypassed
    enqueue_for_session, so transcript-derived sessions never produced
    candidates."""
    from prism_service.engines.brain_engine import Brain

    # Bootstrap real schema (session_outcomes, skill_usage,
    # consolidation_candidates) without keeping the Brain alive.
    scores_db = str(tmp_path / "scores.db")
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=scores_db,
    )

    # Synthetic Claude home with a single transcript whose slug matches
    # the project_source_path we'll pass into import_unseen.
    claude_home = tmp_path / "claude"
    project_path = tmp_path / "my_proj"
    project_path.mkdir()
    slug = ct.path_to_slug(str(project_path))
    proj_dir = claude_home / "projects" / slug
    proj_dir.mkdir(parents=True)
    events = [
        _evt("2026-05-25T12:00:00Z", "user", "stop, that's wrong"),
        _evt("2026-05-25T12:00:01Z", "assistant", [
            {"type": "text", "text": "result: fixed it"},
        ]),
    ]
    tpath = proj_dir / "abc.jsonl"
    with tpath.open("w", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")

    n = ct.import_unseen(scores_db, str(project_path), claude_home=claude_home)
    assert n == 1

    conn = sqlite3.connect(scores_db)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT trigger, scope_json FROM consolidation_candidates "
            "WHERE session_id = ?",
            ("test-session-1",),
        ).fetchall()
    finally:
        conn.close()
    assert len(rows) == 1, "consolidation candidate must be enqueued"
    assert rows[0]["trigger"] == "transcript_imported"
    scope = json.loads(rows[0]["scope_json"])
    assert "transcript_excerpt" in scope
    assert "Pushbacks" in scope["transcript_excerpt"]
    assert "result:" in scope["transcript_excerpt"]
    assert scope["signal_counts"]["pushbacks"] == 1
    assert scope["signal_counts"]["bg_signals"] == 1
