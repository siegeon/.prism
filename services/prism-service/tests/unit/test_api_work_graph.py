"""GET /api/work/graph -- the /live boot snapshot (gamify walking skeleton).

Seeds a real TaskService with one root task (conductor-managed: status
in_progress + a workflow_step) and one child task, drives the REAL
work_graph() handler over a REAL ConductorService (enable_engine=False,
same pattern as test_drive_heartbeat_activity.py's _cond() helper), and
asserts the response contains a task node for the root, a subtask node for
the child, and a parent_of edge between them -- the minimum shape the
/live SPA page needs to paint anything on boot.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _seeded_ctx(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic — walking skeleton")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    child = task_svc.create(title="Child slice — backend publishers", parent_id=root.id)
    task_svc.update(child.id, status="in_progress")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    return ctx, root.id, child.id


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    ctx, root_id, child_id = _seeded_ctx(tmp_path)
    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app), root_id, child_id


def test_route_registered_on_api_router():
    from fastapi import FastAPI
    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/work/graph" in paths, (
        f"GET /api/work/graph not mounted on api_router: {sorted(paths)}")
    assert "/api/work/sim-tokens" in paths, (
        f"POST /api/work/sim-tokens not mounted on api_router: {sorted(paths)}")


def test_graph_returns_task_and_subtask_with_parent_of_edge(tmp_path, monkeypatch):
    client, root_id, child_id = _client(tmp_path, monkeypatch)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    body = resp.json()
    assert "nodes" in body and "edges" in body and "generated_at" in body, (
        f"missing top-level keys; got {sorted(body.keys())}")

    nodes_by_id = {n["id"]: n for n in body["nodes"]}
    assert root_id in nodes_by_id, f"root task node missing; got {body['nodes']!r}"
    assert child_id in nodes_by_id, f"child subtask node missing; got {body['nodes']!r}"

    root_node = nodes_by_id[root_id]
    assert root_node["kind"] == "task", f"got {root_node!r}"
    assert root_node["status"] == "in_progress", f"got {root_node!r}"
    assert root_node["workflow_step"] == "implement_tasks", f"got {root_node!r}"
    assert root_node["href"] == f"/tasks/{root_id}", f"got {root_node!r}"

    child_node = nodes_by_id[child_id]
    assert child_node["kind"] == "subtask", f"got {child_node!r}"
    assert child_node["href"] == f"/tasks/{child_id}", f"got {child_node!r}"

    edges = body["edges"]
    assert any(
        e.get("source") == root_id and e.get("target") == child_id
        and e.get("kind") == "parent_of"
        for e in edges
    ), f"expected a parent_of edge {root_id}->{child_id}; got {edges!r}"


def test_graph_dedupes_a_child_that_is_also_independently_managed(tmp_path, monkeypatch):
    # A subtask that ALSO carries its own workflow_step/gate_state is
    # returned by managed_tasks() a SECOND time as its own top-level
    # entry (real board behavior: "an ENGAGED child... MUST surface").
    # Caught live via the gamify sim: a child sim advanced through
    # /api/conductor/advance rendered as two separate circles with the
    # same id in the /live graph. The node LIST must carry each id once.
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    child = task_svc.create(title="Independently-managed child", parent_id=root.id)
    # The child is ALSO independently in_progress with its own step —
    # this is what makes managed_tasks() surface it a second time.
    task_svc.update(child.id, status="in_progress", workflow_step="write_failing_tests")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    ids = [n["id"] for n in nodes]
    assert ids.count(child.id) == 1, (
        f"child {child.id} must appear exactly once even though it is "
        f"both a subtask AND independently managed; got node ids {ids!r}")
    edges = resp.json()["edges"]
    assert any(
        e.get("source") == root.id and e.get("target") == child.id
        and e.get("kind") == "parent_of" for e in edges
    ), f"parent_of edge must still be recorded; got {edges!r}"


def test_graph_omits_unmanaged_projects_task(tmp_path, monkeypatch):
    # A task that never entered the conductor (no workflow_step, status
    # pending) must not appear -- managed_tasks() is the source of truth.
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    idle = task_svc.create(title="Untouched backlog item")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    ids = {n["id"] for n in resp.json()["nodes"]}
    assert idle.id not in ids, (
        f"an idle, never-managed task must not appear on the live graph; got {ids!r}")


def test_graph_kinds_a_gated_child_as_subtask_even_when_its_root_is_absent(tmp_path, monkeypatch):
    # gamify round5 item 0: managed_tasks() returns an INDEPENDENTLY
    # engaged child (its own workflow_step/gate_state) as its own
    # top-level entry -- the OLD work_graph() loop hard-coded
    # `node["kind"] = "task"` for every entry from that top-level loop
    # regardless of parent_id, so a gated subtask rendered with the
    # root-task icon and, whenever its actual parent root never appeared
    # in `roots` at all (reproduced here: root has no workflow_step of
    # its own, e.g. the brief window before its OWN advance() call has
    # landed), got NO parent_of edge either -- verified live against a
    # real scenario run (a story_gate-pending subtask rendered as a
    # wireless "root" card). The child's kind/edge must be correct from
    # ITS OWN entry, never dependent on the parent also being present.
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic, not yet advanced")
    child = task_svc.create(title="Child sitting at a gate", parent_id=root.id)
    task_svc.update(
        child.id, status="in_progress", workflow_step="story_gate",
        gate_state="pending")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    body = resp.json()
    nodes_by_id = {n["id"]: n for n in body["nodes"]}
    assert root.id not in nodes_by_id, (
        "this test's whole point is the root NOT appearing on its own -- "
        f"got {sorted(nodes_by_id)!r}")
    assert child.id in nodes_by_id, f"got nodes {body['nodes']!r}"
    assert nodes_by_id[child.id]["kind"] == "subtask", (
        f"a gated child must render as a subtask, never the root-task "
        f"kind, even when its parent never appears in `roots`; "
        f"got {nodes_by_id[child.id]!r}")
    edges = body["edges"]
    assert any(
        e.get("source") == root.id and e.get("target") == child.id
        and e.get("kind") == "parent_of" for e in edges
    ), f"the parent_of edge must be synthesized from the child's own entry; got {edges!r}"


# ---------------------------------------------------------------------------
# Data-enrichment slice: model/role/step on session nodes, spend_usd,
# gate_waiting_s, queue_depth on task/subtask nodes.
# ---------------------------------------------------------------------------

def test_session_node_carries_model_role_step_and_label(tmp_path, monkeypatch):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import claude_transcripts as ct
    from prism_service.services import agent_runs_data as ard

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    session_id = "sess-live-role-1"
    task_svc.link_session(root.id, session_id)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path

    import time as _time

    now = _time.time()
    monkeypatch.setattr(
        ct, "live_token_events_for_session",
        lambda sid, sp, override_dir=None: [(now, 500)])
    monkeypatch.setattr(
        ct, "live_spend_for_session",
        lambda sid, sp, override_dir=None: {
            "main": {}, "background": {},
            "total": {"usd": 0.0, "tokens": 0, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        })
    monkeypatch.setattr(
        ard, "get_agent_runs",
        lambda db, limit=1, session_id=None, **kw: [
            {"role": "dev", "model": "claude-sonnet-5", "step": "implement_tasks"}
        ])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    sess_node = next((n for n in nodes if n["id"] == session_id), None)
    assert sess_node is not None, f"session node missing; got {nodes!r}"
    assert sess_node["role"] == "dev", f"got {sess_node!r}"
    assert sess_node["model"] == "claude-sonnet-5", f"got {sess_node!r}"
    assert sess_node["step"] == "implement_tasks", f"got {sess_node!r}"
    assert sess_node["label"] == "dev · claude-sonnet-5", (
        f"label must become 'role · model' once known; got {sess_node!r}")
    assert sess_node["id"] == session_id, (
        "the session id must stay on `id` even once the label changes")


def test_session_node_falls_back_when_no_agent_runs_row(tmp_path, monkeypatch):
    # A session with token motion but no agent_runs telemetry yet must
    # still render -- role/model/step are None, label keeps the sid-prefix
    # fallback, and the field must never raise.
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import claude_transcripts as ct

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    session_id = "sess-live-no-telemetry"
    task_svc.link_session(root.id, session_id)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path

    import time as _time

    now = _time.time()
    monkeypatch.setattr(
        ct, "live_token_events_for_session",
        lambda sid, sp, override_dir=None: [(now, 500)])
    monkeypatch.setattr(
        ct, "live_spend_for_session",
        lambda sid, sp, override_dir=None: {
            "main": {}, "background": {},
            "total": {"usd": 0.0, "tokens": 0, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        })

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    sess_node = next((n for n in nodes if n["id"] == session_id), None)
    assert sess_node is not None, f"session node missing; got {nodes!r}"
    assert sess_node["role"] is None, f"got {sess_node!r}"
    assert sess_node["model"] is None, f"got {sess_node!r}"
    assert sess_node["label"] == session_id[:8], f"got {sess_node!r}"


def test_task_node_carries_spend_usd_from_linked_sessions(tmp_path, monkeypatch):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services import claude_transcripts as ct

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root epic with real spend")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    task_svc.link_session(root.id, "sess-spend-1")

    monkeypatch.setattr(
        ct, "live_spend_for_session",
        lambda sid, sp, override_dir=None: {
            "main": {}, "background": {},
            "total": {"usd": 1.2345, "tokens": 1000, "components": {},
                       "unpriced_tokens": 0, "priced": True},
            "priced": True,
        })
    monkeypatch.setattr(
        ct, "live_token_events_for_session",
        lambda sid, sp, override_dir=None: [])

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    root_node = nodes.get(root.id)
    assert root_node is not None, f"root task must appear; got {resp.json()['nodes']!r}"
    assert root_node["spend_usd"] == 1.2345, f"got {root_node!r}"


def test_task_node_spend_usd_defaults_to_zero_with_no_sessions(tmp_path, monkeypatch):
    client, root_id, child_id = _client(tmp_path, monkeypatch)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    assert nodes[root_id]["spend_usd"] == 0.0, f"got {nodes[root_id]!r}"
    assert nodes[child_id]["spend_usd"] == 0.0, f"got {nodes[child_id]!r}"


def test_queue_depth_counts_pending_never_entered_children(tmp_path, monkeypatch):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root with a queue")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")
    # Queued: pending, never entered the conductor -- counts.
    task_svc.create(title="Queued, untouched", parent_id=root.id)
    # Already entered the conductor (has a workflow_step) -- does NOT count
    # even though its status is still 'pending'.
    started = task_svc.create(title="Already entered conductor", parent_id=root.id)
    task_svc.update(started.id, workflow_step="draft_story")
    # Actively worked -- does NOT count (not queued, in flight).
    in_progress_child = task_svc.create(title="Actively worked", parent_id=root.id)
    task_svc.update(in_progress_child.id, status="in_progress")

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    root_node = nodes.get(root.id)
    assert root_node is not None, f"root must appear; got {resp.json()['nodes']!r}"
    assert root_node["queue_depth"] == 1, (
        f"expected exactly 1 queued (pending, no workflow_step) child; got {root_node!r}")


def test_gate_waiting_s_appears_on_a_pending_gate_node(tmp_path, monkeypatch):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    root = task_svc.create(title="Root at a pending gate")
    task_svc.update(
        root.id, status="in_progress",
        workflow_step="story_gate", gate_state="pending")
    task_svc.record_history(
        root.id, action="advance_task",
        details="from=draft_story; to=story_gate; gate=pending",
        actor="conductor",
    )

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    client = TestClient(app)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    root_node = nodes.get(root.id)
    assert root_node is not None, f"pending-gate task must appear; got {resp.json()['nodes']!r}"
    assert root_node["gate_state"] == "pending", f"got {root_node!r}"
    assert root_node["gate_waiting_s"] is not None, (
        f"a pending gate must carry a non-None gate_waiting_s; got {root_node!r}")
    assert root_node["gate_waiting_s"] >= 0, f"got {root_node!r}"


def test_gate_waiting_s_is_none_when_not_pending(tmp_path, monkeypatch):
    client, root_id, child_id = _client(tmp_path, monkeypatch)

    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    assert nodes[root_id]["gate_waiting_s"] is None, f"got {nodes[root_id]!r}"


# ---------------------------------------------------------------------------
# drive_started_at (task 4e6c4bf3, plan S1, AC-1 + AC-3): a mission clock
# anchor computed from the EARLIEST server-recorded agent_runs row for a
# task, never a client-invented timestamp -- see
# test_live_watchability_ui.py for the matching frontend renderer pins.
# ---------------------------------------------------------------------------

def _ctx_with_scores_db(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db_path = tmp_path / "scores.db"
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=str(scores_db_path))
    conductor = ConductorService(str(scores_db_path), enable_engine=False, task_svc=task_svc)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path
    return ctx, str(scores_db_path), task_svc


def test_task_node_carries_drive_started_at_from_earliest_agent_run(tmp_path, monkeypatch):
    from prism_service.services.agent_runs_data import upsert_agent_run
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    ctx, scores_db, task_svc = _ctx_with_scores_db(tmp_path)
    root = task_svc.create(title="Root epic — mission clock")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")

    # Two agent_runs rows out of chronological insertion order -- the
    # EARLIEST started_at must win regardless of row/insertion order.
    upsert_agent_run(scores_db, {
        "run_id": root.id, "task_id": root.id, "agent_id": f"{root.id}:red",
        "role": "qa", "step": "write_failing_tests", "started_at": "1000000200.0",
    })
    upsert_agent_run(scores_db, {
        "run_id": root.id, "task_id": root.id, "agent_id": f"{root.id}:plan",
        "role": "sm", "step": "verify_plan", "started_at": "1000000100.0",
    })

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    resp = TestClient(app).get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    root_node = nodes.get(root.id)
    assert root_node is not None, f"got nodes {resp.json()['nodes']!r}"
    assert root_node.get("drive_started_at") == 1000000100.0, (
        "drive_started_at must be the EARLIEST agent_runs.started_at for "
        f"this task, not the latest or a client clock; got {root_node!r}")


def test_task_node_drive_started_at_is_none_without_agent_runs(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    ctx, scores_db, task_svc = _ctx_with_scores_db(tmp_path)
    root = task_svc.create(title="Root epic — no runs yet")
    task_svc.update(root.id, status="in_progress", workflow_step="implement_tasks")

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    resp = TestClient(app).get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    root_node = nodes[root.id]
    assert root_node.get("drive_started_at") is None, (
        "a task with no agent_runs telemetry yet must render no mission "
        f"clock rather than a fake one; got {root_node!r}")
