"""A 1-second live channel for the Workflows canvas (owner: "Nodes should be
very fast linked to view").

`api.workflows.get_workflows` answers the FULL catalog -- AosWorkflows engine
JSON fetches, per-node token trend, role bots, tiers -- and measured 21-52s
under load on the live instance, so the canvas lagged a node's real position
by 20-60s even though the underlying live signal
(`drive_heartbeat.latest_many`, one query, <6ms for 2,000 tasks) is tiny.
This module answers ONLY that tiny signal: which FSM step and which
behaviour sub-node each live task is standing on right now, plus who/what
is driving it. No engine HTTP, no trend, no role/tier computation.

Exactly ONE `drive_heartbeat.latest_many` call and ONE task listing per
call -- the same discipline `get_workflows` had to learn the hard way (see
its own docstring: a first version called `drive_heartbeat.latest()` and
`svc.list()` inside a nested per-behaviour-entry loop and measured >90s
under real write contention).

`_BEHAVIOUR_FOR_STEP`/`_STEP_FOR_BEHAVIOUR` and `OPEN_DISPATCH_TOOLS` below
are DELIBERATE duplicates of the equivalent names in api/workflows.py
(`_BEHAVIOUR_FOR_STEP` and a sibling pass's `_OPEN_DISPATCH_TOOLS`) --
kept byte-identical on purpose rather than imported, so this module never
takes a dependency on that file's own concurrent edits. A later pass should
unify the two into one shared source; until then, keep the values identical.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from prism_service.models.workflow import WORKFLOW_STEPS
from prism_service.project_context import get_project
from prism_service.services import drive_heartbeat, sqlite_db

# See module docstring: duplicated from api/workflows.py's _BEHAVIOUR_FOR_STEP
# on purpose. Step id -> behaviour id.
_BEHAVIOUR_FOR_STEP: dict[str, str] = {
    "verify_green_state": "validation",
    "story_gate": "story-gate-check",
    "plan_gate": "plan-gate-check",
    "draft_story": "draft-story-loop",
    "review_previous_notes": "review-previous-notes-loop",
    "verify_plan": "verify-plan-loop",
    "write_failing_tests": "write-failing-tests-loop",
    "implement_tasks": "implement-tasks-loop",
    "red_gate": "red-gate-status",
    "green_gate": "green-gate-status",
}
# Behaviour id -> step id (inverted, same as api/workflows.py's own).
_STEP_FOR_BEHAVIOUR: dict[str, str] = {
    v: k for k, v in _BEHAVIOUR_FOR_STEP.items()}

# A beat's `last_tool` naming one of these means the driver has handed off
# to an open-ended model/tool call right now, not running a fixed codified
# step. Kept identical to sibling G's `_OPEN_DISPATCH_TOOLS` in
# api/workflows.py -- see module docstring.
OPEN_DISPATCH_TOOLS: frozenset[str] = frozenset({
    "dispatch_guard_live", "claude_cli.invoke",
})

# Parsed behaviour step lists, cached per (path, mtime) so a poll every
# second never re-reads and re-parses the same JSON file off disk unless it
# actually changed.
_BEHAVIOUR_STEPS_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _load_behaviour_steps(path: Path) -> list[dict]:
    """The `[{"id", "route"}, ...]` step list a behaviour JSON file declares,
    or `[]` when the file is missing/unparseable. `route` is the tail of a
    step's `url` after `/steps/` (before any `?`), same derivation
    `_conductor_behavior_workflows` uses -- empty when the step has no url
    naming a route (e.g. a `shell` step)."""
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return []
    cached = _BEHAVIOUR_STEPS_CACHE.get(str(path))
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    steps: list[dict] = []
    for step in doc.get("steps") or []:
        step_id = step.get("id")
        if not step_id:
            continue
        url = str(step.get("url") or "")
        route = url.split("/steps/")[-1].split("?")[0] if "/steps/" in url else ""
        steps.append({"id": step_id, "route": route})
    _BEHAVIOUR_STEPS_CACHE[str(path)] = (mtime, steps)
    return steps


def _behaviour_ids(root: Path) -> list[str]:
    """Every behaviour id the conductor bot declares, read from the
    versioned `bot.json` (its `fsms[].behaviorIds`) -- never the AosWorkflows
    engine, which is the HTTP round trip this module exists to avoid.
    Falls back to listing `*.json` files in the directory (excluding
    `bot.json` itself) when the manifest is missing or unreadable, so a
    behaviour with no manifest entry yet still lights."""
    behaviours_dir = root / ".prism" / "behaviors" / "conductor"
    try:
        doc = json.loads((behaviours_dir / "bot.json").read_text(encoding="utf-8"))
        ids: list[str] = []
        for fsm in doc.get("fsms") or []:
            for bid in fsm.get("behaviorIds") or []:
                if bid not in ids:
                    ids.append(bid)
        if ids:
            return ids
    except Exception:
        pass
    try:
        return sorted(p.stem for p in behaviours_dir.glob("*.json")
                       if p.stem != "bot")
    except OSError:
        return []


def _live_node(task_id: str, beat: dict) -> dict:
    tool = str(beat.get("last_tool") or "")
    return {
        "task_id": task_id,
        "driver": str(beat.get("driver") or ""),
        "tool": tool,
        "node": str(beat.get("node") or ""),
        "age_s": round(float(beat["age_s"]), 3),
        "since": str(beat.get("last_progress_at") or ""),
        "dispatching": tool in OPEN_DISPATCH_TOOLS,
    }


def _freshest(task_ids: list[str], heartbeats: dict[str, dict]):
    """The (task_id, beat) pair with the smallest age_s among `task_ids`
    that has a beat fresher than HEARTBEAT_WINDOW_S, or (None, None) when
    none qualifies -- a stale beat lights nothing."""
    best_id = None
    best_beat = None
    for tid in task_ids:
        beat = heartbeats.get(tid)
        if beat is None or beat["age_s"] > drive_heartbeat.HEARTBEAT_WINDOW_S:
            continue
        if best_beat is None or beat["age_s"] < best_beat["age_s"]:
            best_id, best_beat = tid, beat
    return best_id, best_beat


def live_for_project(project: str) -> dict:
    """The tiny live channel: which FSM step and which behaviour sub-node
    each live task is standing on right now, plus who/what is driving it.

    Shape:
        {"ts": <epoch>,
         "entries": {<behaviour_id>: {"occupancy": {<step_id>: 0|1},
                                       "live": {<step_id>: {...}}}},
         "conductor": {"occupancy": {<fsm_step_id>: <count>},
                       "live": {<fsm_step_id>: {...}}}}

    Exactly one `drive_heartbeat.latest_many` call and one task listing
    (a single SQL query) run per call, regardless of how many behaviour
    entries exist on disk.
    """
    ts = time.time()
    _empty_conductor = {"occupancy": {s["id"]: 0 for s in WORKFLOW_STEPS},
                        "live": {}}
    try:
        proj = get_project(project)
        svc = proj.task_svc
    except Exception:
        return {"ts": ts, "entries": {}, "conductor": _empty_conductor}

    scores_db_dir = getattr(proj, "_data_dir", None)
    db_path = getattr(svc, "_db_path", None)

    # ONE task listing: id + workflow_step for every non-terminal task, via
    # an independent short-timeout read-only connection -- a UI poll must
    # never queue behind a long task-store writer (same discipline
    # api.workflows._occupancy uses).
    task_steps: dict[str, str] = {}
    if db_path:
        try:
            with sqlite_db.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=0.25,
            ) as conn:
                rows = conn.execute(
                    "SELECT id, workflow_step FROM tasks "
                    "WHERE status NOT IN ('done', 'cancelled', 'deleted')"
                ).fetchall()
            for tid, step in rows:
                if tid:
                    task_steps[tid] = step or ""
        except Exception:
            task_steps = {}

    # ONE latest_many call.
    heartbeats: dict[str, dict] = {}
    if scores_db_dir is not None and task_steps:
        try:
            heartbeats = drive_heartbeat.latest_many(
                str(scores_db_dir / "scores.db"), task_steps.keys())
        except Exception:
            heartbeats = {}

    tasks_by_step: dict[str, list[str]] = {}
    for tid, step in task_steps.items():
        tasks_by_step.setdefault(step, []).append(tid)

    conductor_step_ids = [s["id"] for s in WORKFLOW_STEPS]
    conductor_occupancy = {
        sid: len(tasks_by_step.get(sid, [])) for sid in conductor_step_ids}
    conductor_live: dict[str, dict] = {}
    for sid in conductor_step_ids:
        tid, beat = _freshest(tasks_by_step.get(sid, []), heartbeats)
        if tid is not None:
            conductor_live[sid] = _live_node(tid, beat)

    from prism_service.services.claude_transcripts import _project_source_path
    configured = Path(_project_source_path(project))
    fallback = Path.home() / "projects" / project
    root = configured if configured.is_absolute() and configured.exists() else fallback

    entries: dict[str, dict] = {}
    if root.exists():
        behaviours_dir = root / ".prism" / "behaviors" / "conductor"
        for bid in _behaviour_ids(root):
            steps = _load_behaviour_steps(behaviours_dir / f"{bid}.json")
            if not steps:
                continue
            occupancy = {s["id"]: 0 for s in steps}
            live: dict[str, dict] = {}
            fsm_step = _STEP_FOR_BEHAVIOUR.get(bid)
            if fsm_step:
                tid, beat = _freshest(tasks_by_step.get(fsm_step, []), heartbeats)
                if tid is not None:
                    entry_point_id = steps[0]["id"]
                    route_to_id = {s["route"]: s["id"] for s in steps if s.get("route")}
                    node = str(beat.get("node") or "")
                    lit = route_to_id.get(node, entry_point_id) if node else entry_point_id
                    occupancy[lit] = 1
                    live[lit] = _live_node(tid, beat)
            entries[bid] = {"occupancy": occupancy, "live": live}

    return {
        "ts": ts,
        "entries": entries,
        "conductor": {"occupancy": conductor_occupancy, "live": conductor_live},
    }
