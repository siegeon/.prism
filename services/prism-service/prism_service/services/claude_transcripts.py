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
from prism_service.services import sqlite_db
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

# v6.3.41 (task c80fd9bb) — deterministic machinery allow/deny gate. Runs with
# ZERO inference BEFORE any LLM call so autonomous-loop machinery (Stop-hook
# re-invocations, "# Autonomous loop tick" ScheduleWakeup, scheduler
# heartbeats, identical retries) never registers as a pushback/correction.
# Reflexion (https://ar5iv.labs.arxiv.org/html/2303.11366) treats
# internally-simulated feedback as distinct from genuine external signal; this
# is the cheap pre-filter that enforces that distinction at ingest.
_MACHINERY_RE = re.compile(
    r"(autonomous loop tick"
    r"|schedulewakeup"
    r"|scheduler heartbeat"
    r"|stop[\s-]*hook"
    r"|work (our|the) github tasks"
    r"|continue the autonomous loop"
    r"|no new input)",
    re.IGNORECASE,
)


# v6.7.23 — the ONE token summer. Every surface used to sum
# usage.output_tokens ONLY, undercounting real spend ~187x (audit across 36
# transcripts: displayed 15.9M vs real 2.97B = output 15.9M + input 1.6M +
# cache_read 2.88B + cache_creation 77.2M). All four usage fields count.
# assets/stop_record_hook.py duplicates this field list (it cannot import
# the service package) — keep the two in sync.
USAGE_TOKEN_FIELDS: tuple[str, ...] = (
    "output_tokens",
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def usage_components(usage: dict | None) -> dict[str, int]:
    """Per-field token counts off one `usage` dict, all defaulting 0.

    `cache_creation_input_tokens` handles both shapes seen in real Claude
    Code JSONL: the flat field (authoritative when present) and the nested
    `cache_creation: {ephemeral_1h_input_tokens, ephemeral_5m_input_tokens}`
    breakdown (summed only as a fallback — real transcripts carry BOTH and
    the flat value equals the nested sum, so never double-count)."""
    u = usage if isinstance(usage, dict) else {}
    out = {f: int(u.get(f) or 0) for f in USAGE_TOKEN_FIELDS}
    # Nested-shape fallback for cache_creation only: when the flat field is
    # absent entirely, sum the nested breakdown (never both — real JSONL
    # carries both shapes with equal values).
    if u.get("cache_creation_input_tokens") is None:
        nested = u.get("cache_creation")
        if isinstance(nested, dict):
            out["cache_creation_input_tokens"] = int(sum(
                int(v or 0) for v in nested.values()
                if isinstance(v, (int, float))
            ))
    return out


def sum_usage(usage: dict | None) -> int:
    """Total tokens for one `usage` dict: output + input + cache_read +
    cache_creation. The single source of truth for token attribution —
    every summation path (session_outcomes import, live conductor read,
    per-turn timeline) routes through here."""
    return sum(usage_components(usage).values())


def is_machinery_turn(text: str) -> bool:
    """True when a turn is autonomous-loop MACHINERY (Stop-hook directive,
    loop-tick/ScheduleWakeup re-invocation, scheduler heartbeat), not genuine
    user signal. Deterministic, no LLM. A genuine user correction — even one
    containing pushback words like "no"/"stop" — must return False so it still
    reaches the typed extractors."""
    if not text or not text.strip():
        return True  # empty turn carries no signal
    return bool(_MACHINERY_RE.search(text))


def _actionable_tip(kind: str, text: str) -> str:
    """One-line actionable tip for a typed signal (Trajectory-Informed Memory
    Generation, 2026: strategy/recovery/optimization tips, concrete not vague)."""
    snippet = " ".join((text or "").split())[:160]
    if kind == "user_correction":
        return f"User corrected the approach: {snippet}"
    if kind == "failure_fix":
        return f"Recovered from a failure: {snippet}"
    if kind == "stuck":
        return f"Degenerate retry loop (no progress): {snippet}"
    return f"Novel success / optimization: {snippet}"


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
    typed_signals: list[dict] = []            # v6.3.41 — typed extractors
    saw_assistant_action = False              # actor-tag: was there a prior asst turn
    _tool_sig_counts: dict[str, int] = {}     # tool_use signature -> repeat count
    _stuck_flagged = False                    # degenerate-loop collapse (one 'stuck')
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
            tokens_out += sum_usage(usage)

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
            # v6.3.41 — actor-tag + machinery gate. A turn that is autonomous-
            # loop machinery (Stop-hook directive, loop tick, heartbeat) is
            # internally-simulated feedback, NOT a genuine user correction: it
            # never counts as a pushback nor a typed user_correction. A real
            # pushback must (a) survive the machinery gate, (b) contain a
            # correction marker, and (c) actually correct a PRIOR assistant
            # action (Reflexion: external feedback against a trajectory).
            if (not is_machinery_turn(joined_text)
                    and _PUSHBACK_RE.search(joined_text)):
                if len(pushbacks) < _MAX_SIGNAL_ITEMS:
                    pushbacks.append({"ts": ts_str, "text": joined_text[:400]})
                if saw_assistant_action and len(typed_signals) < _MAX_SIGNAL_ITEMS:
                    typed_signals.append({
                        "kind": "user_correction",
                        "ts": ts_str,
                        "text": joined_text[:400],
                        "tip": _actionable_tip("user_correction", joined_text),
                    })
            recent_msgs.append(("user", joined_text[:500]))
        elif role == "assistant" and joined_text:
            saw_assistant_action = True
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
                saw_assistant_action = True
                # v6.3.41 — Reflexion degenerate-loop collapse. N IDENTICAL
                # tool invocations (same name+input, no progress) are a single
                # 'stuck' signal, not N pushbacks/failures. Collapse to ONE.
                try:
                    sig = name + "|" + json.dumps(inp, sort_keys=True, default=str)
                except (TypeError, ValueError):
                    sig = name
                rep = _tool_sig_counts.get(sig, 0) + 1
                _tool_sig_counts[sig] = rep
                if rep >= 3 and not _stuck_flagged:
                    _stuck_flagged = True
                    typed_signals.append({
                        "kind": "stuck",
                        "ts": ts_str,
                        "text": sig[:400],
                        "tip": _actionable_tip("stuck", name),
                    })
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
    # v6.3.41 — contrastive failure->fix recovery (ExpeL) + novel-success.
    # A failure that was NOT a degenerate loop AND was followed by real
    # progress (files modified / memory written) is a recovery worth a tip.
    # Unresolved failures and no-op retries are dropped (no progress => no tip).
    made_progress = bool(files_modified) or bool(memory_writes)
    if (tool_failures and not _stuck_flagged and made_progress
            and len(typed_signals) < _MAX_SIGNAL_ITEMS):
        typed_signals.append({
            "kind": "failure_fix",
            "ts": tool_failures[-1].get("ts", ""),
            "text": tool_failures[0].get("error", "")[:400],
            "tip": _actionable_tip("failure_fix", tool_failures[0].get("error", "")),
        })
    # Novel success / optimization: a clean run that wrote durable memory
    # without corrections or failures is a strategy worth keeping.
    if (memory_writes and not pushbacks and not tool_failures
            and len(typed_signals) < _MAX_SIGNAL_ITEMS):
        nm = memory_writes[0]
        label = f"{nm.get('domain','')}/{nm.get('name','')}"
        typed_signals.append({
            "kind": "novel_success",
            "ts": nm.get("ts", ""),
            "text": label[:400],
            "tip": _actionable_tip("novel_success", label),
        })
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
            "typed_signals": typed_signals,
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
        conn = sqlite_db.connect(scores_db, timeout=5.0)
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
                    # Phase 2 (epic 4fd1e6b4): announce the imported session
                    # on the learning bus AFTER conn.commit, ALONGSIDE (not
                    # instead of) _enqueue_with_signals (dual-run). The event
                    # names the import FACT, so it emits unconditionally even
                    # when the v6.2.18 noise filter skips the enqueue.
                    # Best-effort — a bus failure must never break the import.
                    try:
                        from prism_service.services import event_pool as _ep
                        _ep.get_bus().emit(_ep.Event(
                            type=_ep.SESSION_IMPORTED,
                            payload={"session_id": metrics["session_id"]},
                        ))
                    except Exception:
                        pass  # best-effort — never break the import path
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
                for k in ("pushbacks", "bg_signals", "tool_failures",
                          "memory_writes", "typed_signals")
            },
            "skill_invocations": skill_invocations,
        }
        # Inject named skill list into signals so format_transcript_excerpt
        # renders the Skills-invoked section alongside the other buckets.
        signals_for_excerpt = {**signals, "skill_invocations": skill_invocations}
        excerpt = format_transcript_excerpt(signals_for_excerpt)
        if excerpt:
            scope["transcript_excerpt"] = excerpt
        # v6.3.41 — cheap per-candidate token estimate (~4 chars/token) so the
        # deterministic select_cycle_candidates() gate can enforce a
        # tokens/cycle ceiling BEFORE any LLM call. Bounds reflection cost.
        scope["est_tokens"] = (len(excerpt) // 4) + 256 if excerpt else 256
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


# v6.7.23 — one-time token-total backfill. sum_usage widened the count from
# output-only to all four usage fields, so HISTORICAL session_outcomes rows
# (written before the fix) sit ~187x below rows imported after it — every
# blended surface (SessionsPage median/p95, dashboard totals, the conductor's
# max(outcome, live) read) would mix the two scales. Recompute tokens_used
# off the transcripts still on disk; rows whose transcript is gone cannot be
# recomputed and are left as-is (logged as skipped).
# scores_db paths already backfilled this process (idempotent per boot; a
# re-run is cheap anyway — recomputed==0 once totals match).
_BACKFILLED_DBS: set[str] = set()


def backfill_token_totals(
    scores_db: str,
    project_source_path: str,
    claude_home: Path | None = None,
    override_dir: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Recompute session_outcomes.tokens_used via sum_usage for every row
    whose transcript JSONL still exists on disk; UPDATE in place.

    Idempotent + cheap: only rows where the stored value differs are
    written, and per-file sums reuse the (mtime,size) _TOKEN_CACHE. With
    `dry_run` nothing is written (counts only). Returns
    {"recomputed", "skipped_no_transcript", "unchanged"}."""
    stats = {"recomputed": 0, "skipped_no_transcript": 0, "unchanged": 0}
    try:
        conn = sqlite_db.connect(scores_db, timeout=5.0)
    except sqlite3.Error:
        return stats
    try:
        try:
            rows = conn.execute(
                "SELECT session_id, tokens_used FROM session_outcomes"
            ).fetchall()
        except sqlite3.Error:
            return stats
        for sid, stored in rows:
            if not sid:
                continue
            total = live_tokens_for_session(
                str(sid), project_source_path, claude_home=claude_home,
                override_dir=override_dir,
            )
            if total <= 0:
                # Transcript aged off disk (or never matched) — can't
                # recompute; leave the stored value alone.
                stats["skipped_no_transcript"] += 1
                continue
            if int(stored or 0) == total:
                stats["unchanged"] += 1
                continue
            if not dry_run:
                try:
                    conn.execute(
                        "UPDATE session_outcomes SET tokens_used = ? "
                        "WHERE session_id = ?",
                        (total, str(sid)),
                    )
                except sqlite3.Error:
                    continue
            stats["recomputed"] += 1
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return stats


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
                    # claude_memory is an OPTIONAL add-on (the auto-memory
                    # bridge). It must never gate session/token import — if it's
                    # unavailable, guard the import so import_unseen still runs.
                    # (Bug: an unguarded import here zeroed ALL token import.)
                    try:
                        from prism_service.services import claude_memory as cm
                    except Exception:
                        cm = None
                    sp = _project_source_path(pid)
                    cpd = cm.configured_project_dir(pid) if cm else None
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
                    # v6.7.23 — one-time (per process) token-total backfill:
                    # heal pre-fix output-only rows so no surface blends the
                    # two scales. Env-gated; idempotent; skips rows whose
                    # transcript is gone.
                    if (scores_db not in _BACKFILLED_DBS
                            and os.environ.get(
                                "PRISM_TOKEN_BACKFILL", "1",
                            ).strip().lower() not in ("0", "off", "false")):
                        _BACKFILLED_DBS.add(scores_db)
                        try:
                            b = backfill_token_totals(
                                scores_db, sp or "", claude_home,
                                override_dir=cpd or None,
                            )
                            if b["recomputed"] or b["skipped_no_transcript"]:
                                print(
                                    f"[transcripts] {pid}: token backfill "
                                    f"recomputed={b['recomputed']} "
                                    f"skipped_no_transcript="
                                    f"{b['skipped_no_transcript']} "
                                    f"unchanged={b['unchanged']}",
                                    file=sys.stderr, flush=True,
                                )
                        except Exception as be:
                            print(
                                f"[transcripts] {pid}: token backfill error "
                                f"{type(be).__name__}: {be}",
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


# ---------------------------------------------------------------------------
# LIVE token read (conductor SDLC bar). The post-hoc `session_outcomes` table
# is only written when the importer sees a session — so an IN-PROGRESS task
# always reads 0 tok there. These helpers sum ALL usage token fields (see
# sum_usage) straight off the
# transcript JSONL on disk, in real time, including the workflow-subagent
# transcripts filed under <slug>/<session-uuid>/ (they carry the parent
# sessionId, so their spend belongs to the same session). Keyed by (mtime,size)
# so the 5s conductor poll never re-reads an unchanged multi-MB transcript.
# ---------------------------------------------------------------------------

# path -> (mtime, size, offset, total_tokens). `offset` is the byte position
# up to the last COMPLETE line already folded into `total`; a growing file is
# read only from `offset` to EOF each poll (see _read_new_lines).
_TOKEN_CACHE: dict[str, tuple[float, int, int, int]] = {}


def _read_new_lines(
    path: Path, st: os.stat_result,
    cached: tuple[float, int, int, object] | None,
) -> tuple[list[str], int, str]:
    """Return (complete_lines, new_offset, mode) for an append-mostly JSONL.

    mode is one of:
      'hit'   file unchanged since the cache — no read, [] lines.
      'grew'  file strictly larger and clock not rewound — read ONLY the
              bytes from cached offset to EOF (the appended tail).
      'reset' shrank / rewritten / no cache — read the whole file from 0.

    Only bytes up to the LAST newline are consumed; a trailing fragment
    (a partial last line the writer is still appending) is left unconsumed
    so it is re-read once completed. `new_offset` is the byte position after
    the last complete line. Raises OSError to the caller on read failure."""
    size = st.st_size
    if cached is not None:
        c_mtime, c_size, c_offset, _ = cached
        if c_mtime == st.st_mtime and c_size == size:
            return [], c_offset, "hit"
        if size > c_size and st.st_mtime >= c_mtime and c_offset <= size:
            start, mode = c_offset, "grew"
        else:
            start, mode = 0, "reset"
    else:
        start, mode = 0, "reset"
    with open(path, "rb") as fh:
        fh.seek(start)
        chunk = fh.read()
    nl = chunk.rfind(b"\n")
    if nl == -1:
        # No complete line in the new bytes — consume nothing this poll.
        return [], start, mode
    consumed = chunk[: nl + 1]
    text = consumed.decode("utf-8", errors="replace")
    return text.splitlines(), start + len(consumed), mode


def _sum_billable_tokens(path: Path) -> int:
    """Sum ALL usage token fields (sum_usage: output + input + cache_read +
    cache_creation) across one transcript JSONL. Incremental: a growing file
    is re-read only from the last consumed byte offset, so the ~5s conductor
    poll never re-parses the whole multi-MB transcript on every tick."""
    try:
        st = path.stat()
    except OSError:
        return 0
    key = str(path)
    cached = _TOKEN_CACHE.get(key)
    try:
        lines, new_offset, mode = _read_new_lines(path, st, cached)
    except OSError:
        return cached[3] if cached else 0
    if mode == "hit":
        return cached[3]  # type: ignore[index]
    total = cached[3] if (mode == "grew" and cached) else 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = (evt.get("message") or {}).get("usage") or {}
        if usage:
            total += sum_usage(usage)
    _TOKEN_CACHE[key] = (st.st_mtime, st.st_size, new_offset, total)
    return total


def _session_id_of(path: Path) -> str:
    """Read just the first `sessionId` from a transcript, '' if none."""
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            sid = evt.get("sessionId") or evt.get("session_id")
            if sid:
                return str(sid)
    except OSError:
        pass
    return ""


def _override_session_paths(override_dir: str, session_id: str) -> list[Path]:
    """Transcript paths for `session_id` under an explicit `override_dir` (the
    per-project claude_project_dir): the flat <dir>/<sid>.jsonl plus every
    nested workflow-subagent transcript under <dir>/<sid>/. Mirrors the
    slug-scan layout so the override path reads exactly what the live path does.
    When session_id is blank, returns every <dir>/*.jsonl (the window read)."""
    d = Path(override_dir.strip())
    if not d.is_dir():
        return []
    if not session_id:
        return sorted(d.glob("*.jsonl"))
    out: list[Path] = []
    seen: set[Path] = set()
    main = d / f"{session_id}.jsonl"
    if main.is_file():
        out.append(main)
        seen.add(main)
    sess_dir = d / session_id
    if sess_dir.is_dir():
        for f in sess_dir.rglob("*.jsonl"):
            if f not in seen:
                out.append(f)
                seen.add(f)
    return out


def live_tokens_for_session(
    session_id: str, project_path: str, claude_home: Path | None = None,
    override_dir: str | None = None,
) -> int:
    """Sum live `output_tokens` for `session_id` across its on-disk transcript
    AND any workflow-subagent transcripts nested under <slug>/<session_id>/.
    Returns 0 when nothing is found (caller falls back to session_outcomes).

    When `override_dir` is set (the per-project claude_project_dir), read
    <override_dir>/<sid>.jsonl + nested <override_dir>/<sid>/ directly instead
    of slug-scanning ~/.claude/projects (v6.3.21, mirrors import_unseen)."""
    if not session_id:
        return 0
    if override_dir and override_dir.strip():
        return sum(_sum_billable_tokens(p)
                   for p in _override_session_paths(override_dir, session_id))
    if not project_path:
        return 0
    claude_home = claude_home or resolve_claude_home()
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return 0
    total = 0
    seen: set[Path] = set()
    for sub in projects_dir.iterdir():
        if not sub.is_dir() or not slug_matches(sub.name, project_path):
            continue
        main = sub / f"{session_id}.jsonl"
        if main.is_file() and main not in seen:
            total += _sum_billable_tokens(main)
            seen.add(main)
        # Subagent transcripts (workflow agents) live under the session dir
        # and carry the parent sessionId — their spend is part of this session.
        sess_dir = sub / session_id
        if sess_dir.is_dir():
            for f in sess_dir.rglob("*.jsonl"):
                if f not in seen:
                    total += _sum_billable_tokens(f)
                    seen.add(f)
    return total


# path -> (mtime, size, offset, [(epoch_s, tokens), ...]) — tokens per
# sum_usage (all four usage fields). Same incremental discipline as
# _sum_billable_tokens (offset = last consumed complete-line byte), but a
# KEYED timeline of assistant turns so token spend can be attributed to a
# wall-clock window (e.g. the turn-to-turn interval on the task-detail
# timeline).
_TOKEN_EVENTS_CACHE: dict[str, tuple[float, int, int, list[tuple[float, int]]]] = {}


def _parse_iso_epoch(ts: str) -> float | None:
    """ISO-8601 (UTC 'Z' or numeric offset) -> POSIX seconds; None on garbage."""
    if not ts:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _token_events(path: Path) -> list[tuple[float, int]]:
    """Per-assistant-turn (timestamp_epoch, billable_tokens) for one
    transcript, sorted ascending. Incremental: a growing file folds only its
    appended turns into the cached timeline (same offset discipline as
    _sum_billable_tokens), so a steady file costs nothing on repeat polls."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = str(path)
    cached = _TOKEN_EVENTS_CACHE.get(key)
    try:
        lines, new_offset, mode = _read_new_lines(path, st, cached)
    except OSError:
        return cached[3] if cached else []
    if mode == "hit":
        return cached[3]  # type: ignore[index]
    out: list[tuple[float, int]] = list(cached[3]) if (mode == "grew" and cached) else []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        usage = (evt.get("message") or {}).get("usage") or {}
        tok = sum_usage(usage)
        if tok <= 0:
            continue
        ep = _parse_iso_epoch(evt.get("timestamp") or "")
        if ep is None:
            continue
        out.append((ep, tok))
    out.sort(key=lambda e: e[0])
    _TOKEN_EVENTS_CACHE[key] = (st.st_mtime, st.st_size, new_offset, out)
    return out


def live_token_events_for_session(
    session_id: str, project_path: str, claude_home: Path | None = None,
    override_dir: str | None = None,
) -> list[tuple[float, int]]:
    """Like live_tokens_for_session, but returns the per-turn
    (epoch_s, output_tokens) timeline across the parent transcript AND nested
    workflow-subagent transcripts, sorted ascending. Empty when none found.
    Lets a caller bucket spend into arbitrary wall-clock windows.

    `override_dir` (v6.3.21): when set, read <override_dir>/<sid>.jsonl +
    nested <override_dir>/<sid>/ directly instead of slug-scanning."""
    if not session_id:
        return []
    if override_dir and override_dir.strip():
        out: list[tuple[float, int]] = []
        for p in _override_session_paths(override_dir, session_id):
            out.extend(_token_events(p))
        out.sort(key=lambda e: e[0])
        return out
    if not project_path:
        return []
    claude_home = claude_home or resolve_claude_home()
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return []
    out: list[tuple[float, int]] = []
    seen: set[Path] = set()
    for sub in projects_dir.iterdir():
        if not sub.is_dir() or not slug_matches(sub.name, project_path):
            continue
        main = sub / f"{session_id}.jsonl"
        if main.is_file() and main not in seen:
            out.extend(_token_events(main))
            seen.add(main)
        sess_dir = sub / session_id
        if sess_dir.is_dir():
            for f in sess_dir.rglob("*.jsonl"):
                if f not in seen:
                    out.extend(_token_events(f))
                    seen.add(f)
    out.sort(key=lambda e: e[0])
    return out


def project_token_events_in_window(
    project_path: str, since: float, until: float,
    claude_home: Path | None = None,
    override_dir: str | None = None,
) -> list[tuple[float, int]]:
    """Per-turn (epoch_s, output_tokens) across ALL transcripts matching the
    project whose events fall in [since, until], sorted ascending.

    The task-timeline attribution fallback: a task that was never explicitly
    link_session'd still has the work that produced its turns on disk, so we
    bucket the project's transcript spend into the task's history window by
    wall-clock. Files are pruned by mtime (a transcript last written before
    `since` cannot hold in-window events) and per-file reads are cached on
    (mtime,size) via _token_events. Best-effort: when two tasks were worked in
    the SAME window their spend can't be told apart, so this only fills the gap
    when no authoritative linked-session events exist. [] when none match.

    `override_dir` (v6.3.21): when set, bucket every <override_dir>/**/*.jsonl
    transcript directly instead of slug-scanning ~/.claude/projects."""
    if until <= since:
        return []
    out: list[tuple[float, int]] = []

    def _bucket(f: Path) -> None:
        try:
            if f.stat().st_mtime < since:
                return
        except OSError:
            return
        out.extend((ep, tok) for ep, tok in _token_events(f) if since <= ep <= until)

    if override_dir and override_dir.strip():
        d = Path(override_dir.strip())
        if d.is_dir():
            for f in d.rglob("*.jsonl"):
                _bucket(f)
        out.sort(key=lambda e: e[0])
        return out
    if not project_path:
        return []
    claude_home = claude_home or resolve_claude_home()
    projects_dir = claude_home / "projects"
    if not projects_dir.is_dir():
        return []
    for sub in projects_dir.iterdir():
        if not sub.is_dir() or not slug_matches(sub.name, project_path):
            continue
        for f in sub.rglob("*.jsonl"):
            _bucket(f)
    out.sort(key=lambda e: e[0])
    return out


def token_turns_from_events(events: list[tuple[float, int]]) -> list[dict]:
    """Derive the per-turn burn-RATE series {out, dt_s, tok_s} from a sorted
    (epoch, output_tokens) timeline, applying the SHARED 1s/600s clamp. Single
    source of truth so the live path and the wall-clock fallback can't drift.

    Clamp rationale: transcript timestamps are message WRITE times, not
    generation durations, so a rapid tool-result -> assistant pair can land <1s
    apart and make out/dt read as a megatoken/s phantom (the "peak 1369.1k
    tok/s" spike that flatlines every other bar). Floor sub-second / zero /
    negative gaps at 1s, and cap idle gaps at the 600s ceiling."""
    if not events:
        return []
    out: list[dict] = []
    prev_ts = events[0][0]
    for ts, tok in events:
        dt = ts - prev_ts
        if dt < 1.0 or dt > 600:
            dt = 1.0
        prev_ts = ts
        out.append({"out": tok, "dt_s": round(dt, 2), "tok_s": round(tok / dt, 1)})
    return out


def live_token_turns_for_session(
    session_id: str, project_path: str, claude_home: Path | None = None,
    limit: int = 40,
) -> list[dict]:
    """Recent per-turn burn-RATE series for the conductor tile's live graph.
    Thin wrapper over live_token_events_for_session: takes the merged, sorted
    (epoch, output_tokens) timeline, keeps the last `limit` turns, and derives
    {out, dt_s, tok_s} per turn via token_turns_from_events. [] when none."""
    raw = live_token_events_for_session(session_id, project_path, claude_home)
    if not raw:
        return []
    return token_turns_from_events(raw[-limit:])


def current_session_id(
    project_path: str, claude_home: Path | None = None,
    override_dir: str | None = None,
) -> str:
    """Best-effort id of the project's ACTIVE Claude session — the sessionId
    of the most-recently-modified matching transcript. Used as the conductor
    link fallback so we stamp a REAL, resolvable session instead of the MCP
    request handle (which maps to no transcript and no token data).

    `override_dir` (mirrors the v6.3.21 readers): when set, resolve the active
    session from <override_dir>/*.jsonl directly (via _override_session_paths
    with a blank session_id) instead of slug-scanning ~/.claude/projects. This
    lets folder-mode / cwd-mismatched projects (project_path empty) still stamp
    a REAL session id off the explicit claude_project_dir (#134)."""
    if override_dir and override_dir.strip():
        paths = _override_session_paths(override_dir, "")
        if paths:
            newest = max(paths, key=lambda p: p.stat().st_mtime)
            return _session_id_of(newest) or newest.stem
    if not project_path:
        return ""
    claude_home = claude_home or resolve_claude_home()
    paths = transcripts_for(claude_home, project_path)
    if not paths:
        return ""
    newest = max(paths, key=lambda p: p.stat().st_mtime)
    return _session_id_of(newest) or newest.stem
