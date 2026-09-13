"""Session file-set resolution must be CACHED, not re-walked on every call
(external fixer perf pass, owner brief 2026-09-13 "graphtx").

Live symptom: GET /api/work/graph?project=prism answered in ~0.46s on
EVERY warm poll (never faster), while every other route answered under
30ms. Root cause: live_spend_for_session and live_token_events_for_session
each independently re-walked every project directory under
~/.claude/projects (440 on the live instance) plus rglob'd the session's
own subagent directory, from scratch, on EVERY single call -- the per-FILE
content was already cached by (mtime, size), but the WALK that finds which
files exist was not cached at all, so the wall-clock cost never dropped
even on a fully warm corpus.

This pins the fix: `_cached_session_files` must call the real walk
(`_scan_session_files`) AT MOST ONCE across several calls for the same
session inside the cache TTL, and both public entry points
(live_spend_for_session, live_token_events_for_session) must share that
one cached walk rather than each doing their own."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_cached_session_files_walks_at_most_once_within_ttl(monkeypatch):
    from prism_service.services import claude_transcripts as ct

    calls = {"n": 0}

    def counting_scan(session_id, project_path, claude_home=None, override_dir=None):
        calls["n"] += 1
        return ([Path(f"/fake/{session_id}.jsonl")], [])

    monkeypatch.setattr(ct, "_scan_session_files", counting_scan)
    monkeypatch.setattr(ct, "_SESSION_FILES_CACHE", {})

    for _ in range(5):
        main_paths, bg_paths = ct._cached_session_files("sess-1", "prism")
        assert main_paths == [Path("/fake/sess-1.jsonl")], f"got {main_paths!r}"
        assert bg_paths == [], f"got {bg_paths!r}"

    assert calls["n"] == 1, (
        f"_scan_session_files was invoked {calls['n']} times across 5 calls "
        "for the same session inside the cache TTL -- expected exactly 1: "
        "a directory walk redone on every call is the live perf defect "
        "(GET /api/work/graph answering ~0.46s on every warm poll, never "
        "dropping, because the walk was never cached)"
    )


def test_live_spend_and_token_events_share_the_cached_walk(monkeypatch):
    # Both public entry points must go through the SAME cache -- a fix that
    # only wired one of them would leave the other's walk unbounded, and
    # /api/work/graph calls both (the spend loop, then the session/
    # token-motion loop) for the SAME session on every single poll.
    from prism_service.services import claude_transcripts as ct

    calls = {"n": 0}

    def counting_scan(session_id, project_path, claude_home=None, override_dir=None):
        calls["n"] += 1
        return ([], [])

    monkeypatch.setattr(ct, "_scan_session_files", counting_scan)
    monkeypatch.setattr(ct, "_SESSION_FILES_CACHE", {})

    ct.live_spend_for_session("sess-2", "prism")
    ct.live_token_events_for_session("sess-2", "prism")

    assert calls["n"] == 1, (
        f"expected live_spend_for_session and live_token_events_for_session "
        f"to share one cached walk per session; got {calls['n']} separate "
        "walks -- a request that calls both (as /api/work/graph does) would "
        "still pay the walk cost twice"
    )
