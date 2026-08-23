"""GET /api/work/graph batches its per-node lookups (task 356ffdd2).

Owner 2026-08-17: "please look at the live board, it feels like its not
valid, or the system is locked up." Measured on the real dev daemon with 3
drive lanes + adjudicator active: GET /api/work/graph?project=prism took
>10s (timeout) then 3.9s/7.5s/2.9s across retries for a 10.5KB payload,
while /api/version stayed 2-126ms -- the graph-assembly loop is N+1.

FAILS TODAY because:
  - work.py:87-129's _drive_started_at() issues its own
    `SELECT started_at, recorded_at FROM agent_runs WHERE task_id = ?`
    against scores.db for EVERY root and child node (called at work.py:287
    and :322) -- N nodes means N queries, never batched.
  - work.py:147-181's _gate_actionability() calls
    conductor_api.gate_readiness(task_id=...) once per gate_state=="pending"
    node (work.py:275-277, :309-312), and gate_readiness() (api/conductor.py:
    276-308) UNCONDITIONALLY calls control_plane.dirty_judge_reason() on
    every invocation -- which shells out to `git status --porcelain`
    (control_plane.py:212-238) -- so a dirty checkout re-forks a subprocess
    once per pending-gate node instead of once per /graph request.

The fix (task plan_doc §1-§3) batches both sources into O(1) reads/calls
before the node-building loop runs, and preserves the exact
(owner_actionable, waiting_on) == (True, "you") result the dirty-judge
short-circuit branch already produces today via gate_readiness's own
receipt_ok==True path (AC-5).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _ctx(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path
    return ctx, scores_db, task_svc


def _client(ctx, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api
    from prism_service.api import conductor as conductor_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    # _gate_actionability calls conductor_api.gate_readiness(), which
    # resolves its OWN project context via conductor.py's module-level
    # get_project — a separate binding from work_api's, so it must be
    # pointed at the SAME fake ctx or gate_readiness 404s on an unknown
    # project and _gate_actionability's except-clause swallows it.
    monkeypatch.setattr(conductor_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app)


def _seed_n_managed_tasks(task_svc, n, workflow_step="implement_tasks"):
    ids = []
    for i in range(n):
        t = task_svc.create(title=f"Node {i}")
        task_svc.update(t.id, status="in_progress", workflow_step=workflow_step)
        ids.append(t.id)
    return ids


def _count_drive_started_at_calls(ctx, monkeypatch, n):
    from prism_service.api import work as work_api

    calls = {"n": 0}
    orig = work_api._drive_started_at

    def counting(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(work_api, "_drive_started_at", counting)
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200, resp.text
    return calls["n"]


# ---------------------------------------------------------------------------
# AC-3 (scores.db half): _drive_started_at's per-node query is batched --
# call count must not scale with node count N.
# ---------------------------------------------------------------------------

def test_drive_started_at_lookup_does_not_scale_with_node_count(tmp_path, monkeypatch):
    ctx_small, _, task_svc_small = _ctx(tmp_path / "small")
    _seed_n_managed_tasks(task_svc_small, 2)
    small_calls = _count_drive_started_at_calls(ctx_small, monkeypatch, 2)

    ctx_big, _, task_svc_big = _ctx(tmp_path / "big")
    _seed_n_managed_tasks(task_svc_big, 20)
    big_calls = _count_drive_started_at_calls(ctx_big, monkeypatch, 20)

    assert small_calls <= 1, (
        f"_drive_started_at ran {small_calls} times for 2 graph nodes -- "
        "expected AT MOST 1 (a single batched `WHERE task_id IN (...)` "
        "query run once before the node loop, per work.py:112-116/:287/"
        f":322 today calling it once PER node); got {small_calls} calls"
    )
    assert big_calls <= 1, (
        f"_drive_started_at ran {big_calls} times for 20 graph nodes -- "
        f"expected AT MOST 1, same as for 2 nodes (batched, O(1)); got "
        f"{big_calls} calls, which scales 1:1 with node count today"
    )
    assert big_calls == small_calls, (
        f"_drive_started_at's call count must NOT scale with node count -- "
        f"got {small_calls} calls for 2 nodes vs {big_calls} calls for 20 "
        "nodes; today's per-node loop call means these differ (2 vs 20)"
    )


# ---------------------------------------------------------------------------
# AC-3 (scores.db subprocess half): the dirty-judge git-status shell-out
# inside _gate_actionability -> gate_readiness must run once per REQUEST,
# never once per pending-gate node.
# ---------------------------------------------------------------------------

def _count_dirty_judge_calls(tmp_path_variant, n_pending, monkeypatch):
    from prism_service.services import control_plane as cp_mod

    ctx, _, task_svc = _ctx(tmp_path_variant)
    ids = []
    for i in range(n_pending):
        t = task_svc.create(title=f"Gated node {i}")
        task_svc.update(
            t.id, status="in_progress",
            workflow_step="plan_gate", gate_state="pending")
        ids.append(t.id)

    calls = {"n": 0}

    def fake_dirty():
        calls["n"] += 1
        return "uncommitted edit to conductor_service.py (test double)"

    monkeypatch.setattr(cp_mod, "dirty_judge_reason", fake_dirty)
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200, resp.text
    return calls["n"], ids, resp.json()


def test_dirty_judge_check_runs_once_per_request_not_per_pending_gate_node(
        tmp_path, monkeypatch):
    calls_2, ids_2, _ = _count_dirty_judge_calls(tmp_path / "two", 2, monkeypatch)
    calls_6, ids_6, body_6 = _count_dirty_judge_calls(tmp_path / "six", 6, monkeypatch)

    assert calls_2 <= 1, (
        f"control_plane.dirty_judge_reason() (a `git status --porcelain` "
        f"subprocess shell-out, control_plane.py:212-238) ran {calls_2} "
        "times for 2 pending-gate nodes -- expected AT MOST 1 (hoisted to "
        "run once per /api/work/graph request per the plan's §3, instead "
        "of once per node via _gate_actionability -> conductor_api."
        f"gate_readiness, work.py:147-181/:275-277/:309-312); got {calls_2}"
    )
    assert calls_6 <= 1, (
        f"control_plane.dirty_judge_reason() ran {calls_6} times for 6 "
        f"pending-gate nodes -- expected AT MOST 1, same as for 2 nodes; "
        f"got {calls_6}, which scales 1:1 with pending-gate node count today"
    )
    assert calls_6 == calls_2, (
        "the dirty-judge subprocess call count must NOT scale with the "
        f"number of pending-gate nodes -- got {calls_2} calls for 2 nodes "
        f"vs {calls_6} calls for 6 nodes today"
    )

    # AC-5: whichever node(s) it did evaluate must still land on the exact
    # (owner_actionable, waiting_on) == (True, "you") result gate_readiness's
    # own receipt_ok==True dirty-judge branch produces today (api/
    # conductor.py:303-308 -> work.py:177-178) -- the hoisted short-circuit
    # must reproduce it, not merely skip evaluating it.
    nodes_by_id = {node["id"]: node for node in body_6["nodes"]}
    for tid in ids_6:
        node = nodes_by_id.get(tid)
        assert node is not None, (
            f"pending-gate node {tid} missing from graph; got "
            f"{body_6['nodes']!r}")
        assert node["gate_state"] == "pending", f"got {node!r}"
        assert node["owner_actionable"] is True, (
            "a dirty checkout must short-circuit a pending non-red_gate "
            "node to owner_actionable=True (AC-5, same result "
            f"gate_readiness's own receipt_ok branch gives today); got {node!r}")
        assert node["waiting_on"] == "you", f"got {node!r}"


# ---------------------------------------------------------------------------
# AC-4: batching must not become a stale cached snapshot -- a DB write
# between two /graph calls must be visible on the very next fetch.
# ---------------------------------------------------------------------------

def test_graph_reflects_a_write_made_between_two_consecutive_fetches(
        tmp_path, monkeypatch):
    from prism_service.services.agent_runs_data import upsert_agent_run

    ctx, scores_db, task_svc = _ctx(tmp_path)
    root = task_svc.create(title="Root epic -- freshness")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")

    client = _client(ctx, monkeypatch)

    r0 = client.get("/api/work/graph?project=gamify")
    assert r0.status_code == 200, r0.text
    node0 = {n["id"]: n for n in r0.json()["nodes"]}[root.id]
    assert node0.get("drive_started_at") is None, (
        f"before any agent_runs row exists, drive_started_at must be None; "
        f"got {node0!r}")

    upsert_agent_run(scores_db, {
        "run_id": root.id, "task_id": root.id, "agent_id": f"{root.id}:qa",
        "role": "qa", "step": "write_failing_tests", "started_at": "1000000000.0",
    })

    r1 = client.get("/api/work/graph?project=gamify")
    assert r1.status_code == 200, r1.text
    node1 = {n["id"]: n for n in r1.json()["nodes"]}[root.id]
    assert node1.get("drive_started_at") == 1000000000.0, (
        "a change written to scores.db between two /graph fetches must be "
        "visible on the VERY NEXT fetch -- batching must be a per-request "
        f"read, never a memoized/cached snapshot; got {node1!r}")


# ---------------------------------------------------------------------------
# AC-6: the graph payload projects only the fields the /live view needs --
# no full task row (e.g. plan_doc) leaks into the response.
# ---------------------------------------------------------------------------

def test_graph_response_never_carries_plan_doc(tmp_path, monkeypatch):
    ctx, _, task_svc = _ctx(tmp_path)
    root = task_svc.create(title="Root epic with a big plan_doc")
    task_svc.update(
        root.id, status="in_progress", workflow_step="implement_tasks",
        plan_doc="## Plan\n" + ("x" * 5000))

    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for node in body["nodes"]:
        assert "plan_doc" not in node, (
            f"graph node must not carry the full task row (plan_doc) -- "
            f"the batched read must project only the columns the /live "
            f"view needs (AC-6); got node {node!r}")
