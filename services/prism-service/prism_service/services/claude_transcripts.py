"""Claude Code transcript reader (v5.3.15).

Claude Code persists every session as a JSONL stream at
`~/.claude/projects/<dir-slug>/<session-uuid>.jsonl`, where the slug is
the working-directory path with `:`, `/`, `\\`, and `.` replaced by `-`.
Each line is an event: user prompts, assistant turns (with usage),
tool uses, attachments, snapshots, etc.

This module reads those transcripts directly and populates the same
`session_outcomes` table the Stop hook used to write to. Result: a
disk-reader path that gives us Sessions / Consolidation data **without
depending on the Stop hook at all**. Works retroactively (backfills the
user's existing history) and cross-platform (Linux/macOS read the same
files at `$HOME/.claude/projects/`).

Hooks still useful for things that genuinely need to happen mid-session
(SessionStart sync, PostToolUse-Edit-to-Brain reingest); those are
narrower and harder to break.

Public surface:
  * `import_unseen(scores_db, project_source_path, claude_home=None) -> int`
    — parse every transcript whose slug matches the project, insert any
    session not already in `session_outcomes`, return the count imported.
  * `start_transcript_importer()` — daemon-loop driver; polls every
    PRISM_TRANSCRIPT_POLL_S (default 60s). Iterates all PRISM projects.
  * `path_to_slug(path)` / `slug_matches(slug, project_path)` — helpers.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from prism_service.data_dir import resolve_claude_home
from prism_service.project_context import get_all_projects, get_project


_SLUG_RE = re.compile(r"[:/\\.]")


def path_to_slug(path: str) -> str:
    """Convert a filesystem path to Claude Code's project-slug form.

    Example: `E:\\.prism` -> `E---prism`. Replaces colon, both slashes,
    and dot. Trailing separators are stripped first.
    """
    stripped = (path or "").rstrip("/\\")
    return _SLUG_RE.sub("-", stripped)


def slug_matches(slug: str, project_path: str) -> bool:
    """True if `slug` is the project's slug or a worktree/subpath under it."""
    base = path_to_slug(project_path)
    if not base:
        return False
    return slug == base or slug.startswith(base + "-")


def transcripts_for(claude_home: Path, project_path: str) -> list[Path]:
    """Return every .jsonl transcript whose slug matches `project_path`."""
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return []
    out: list[Path] = []
    for sub in projects_dir.iterdir():
        if not sub.is_dir():
            continue
        if not slug_matches(sub.name, project_path):
            continue
        out.extend(sorted(sub.glob("*.jsonl")))
    return out


def _is_imported(conn: sqlite3.Connection, session_id: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM session_outcomes WHERE session_id = ? LIMIT 1",
        (session_id,),
    )
    return cur.fetchone() is not None


def parse_session_metrics(path: Path) -> dict | None:
    """Walk a transcript JSONL once and return aggregated metrics +
    per-skill invocation records.

    Returns None if the file is empty / unparseable / has no sessionId.
    Returned dict carries session-level totals (matching
    `Brain.record_session_outcome` shape) plus a `skill_invocations`
    list of (skill_name, ts_iso) pairs for skill_usage rows.
    """
    session_id = ""
    tokens_out = 0
    files_read: set[str] = set()
    files_modified: set[str] = set()
    skill_invocations: list[tuple[str, str]] = []  # (skill_name, timestamp_iso)
    first_ts: float | None = None
    last_ts: float | None = None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not session_id:
            session_id = evt.get("sessionId") or evt.get("session_id") or ""
        ts_str = evt.get("timestamp") or ""
        if ts_str:
            ts = _parse_ts(ts_str)
            if ts is not None:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts
        msg = evt.get("message") or {}
        usage = msg.get("usage") or {}
        if usage:
            tokens_out += int(usage.get("output_tokens") or 0)
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name") or ""
            inp = block.get("input") or {}
            if name == "Read":
                fp = inp.get("file_path") or inp.get("path") or ""
                if fp:
                    files_read.add(fp)
            elif name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                fp = inp.get("file_path") or inp.get("path") or ""
                if fp:
                    files_modified.add(fp)
            elif name == "Skill":
                skill_name = (inp.get("skill") or inp.get("name") or "").strip()
                if skill_name:
                    skill_invocations.append((skill_name, ts_str or ""))
    if not session_id:
        return None
    duration = 0
    if first_ts is not None and last_ts is not None:
        duration = max(0, int(last_ts - first_ts))
    return {
        "session_id": session_id,
        "duration_s": duration,
        "tokens_used": tokens_out,
        "files_read": len(files_read),
        "files_modified": len(files_modified),
        "skills_invoked": len(skill_invocations),
        "skill_invocations": skill_invocations,
    }


def _parse_ts(s: str) -> float | None:
    """Parse ISO-8601 timestamp to unix seconds. Tolerates trailing 'Z'."""
    try:
        from datetime import datetime
        t = s.rstrip("Z") + "+00:00" if s.endswith("Z") else s
        return datetime.fromisoformat(t).timestamp()
    except Exception:
        return None


def import_unseen(
    scores_db: str,
    project_source_path: str,
    claude_home: Path | None = None,
) -> int:
    """Parse every transcript matching the project, insert any session
    whose id isn't already in `session_outcomes`. Returns the import count."""
    claude_home = claude_home or resolve_claude_home()
    paths = transcripts_for(claude_home, project_source_path)
    if not paths:
        return 0
    n = 0
    try:
        conn = sqlite3.connect(scores_db)
    except sqlite3.Error:
        return 0
    try:
        for path in paths:
            metrics = parse_session_metrics(path)
            if not metrics:
                continue
            if _is_imported(conn, metrics["session_id"]):
                continue
            # Use the file's mtime as recorded_at if possible — better
            # than the import moment for chronological ordering.
            try:
                ts_iso = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.gmtime(path.stat().st_mtime),
                )
            except OSError:
                ts_iso = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
            try:
                before = conn.total_changes
                conn.execute(
                    "INSERT OR IGNORE INTO session_outcomes "
                    "(session_id, duration_s, tokens_used, files_read, "
                    "files_modified, skills_invoked, timestamp) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        metrics["session_id"],
                        metrics["duration_s"],
                        metrics["tokens_used"],
                        metrics["files_read"],
                        metrics["files_modified"],
                        metrics["skills_invoked"],
                        ts_iso,
                    ),
                )
                if conn.total_changes > before:
                    n += 1
                # v5.3.16 — also populate skill_usage from the same
                # walk. Each Skill tool_use in the JSONL = one row.
                # The table has no unique constraint on (session_id,
                # skill_name) so we only insert when the session row
                # itself was new (idempotency by proxy).
                if conn.total_changes > before:
                    for skill_name, ts_evt in metrics.get("skill_invocations", []):
                        skill_ts = ts_evt[:19].replace("T", " ") if ts_evt else ts_iso
                        try:
                            conn.execute(
                                "INSERT INTO skill_usage "
                                "(session_id, skill_name, timestamp) "
                                "VALUES (?, ?, ?)",
                                (metrics["session_id"], skill_name, skill_ts),
                            )
                        except sqlite3.Error:
                            continue
            except sqlite3.Error:
                continue
        conn.commit()
    finally:
        conn.close()
    return n


def start_transcript_importer() -> None:
    """Daemon-loop driver — runs forever, polls every interval seconds.

    For each PRISM project, resolves its source_path, derives the slug,
    enumerates matching ~/.claude/projects/<slug>/*.jsonl transcripts,
    and imports any session not yet in `session_outcomes`. Idempotent —
    re-imports the same files on every pass but only writes new rows.
    """
    interval = int(os.environ.get("PRISM_TRANSCRIPT_POLL_S", "60"))
    if interval <= 0:
        print("[transcripts] importer disabled (PRISM_TRANSCRIPT_POLL_S=0)",
              file=sys.stderr, flush=True)
        return
    print(f"[transcripts] importer running every {interval}s",
          file=sys.stderr, flush=True)
    while True:
        try:
            claude_home = resolve_claude_home()
            for pid in get_all_projects():
                try:
                    ctx = get_project(pid)
                    scores_db = str(ctx._data_dir / "scores.db")
                    sp = _project_source_path(pid)
                    if not sp:
                        continue
                    n = import_unseen(scores_db, sp, claude_home)
                    if n:
                        print(
                            f"[transcripts] {pid}: imported {n} session(s)",
                            file=sys.stderr, flush=True,
                        )
                except Exception as e:
                    print(
                        f"[transcripts] {pid}: error {type(e).__name__}: {e}",
                        file=sys.stderr, flush=True,
                    )
        except Exception as e:
            print(f"[transcripts] loop error: {e}", file=sys.stderr, flush=True)
        time.sleep(interval)


def _project_source_path(project_id: str) -> str:
    """Return the project's configured source_path, or '' if folder mode
    isn't set up. Reads understand_state.json directly to avoid pulling
    extra dependencies."""
    from prism_service.engines import understand_engine as ue
    try:
        state = ue._read_state(project_id)
        return (state.get("source_path") or "").strip()
    except Exception:
        return ""
