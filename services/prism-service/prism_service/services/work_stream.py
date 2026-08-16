"""tokens.turn ticker -- the third bus publisher of the gamify walking
skeleton ("PRISM shows its work").

drive.heartbeat and agent.run are event-shaped already (something POSTs a
row, we publish it). Live token burn is NOT event-shaped -- it lives in
Claude transcript JSONL files on disk that nothing pushes -- so this module
is a small poller: every ``interval_s`` it walks every ACTIVE managed task
in every project, rereads each linked session's transcript via the existing
incremental cache (claude_transcripts._TOKEN_CACHE, sub-second warm), and
publishes one ``tokens.turn`` event per (task, session) pair that produced
NEW tokens since the last tick. A pair with no new tokens publishes nothing
-- the /live graph's packet dots are driven by real motion, never a clock.

Started once at app boot (see main.py, alongside start_transcript_importer)
on its own daemon thread. Every per-tick exception is caught and logged so
one bad project/task/session can never wedge the ticker for everyone else.
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

# How often the ticker walks every active task. Cheap: the transcript read
# is the incremental tail-cache (claude_transcripts._TOKEN_CACHE), not a
# fresh full parse each time.
_DEFAULT_INTERVAL_S = 1.5

# A session earns a graph node only while its last token event is within
# this many seconds -- kept as a private mirror of api/work.py's constant
# so this module has no import-time dependency on the API layer.
_SESSION_RECENCY_S = 30 * 60

# key = f"{project}\x00{task_id}\x00{session_id}" -> last epoch_s observed
# and the wall-clock time of the last PUBLISH for that key (used as dt_s).
_TICK_STATE: dict[str, dict[str, float]] = {}
_STATE_LOCK = threading.Lock()


def _active_task_ids(conductor) -> list[str]:
    """Root + subtask ids the conductor considers currently managed and
    in motion -- status in_progress, or a live workflow_step. Mirrors the
    same fields managed_tasks() already computes, so this reuses its walk
    rather than re-deriving activity."""
    out: list[str] = []
    try:
        roots = conductor.managed_tasks()
    except Exception:
        logger.exception("work ticker: managed_tasks() failed")
        return out
    for r in roots:
        if r.get("status") == "in_progress" or r.get("workflow_step"):
            out.append(r["id"])
        for c in r.get("subtasks") or []:
            if (c.get("status") or "") == "in_progress":
                out.append(c["id"])
    return out


def _tick_project(project: str) -> None:
    from prism_service.project_context import get_project
    from prism_service.services.claude_transcripts import (
        live_token_events_for_session,
    )

    ctx = get_project(project)
    conductor = ctx.conductor_svc
    task_svc = ctx.task_svc

    task_ids = _active_task_ids(conductor)
    if not task_ids:
        return

    try:
        source_path = conductor._project_source_path()
        override_dir = conductor._project_override_dir()
    except Exception:
        logger.exception("work ticker: failed to resolve %s transcript source", project)
        return

    now = time.time()
    from prism_service.events import bus

    for task_id in task_ids:
        # gamify round6 item 2 (atomic card+wire): resolved ONCE per task
        # per tick (never per session) -- see task_service.py's
        # task.changed publish for the full rationale.
        parent_id = ""
        try:
            obj = task_svc.get(task_id)
            parent_id = (getattr(obj, "parent_id", "") or "") if obj else ""
        except Exception:
            pass
        try:
            sessions = task_svc.sessions_for_task(task_id)
        except Exception:
            logger.exception("work ticker: sessions_for_task failed for %s/%s", project, task_id)
            continue
        for sess in sessions:
            sid = sess.get("session_id")
            if not sid:
                continue
            key = f"{project}\x00{task_id}\x00{sid}"
            try:
                events = live_token_events_for_session(
                    sid, source_path, override_dir=override_dir or None)
            except Exception:
                logger.exception(
                    "work ticker: live_token_events_for_session failed for %s/%s/%s",
                    project, task_id, sid)
                continue
            if not events:
                continue
            last_epoch = events[-1][0]
            if now - last_epoch > _SESSION_RECENCY_S:
                continue
            with _STATE_LOCK:
                state = _TICK_STATE.get(key)
                if state is None:
                    # First sight of this pair: establish the baseline
                    # without publishing, so a session that's been running
                    # for hours doesn't dump its whole history as one
                    # instantaneous "burst" the moment it enters scope.
                    _TICK_STATE[key] = {"last_epoch": last_epoch, "last_tick": now}
                    continue
                new_events = [e for e in events if e[0] > state["last_epoch"]]
                if not new_events:
                    continue
                out_tokens = sum(int(tok or 0) for _, tok in new_events)
                if out_tokens <= 0:
                    continue
                dt_s = max(0.1, now - state["last_tick"])
                state["last_epoch"] = last_epoch
                state["last_tick"] = now
            tok_s = round(out_tokens / dt_s, 1)
            tokens_total = sum(int(tok or 0) for _, tok in events)
            # gamify data-enrichment slice item 4: usd_total was considered
            # here (running spend for the session, priced via
            # claude_transcripts.live_spend_for_session / usage_components).
            # Deliberately SKIPPED: this tick's `events` come from
            # live_token_events_for_session, which reads the transcript's
            # new tail bytes through its OWN incremental cache
            # (_TOKEN_EVENTS_CACHE, keyed on path+offset). Honest per-field
            # USD needs the full usage dict + per-turn model
            # (claude_transcripts._spend_for_path / _SPEND_CACHE), which is
            # a SEPARATE incremental reader keyed on the same path -- so
            # calling it here would open/seek/read the SAME new lines a
            # second time, every tick, for every active session. That is
            # exactly the "double transcript reads" case this task's
            # instructions call out to skip. api/work.py's spend_usd (task/
            # subtask nodes, cached ~5s) is the honest live-spend surface;
            # a session-level usd_total on this per-tick event would need a
            # THIRD cache that shares _SPEND_CACHE's offsets rather than
            # re-deriving them, which is a real refactor, not a cheap add.
            try:
                bus.publish({
                    "project": project,
                    "type": "tokens.turn",
                    "task_id": task_id,
                    "parent_id": parent_id,
                    "session_id": sid,
                    "out_tokens": out_tokens,
                    "dt_s": round(dt_s, 2),
                    "tok_s": tok_s,
                    "tokens_total": tokens_total,
                    "ts": now,
                })
            except Exception:
                logger.exception("work ticker: publish failed for %s/%s/%s", project, task_id, sid)


def _tick_once() -> None:
    from prism_service.project_context import get_all_projects

    try:
        projects = get_all_projects()
    except Exception:
        logger.exception("work ticker: get_all_projects() failed")
        return
    for project in projects:
        try:
            _tick_project(project)
        except Exception:
            logger.exception("work ticker: tick failed for project %s", project)


def start_work_ticker(interval_s: float = _DEFAULT_INTERVAL_S) -> None:
    """Blocking loop -- run this on its own daemon thread, same pattern as
    start_transcript_importer. Never raises: every tick and every per-tick
    exception is caught and logged so the ticker itself never dies."""
    while True:
        try:
            _tick_once()
        except Exception:
            logger.exception("work ticker: tick_once raised")
        time.sleep(interval_s)
