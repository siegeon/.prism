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

# v6.0.5 — signal extraction for the consolidation pipeline. Inspired by
# Redis Iris (2026-05-18): the transcripts already on disk carry rich
# session content (pushback, errors, decisions, memory-store call sites)
# that prior versions silently discarded during the metrics walk. The
# reflection sub-agent (JanitorService.check brief) can't reason about
# what happened in a session if it only sees aggregate counts.
_PUSHBACK_RE = re.compile(
    r"\b(no|stop|don['’]?t|wait|actually|wrong|nope)\b",
    re.IGNORECASE,
)
_BG_PROTOCOL_RE = re.compile(
    r"^(result|needs input|failed):\s+(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
_MEMORY_STORE_TOOL_NAMES: tuple[str, ...] = (
    "mcp__prism__memory_store",
    "memory_store",
)
_MAX_SIGNAL_ITEMS = 12        # cap per-bucket items to bound brief size
_EXCERPT_MAX_CHARS = 3500     # cap total transcript_excerpt length
_RECENT_MSG_WINDOW = 8        # messages kept for memory_store context


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
    per-skill invocation records + signals for the reflection pipeline.

    Returns None if the file is empty / unparseable / has no sessionId.
    The dict carries session-level totals (matching
    `Brain.record_session_outcome` shape), a `skill_invocations` list of
    (skill_name, ts_iso) pairs for skill_usage rows, and a `signals`
    bucket — pushbacks, background-protocol lines, tool failures, and
    memory-store call sites — that feeds the consolidation pipeline.
    """
    session_id = ""
    tokens_out = 0
    files_read: set[str] = set()
    files_modified: set[str] = set()
    skill_invocations: list[tuple[str, str]] = []
    pushbacks: list[dict] = []
    bg_signals: list[dict] = []
    tool_failures: list[dict] = []
    memory_writes: list[dict] = []
    recent_msgs: list[tuple[str, str]] = []   # rolling (role, text) window
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
        role = (msg.get("role") or evt.get("type") or "").lower()
        usage = msg.get("usage") or {}
        if usage:
            tokens_out += int(usage.get("output_tokens") or 0)

        # Collect plain-text spans for the role-level signals.
        raw_content = msg.get("content")
        if isinstance(raw_content, str):
            text_chunks = [raw_content]
            blocks: list = []
        elif isinstance(raw_content, list):
            blocks = raw_content
            text_chunks = [
                b.get("text", "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
        else:
            blocks = []
            text_chunks = []
        joined_text = "\n".join(t for t in text_chunks if t)

        if role == "user" and joined_text:
            if (_PUSHBACK_RE.search(joined_text)
                    and len(pushbacks) < _MAX_SIGNAL_ITEMS):
                pushbacks.append({"ts": ts_str, "text": joined_text[:400]})
            recent_msgs.append(("user", joined_text[:500]))
        elif role == "assistant" and joined_text:
            for m in _BG_PROTOCOL_RE.finditer(joined_text):
                if len(bg_signals) >= _MAX_SIGNAL_ITEMS:
                    break
                bg_signals.append({
                    "ts": ts_str,
                    "kind": m.group(1).lower().replace(" ", "_"),
                    "text": m.group(2).strip()[:400],
                })
            recent_msgs.append(("assistant", joined_text[:500]))
        if len(recent_msgs) > _RECENT_MSG_WINDOW:
            recent_msgs = recent_msgs[-_RECENT_MSG_WINDOW:]

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
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
                elif name in _MEMORY_STORE_TOOL_NAMES:
                    if len(memory_writes) < _MAX_SIGNAL_ITEMS:
                        memory_writes.append({
                            "ts": ts_str,
                            "name": inp.get("name") or inp.get("title") or "",
                            "domain": inp.get("domain") or "",
                            "context": [
                                {"role": r, "text": t}
                                for r, t in recent_msgs[-4:]
                            ],
                        })
            elif btype == "tool_result":
                if block.get("is_error") and len(tool_failures) < _MAX_SIGNAL_ITEMS:
                    raw = block.get("content") or ""
                    if isinstance(raw, list):
                        raw = " ".join(
                            x.get("text", "") for x in raw
                            if isinstance(x, dict) and x.get("text")
                        )
                    tool_failures.append({
                        "ts": ts_str,
                        "tool_use_id": block.get("tool_use_id", ""),
                        "error": str(raw)[:400],
                    })
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
        "signals": {
            "pushbacks": pushbacks,
            "bg_signals": bg_signals,
            "tool_failures": tool_failures,
            "memory_writes": memory_writes,
        },
    }


def format_transcript_excerpt(signals: dict) -> str:
    """Render a signals bucket into a compact text excerpt for the
    reflection sub-agent. JanitorService.check wraps it in
    ``<untrusted>...</untrusted>`` before showing it to the LLM, so we
    only need a human-scannable plain-text rendering here.

    Capped at ``_EXCERPT_MAX_CHARS`` so the brief stays bounded even on
    sessions with many signals; trailing buckets are truncated rather
    than spreading the budget thinly.
    """
    parts: list[str] = []
    skills = signals.get("skill_invocations") or []
    if skills:
        # tuples (name, ts) from parse_session_metrics; sort by ts asc.
        ordered = sorted(skills, key=lambda x: (x[1] if len(x) > 1 else ""))
        parts.append(f"Skills invoked ({len(ordered)}):")
        for entry in ordered:
            name = entry[0] if entry else ""
            ts = entry[1] if len(entry) > 1 else ""
            parts.append(f"- [{ts}] {name}")
        parts.append("")
    pushbacks = signals.get("pushbacks") or []
    if pushbacks:
        parts.append(f"Pushbacks ({len(pushbacks)}):")
        for p in pushbacks:
            # Real path passes dicts {ts,text}; tolerate bare strings too
            # so a malformed/legacy signal can't crash the enqueue path.
            if isinstance(p, dict):
                parts.append(f"- [{p.get('ts','')}] user: {p.get('text','')}")
            else:
                parts.append(f"- user: {p}")
    bg = signals.get("bg_signals") or []
    if bg:
        parts.append(f"\nBackground protocol ({len(bg)}):")
        for s in bg:
            parts.append(f"- {s.get('kind','')}: {s.get('text','')}")
    failures = signals.get("tool_failures") or []
    if failures:
        parts.append(f"\nTool failures ({len(failures)}):")
        for f in failures:
            parts.append(f"- {f.get('error','')[:200]}")
    writes = signals.get("memory_writes") or []
    if writes:
        parts.append(f"\nMemory-store call sites ({len(writes)}):")
        for w in writes:
            parts.append(
                f"- {w.get('domain','')}/{w.get('name','')} at {w.get('ts','')}"
            )
            for c in w.get("context") or []:
                parts.append(f"    {c.get('role','')}: {c.get('text','')[:160]}")
    out = "\n".join(parts)
    if len(out) > _EXCERPT_MAX_CHARS:
        out = out[:_EXCERPT_MAX_CHARS] + "\n... (truncated)"
    return out


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
    override_dir: str | None = None,
) -> int:
    """Parse every transcript matching the project, insert any session
    whose id isn't already in `session_outcomes`. Returns the import count.

    v6.2.16 — when `override_dir` is set (the per-project claude_project_dir
    config), read transcripts directly from `<override_dir>/*.jsonl` instead
    of slug-scanning ~/.claude/projects; used when auto-discovery misses."""
    claude_home = claude_home or resolve_claude_home()
    if override_dir and override_dir.strip():
        d = Path(override_dir.strip())
        paths = sorted(d.glob("*.jsonl")) if d.is_dir() else []
    else:
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
                # v6.0.5 — bridge to /consolidation. The MCP-call path
                # (Brain.record_session_outcome) already enqueues, but
                # the disk-reader path was inserting via raw SQL and
                # bypassing the bridge entirely — so transcripts pulled
                # off disk never produced a candidate. Pass the
                # transcript_excerpt so the reflection sub-agent sees
                # real session content, not just aggregate counts.
                #
                # Commit the outer conn first so the bridge's own
                # connection isn't blocked by our pending write lock.
                if conn.total_changes > before:
                    conn.commit()
                    _enqueue_with_signals(
                        scores_db, metrics["session_id"],
                        metrics, ts_iso,
                    )
            except sqlite3.Error:
                continue
        conn.commit()
    finally:
        conn.close()
    return n


def _enqueue_with_signals(
    scores_db: str,
    session_id: str,
    metrics: dict,
    ts_iso: str,
) -> None:
    """Wrap enqueue_for_session with a rich scope built from signals."""
    try:
        from prism_service.services.consolidation_data import (
            enqueue_for_session, resolve_task_id_for_session,
        )
        signals = metrics.get("signals") or {}
        # FIX 2a — the disk path has no task_id of its own, but a session
        # that was task-linked (task_sessions) must stamp that task_id onto
        # the candidate so /learning's Layer-B rollup fills. Resolve it here
        # so the noise filter below also sees the link.
        task_id = metrics.get("task_id") or resolve_task_id_for_session(
            scores_db, session_id,
        )
        skill_invocations = metrics.get("skill_invocations") or []
        scope: dict = {
            "files_read": metrics.get("files_read", 0),
            "files_modified": metrics.get("files_modified", 0),
            "skills_invoked": metrics.get("skills_invoked", 0),
            "duration_s": metrics.get("duration_s", 0),
            "tokens_used": metrics.get("tokens_used", 0),
            "signal_counts": {
                k: len(signals.get(k) or [])
                for k in ("pushbacks", "bg_signals", "tool_failures", "memory_writes")
            },
            "skill_invocations": skill_invocations,
        }
        # Inject named skill list into signals so format_transcript_excerpt
        # renders the Skills-invoked section alongside the other buckets.
        signals_for_excerpt = {**signals, "skill_invocations": skill_invocations}
        excerpt = format_transcript_excerpt(signals_for_excerpt)
        if excerpt:
            scope["transcript_excerpt"] = excerpt
        # v6.2.18 — noise filter. A session with NO usable signal would
        # produce an empty consolidation_candidate the reflection loop can
        # learn nothing from — these piled up (61 deep) and drained tokens.
        # Skip enqueue when there's no task link AND every signal bucket is
        # zero AND nothing was modified. The disk path never sets task_id,
        # so we read metrics.get("task_id") (None here) but still guard on
        # it so a future task-linked path is never filtered.
        sc = scope["signal_counts"]
        if (
            task_id is None
            and all(int(sc.get(k) or 0) == 0 for k in sc)
            and int(scope.get("files_modified", 0) or 0) == 0
        ):
            print(
                f"[transcript_importer] skip noise candidate "
                f"session={session_id}: no task, zero signals, "
                f"no files modified",
                flush=True,
            )
            return
        enqueue_for_session(
            scores_db, session_id, scope=scope, trigger="transcript_imported",
            task_id=task_id,
        )
    except Exception:
        pass  # never break the metrics insert path


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
                    from prism_service.services import claude_memory as cm
                    sp = _project_source_path(pid)
                    cpd = cm.configured_project_dir(pid)
                    if not sp and not cpd:
                        continue
                    n = import_unseen(
                        scores_db, sp or "", claude_home,
                        override_dir=cpd or None,
                    )
                    if n:
                        print(
                            f"[transcripts] {pid}: imported {n} session(s)",
                            file=sys.stderr, flush=True,
                        )
                    # v6.2.16 — same cadence, bridge Claude Code auto-memory
                    # (~/.claude/projects/<slug>/memory/*.md or a per-project
                    # configured override) into PRISM Memory. Best-effort:
                    # a memory-import failure must not stall transcript import.
                    try:
                        from prism_service.services import claude_memory as cm
                        mres = cm.import_project_memories(
                            pid, ctx.memory_svc, claude_home=claude_home,
                        )
                        if mres.get("imported"):
                            print(
                                f"[transcripts] {pid}: imported "
                                f"{mres['imported']} memory entr(ies)",
                                file=sys.stderr, flush=True,
                            )
                    except Exception as me:
                        print(
                            f"[transcripts] {pid}: memory import error "
                            f"{type(me).__name__}: {me}",
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
