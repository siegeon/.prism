"""Work graph -- the /live screen's boot snapshot + dev-only sim door
(gamify walking skeleton, "PRISM shows its work").

GET /graph assembles ONE snapshot of the live-work node graph: every
conductor-managed root task, its subtasks, and any session that has had
token motion in the last 30 minutes on one of them. The /live SPA page
boots from this, then opens EventSource('/sse/work?project=...') for
incremental updates (task.changed, drive.heartbeat, agent.run,
tokens.turn) so it never has to re-poll this endpoint.

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
import time

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project

router = APIRouter()

# Mirrors services/work_stream.py's _SESSION_RECENCY_S -- kept as a private
# constant here too so this module has no import-time dependency on the
# ticker (and vice versa).
_SESSION_RECENCY_S = 30 * 60


def _task_node(task_id: str, title: str, status: str, workflow_step: str,
               gate_state: str, activity: dict) -> dict:
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
        "href": f"/tasks/{task_id}",
    }


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

    roots = conductor.managed_tasks()
    for r in roots:
        if r["id"] not in seen_node_ids:
            node = _task_node(
                r["id"], r["title"], r["status"], r.get("workflow_step"),
                r.get("gate_state"), r.get("activity"),
            )
            node["kind"] = "task"
            nodes.append(node)
            seen_node_ids.add(r["id"])
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
            )
            cnode["kind"] = "subtask"
            nodes.append(cnode)
            seen_node_ids.add(child.id)

    # Sessions linked to any task/subtask node, gated to recent token
    # motion (last _SESSION_RECENCY_S) so a stale historical link doesn't
    # clutter the graph with a dead node.
    try:
        source_path = conductor._project_source_path()
        override_dir = conductor._project_override_dir()
    except Exception:
        source_path, override_dir = "", ""

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
            nodes.append({
                "id": sid,
                "kind": "session",
                "label": sid[:8],
                "status": "",
                "workflow_step": "",
                "gate_state": "",
                "activity_state": "active",
                "heartbeat_age_s": None,
                "tok_s": tok_s,
                "tokens_total": tokens_total,
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
    one. See E:\\gamify-lab\\sim\\drive_sim.py, the ONLY intended caller."""
    if os.environ.get("PRISM_DEV_SIM") != "1":
        raise HTTPException(status_code=404)

    from prism_service.events import bus

    event = {
        "project": project,
        "type": "tokens.turn",
        "task_id": row.get("task_id"),
        "session_id": row.get("session_id"),
        "out_tokens": row.get("out_tokens", 0),
        "dt_s": row.get("dt_s", 1.0),
        "tok_s": row.get("tok_s", 0),
        "tokens_total": row.get("tokens_total", 0),
        "ts": row.get("ts") or time.time(),
    }
    bus.publish(event)
    return {"ok": True}
