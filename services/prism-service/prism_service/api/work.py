"""Work graph -- the /live screen's boot snapshot + dev-only sim door
(gamify walking skeleton, "PRISM shows its work").

GET /graph assembles ONE snapshot of the live-work node graph: every
conductor-managed root task, its subtasks, and any session that has had
token motion in the last 30 minutes on one of them. The /live SPA page
boots from this, then opens EventSource('/sse/work?project=...') for
incremental updates (task.changed, drive.heartbeat, agent.run,
tokens.turn) so it never has to re-poll this endpoint.

Data-enrichment slice (gamify): task/subtask nodes also carry spend_usd
(live dollar spend across the task's linked sessions, cached per task
~_SPEND_CACHE_TTL_S so this endpoint stays cheap under polling),
gate_waiting_s (seconds since the CURRENT gate went pending, None when
not pending), and queue_depth (count of not-yet-started children).
Session nodes carry model/role/step off their latest agent_runs row, and
their label becomes "role · model" once known -- the session id itself
stays on `id`, never overwritten.

POST /sim-tokens is a DEV-ONLY door that publishes a tokens.turn event
directly onto the bus, with no real transcript behind it, so
E:\\gamify-lab\\sim\\drive_sim.py can drive reproducible packet motion on
the sandbox instance for recordings. It 404s unless PRISM_DEV_SIM=1 is
set in the environment -- structurally absent everywhere else. Real
token motion NEVER uses this route; it always flows from
services/work_stream.py's transcript ticker.
"""

from __future__ import annotations

import os
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project

router = APIRouter()

# Mirrors services/work_stream.py's _SESSION_RECENCY_S -- kept as a private
# constant here too so this module has no import-time dependency on the
# ticker (and vice versa).
_SESSION_RECENCY_S = 30 * 60

# Per-task live-spend cache: (project, task_id) -> (fetched_at, usd). Spend
# requires walking every linked session's transcript(s) via
# live_spend_for_session, which is its own directory walk + incremental
# file reads -- cheap once warm, but not cheap enough to redo for every
# task on every /live poll. A short TTL keeps the number honest (spend
# only ever grows) while bounding cost.
_SPEND_CACHE_TTL_S = 5.0
_TASK_SPEND_CACHE: dict[str, tuple[float, float]] = {}
_TASK_SPEND_LOCK = threading.Lock()


def _task_node(task_id: str, title: str, status: str, workflow_step: str,
               gate_state: str, activity: dict, spend_usd: float = 0.0,
               gate_waiting_s: float | None = None,
               queue_depth: int = 0,
               drive_started_at: float | None = None) -> dict:
    heartbeat = (activity or {}).get("heartbeat") or {}
    kind = "task"
    return {
        "id": task_id,
        "kind": kind,
        "label": title,
        "status": status,
        "workflow_step": workflow_step or "",
        "gate_state": gate_state or "none",
        "activity_state": (activity or {}).get("state") or "",
        "heartbeat_age_s": heartbeat.get("age_s"),
        "tok_s": None,
        "tokens_total": None,
        "spend_usd": round(spend_usd or 0.0, 4),
        "gate_waiting_s": gate_waiting_s,
        "queue_depth": queue_depth,
        "drive_started_at": drive_started_at,
        "href": f"/tasks/{task_id}",
    }


def _drive_started_at(scores_db: str, task_id: str) -> float | None:
    """Epoch seconds of `task_id`'s EARLIEST agent_runs row -- the /live
    mission clock's server anchor (task 4e6c4bf3 plan S1, AC-1/AC-3).
    None when no telemetry exists yet, never a client-invented start
    time (mx-9f2018: no gauge renders from a value the server didn't
    send for THIS task).

    Reads started_at/recorded_at directly for ALL of the task's rows
    (task 9c6401dc) rather than trusting only the first row of
    get_task_agent_rollup's ORDER BY started_at ASC, recorded_at ASC
    path: SQLite sorts NULL first in ASC order, and every drive step
    deliberately lands TWO rows -- the in-process conductor row with a
    real epoch, and the step agent's curl-ingested row with
    started_at=NULL (its timing lands in recorded_at on ingest instead).
    So the anchor is min(non-null started_at) across every row, falling
    back to min(non-null recorded_at) only when NO row carries a
    started_at at all."""
    from pathlib import Path
    if not scores_db or not Path(scores_db).exists():
        return None
    from prism_service.services import sqlite_db
    from prism_service.services.agent_runs_data import _ts_epoch
    try:
        conn = sqlite_db.connect(scores_db, timeout=5.0)
        try:
            rows = conn.execute(
                "SELECT started_at, recorded_at FROM agent_runs "
                "WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return None
    if not rows:
        return None
    started = [_ts_epoch(r["started_at"]) for r in rows]
    started = [s for s in started if s is not None]
    if started:
        return min(started)
    recorded = [_ts_epoch(r["recorded_at"]) for r in rows]
    recorded = [r for r in recorded if r is not None]
    return min(recorded) if recorded else None


def _queue_depth(task_svc, task_id: str) -> int:
    """Count of `task_id`'s children that are pending AND have never
    entered the conductor (no workflow_step) -- the work queued up behind
    this node, as distinct from work already in flight."""
    try:
        kids = task_svc.list(parent_id=task_id)
    except Exception:
        return 0
    return sum(
        1 for c in kids
        if (getattr(c, "status", "") or "") == "pending"
        and not (getattr(c, "workflow_step", "") or "")
    )


def _task_spend_usd(project: str, task_id: str, task_svc,
                     source_path: str, override_dir: str) -> float:
    """Live USD spend summed over `task_id`'s linked sessions, cached per
    (project, task_id) for _SPEND_CACHE_TTL_S. See module docstring."""
    key = f"{project}\x00{task_id}"
    now = time.time()
    with _TASK_SPEND_LOCK:
        hit = _TASK_SPEND_CACHE.get(key)
        if hit is not None and (now - hit[0]) < _SPEND_CACHE_TTL_S:
            return hit[1]

    from prism_service.services.claude_transcripts import live_spend_for_session

    try:
        sessions = task_svc.sessions_for_task(task_id)
    except Exception:
        sessions = []
    total = 0.0
    for sess in sessions:
        sid = sess.get("session_id")
        if not sid:
            continue
        try:
            spend = live_spend_for_session(
                sid, source_path, override_dir=override_dir or None)
            total += spend["total"]["usd"]
        except Exception:
            continue
    with _TASK_SPEND_LOCK:
        _TASK_SPEND_CACHE[key] = (now, total)
    return total


@router.get("/graph")
def work_graph(project: str = Query("default")) -> dict:
    """One snapshot: {nodes, edges, generated_at}. See module docstring
    for the shape contract the /live SPA page relies on."""
    ctx = get_project(project)
    conductor = ctx.conductor_svc
    task_svc = ctx.task_svc

    nodes: list[dict] = []
    edges: list[dict] = []
    # A child can ALSO be independently conductor-managed (its own
    # workflow_step/gate_state set), which makes managed_tasks() return
    # it a SECOND time as its own top-level entry (mirrors the real
    # board: "an ENGAGED child... MUST surface", conductor_service.py's
    # managed_tasks docstring). De-dupe by id here so the graph gets ONE
    # node per task -- a duplicate id would confuse d3-force's link-by-id
    # force and double-draw the circle client-side.
    seen_node_ids: set[str] = set()

    # Resolved ONCE up front -- both the spend lookup (task/subtask nodes)
    # and the session-recency walk below need it.
    try:
        source_path = conductor._project_source_path()
        override_dir = conductor._project_override_dir()
    except Exception:
        source_path, override_dir = "", ""
    try:
        scores_db = str(ctx._data_dir / "scores.db")
    except Exception:
        scores_db = ""

    roots = conductor.managed_tasks()
    for r in roots:
        if r["id"] not in seen_node_ids:
            task_obj = task_svc.get(r["id"])
            # gamify round5 item 0 fix: managed_tasks() surfaces an
            # INDEPENDENTLY-ENGAGED child (its own workflow_step/gate_state
            # set, e.g. a subtask sitting at a gate) as its own top-level
            # entry -- correctly, per its own docstring ("an ENGAGED child
            # MUST surface"). This loop used to hard-code kind="task" for
            # every entry from `roots` regardless of parent_id, so such a
            # child rendered with the ROOT-task glyph/icon and, if its
            # actual parent root happened to iterate AFTER it in
            # task_svc.list()'s order (not guaranteed parent-first), never
            # got re-classified or wired at all -- verified live against a
            # real scenario run (a story_gate-pending subtask rendered
            # kind="task" with no parent_of edge in the snapshot). Now: a
            # parent_id on the underlying task flips this to "subtask" and
            # adds its own parent_of edge here, so it's correct even if the
            # root's own subtasks loop below never gets to add it first (a
            # duplicate parent_of edge from both paths is harmless --
            # GraphState.ensureEdge is idempotent on re-adding one it
            # already has).
            parent_id = (
                getattr(task_obj, "parent_id", "") or ""
                if task_obj is not None else "")
            node = _task_node(
                r["id"], r["title"], r["status"], r.get("workflow_step"),
                r.get("gate_state"), r.get("activity"),
                spend_usd=_task_spend_usd(
                    project, r["id"], task_svc, source_path, override_dir),
                gate_waiting_s=(
                    conductor.gate_waiting_s(task_obj)
                    if task_obj is not None else None),
                queue_depth=_queue_depth(task_svc, r["id"]),
                drive_started_at=_drive_started_at(scores_db, r["id"]),
            )
            node["kind"] = "subtask" if parent_id else "task"
            nodes.append(node)
            seen_node_ids.add(r["id"])
            if parent_id:
                edges.append({"source": parent_id, "target": r["id"], "kind": "parent_of"})
        for c in r.get("subtasks") or []:
            edges.append({"source": r["id"], "target": c["id"], "kind": "parent_of"})
            if c["id"] in seen_node_ids:
                continue
            child = task_svc.get(c["id"])
            if child is None:
                continue
            try:
                pp = conductor.phase_progress(child.id)
                c_activity = conductor.activity_for(child, pp)
            except Exception:
                c_activity = {}
            cnode = _task_node(
                child.id, child.title, child.status,
                getattr(child, "workflow_step", ""),
                getattr(child, "gate_state", "none"),
                c_activity,
                spend_usd=_task_spend_usd(
                    project, child.id, task_svc, source_path, override_dir),
                gate_waiting_s=conductor.gate_waiting_s(child),
                queue_depth=_queue_depth(task_svc, child.id),
                drive_started_at=_drive_started_at(scores_db, child.id),
            )
            cnode["kind"] = "subtask"
            nodes.append(cnode)
            seen_node_ids.add(child.id)

    # Sessions linked to any task/subtask node, gated to recent token
    # motion (last _SESSION_RECENCY_S) so a stale historical link doesn't
    # clutter the graph with a dead node.
    from prism_service.services.agent_runs_data import get_agent_runs
    from prism_service.services.claude_transcripts import (
        live_token_events_for_session,
        token_turns_from_events,
    )

    now = time.time()
    seen_sessions: set[str] = set()
    for n in list(nodes):
        task_id = n["id"]
        try:
            sessions = task_svc.sessions_for_task(task_id)
        except Exception:
            sessions = []
        for sess in sessions:
            sid = sess.get("session_id")
            if not sid or sid in seen_sessions:
                continue
            try:
                events = live_token_events_for_session(
                    sid, source_path, override_dir=override_dir or None)
            except Exception:
                events = []
            if not events or (now - events[-1][0]) > _SESSION_RECENCY_S:
                continue
            seen_sessions.add(sid)
            tokens_total = sum(int(tok or 0) for _, tok in events)
            turns = token_turns_from_events(events[-5:])
            tok_s = turns[-1]["tok_s"] if turns else None

            # Latest agent_runs row for this session -- role/model/step
            # (item 1 of the gamify data-enrichment slice). Best-effort:
            # a session with no agent_runs telemetry yet (e.g. a plain
            # /implement drive that hasn't ingested a row) just carries
            # None on all three rather than failing the whole graph.
            role = model = step = None
            if scores_db:
                try:
                    run_rows = get_agent_runs(scores_db, limit=1, session_id=sid)
                    if run_rows:
                        role = run_rows[0].get("role")
                        model = run_rows[0].get("model")
                        step = run_rows[0].get("step")
                except Exception:
                    pass
            label = f"{role} · {model}" if role and model else sid[:8]

            nodes.append({
                "id": sid,
                "kind": "session",
                "label": label,
                "status": "",
                "workflow_step": "",
                "gate_state": "",
                "activity_state": "active",
                "heartbeat_age_s": None,
                "tok_s": tok_s,
                "tokens_total": tokens_total,
                "model": model,
                "role": role,
                "step": step,
                "href": f"/sessions/{sid}",
            })
            edges.append({"source": sid, "target": task_id, "kind": "driven_in"})

    return {"nodes": nodes, "edges": edges, "generated_at": now}


@router.post("/sim-tokens")
def sim_tokens(project: str = Query("default"), row: dict = Body(...)) -> dict:
    """Dev-only: publish one tokens.turn event straight onto the bus, no
    transcript required. Gated behind PRISM_DEV_SIM=1 -- 404s (not a 403,
    so it reads as "this route does not exist" rather than "exists but
    refused") on every instance that doesn't opt in, which is every real
    one. See E:\\gamify-lab\\sim\\drive_sim.py, the ONLY intended caller.

    `usd_total` is OPTIONAL passthrough (gamify data-enrichment slice item
    5): the sim scenario doesn't send it yet, but the real ticker's
    tokens.turn payload may in future (see services/work_stream.py's
    comment on why it doesn't today), so this door accepts it now rather
    than needing a second change when the sim catches up."""
    if os.environ.get("PRISM_DEV_SIM") != "1":
        raise HTTPException(status_code=404)

    from prism_service.events import bus

    # gamify round6 item 2 (atomic card+wire): this dev-only door is what
    # the recording rig's scenario actually drives tokens.turn through --
    # see task_service.py's task.changed publish for the full rationale.
    parent_id = ""
    try:
        ctx = get_project(project)
        obj = ctx.task_svc.get(row.get("task_id"))
        parent_id = (getattr(obj, "parent_id", "") or "") if obj else ""
    except Exception:
        pass

    event = {
        "project": project,
        "type": "tokens.turn",
        "task_id": row.get("task_id"),
        "parent_id": parent_id,
        "session_id": row.get("session_id"),
        "out_tokens": row.get("out_tokens", 0),
        "dt_s": row.get("dt_s", 1.0),
        "tok_s": row.get("tok_s", 0),
        "tokens_total": row.get("tokens_total", 0),
        "ts": row.get("ts") or time.time(),
    }
    if row.get("usd_total") is not None:
        event["usd_total"] = row.get("usd_total")
    bus.publish(event)
    return {"ok": True}
