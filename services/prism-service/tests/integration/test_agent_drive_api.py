"""RED — POST /api/agent/drive: one call through the planning gates
(task 4e28dcab, C4 of the PI-orchestration build, parent 81b23574 FR-4).

The PI surfaces hand-step conductor tools through text interception under
a 6-call budget, so full planning drives essentially never complete. The
drive endpoint is the ONE door: it accepts {ask?, task_id?, project?,
session_id?}, creates the task from the ask when no task_id is given, and
runs services/drive_engine.DriveEngine.plan — the deterministic
server-side state machine — walking the task through story_gate +
plan_gate with zero overrides. Every conductor step is telemetered
through the EXISTING ingest paths: a pi_runs row per step (purpose
"drive:<step>" — the step label rides purpose because the manifest
schema is fixed) and an agent_runs row per step (real `step` column).

FAIL today: the /api/agent/drive route does not exist (404).
"""

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import prism_service.services.pi_run_log as prl
    runs_dir = tmp_path / "pi_runs"
    monkeypatch.setattr(prl, "_RUNS_DIR", runs_dir)
    monkeypatch.setattr(prl, "_MANIFEST", runs_dir / "manifest.jsonl")

    from fastapi.testclient import TestClient
    from prism_service.main import app

    c = TestClient(app)
    c.post("/api/projects", json={"name": "drivetest"})
    # plan_gate scores conformance against seeded principles; an empty
    # principle set NEVER passes (issue #171) — seed the defaults the
    # way prism_onboard does.
    from prism_service.project_context import get_project
    from prism_service.services.arc_governance import (
        seed_default_principles,
    )
    seed_default_principles(get_project("drivetest").memory_svc)
    return c


def _drive(client, body):
    return client.post(
        "/api/agent/drive", params={"project": "drivetest"}, json=body,
    )


def test_drive_from_ask_walks_to_plan_gate_passed(client):
    """AC-1: one POST with an ask creates a task and drives it to
    plan_gate PASSED with ZERO overrides — the whole planning half in a
    single HTTP call, no text-intercepted tool stepping."""
    r = _drive(client, {"ask": "Add an export button to the widget list"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("created") is True, body
    assert body.get("task_id"), body
    assert body.get("final_step") == "plan_gate", body
    assert body.get("gate_state") == "passed", body
    stats = body.get("stats") or {}
    assert stats.get("overrides") == 0, stats

    # The driven task's artifacts persisted with the rubric shape.
    rt = client.get(
        f"/api/tasks/{body['task_id']}", params={"project": "drivetest"},
    )
    assert rt.status_code == 200, rt.text
    task = rt.json().get("task") or {}
    assert "## Acceptance Criteria" in (task.get("plan_doc") or ""), task
    assert (task.get("plan_diagram") or "").startswith("flowchart"), task


def test_drive_emits_step_labeled_rows_in_both_ledgers(client):
    """AC-2: the drive's telemetry rows carry per-SDLC-step labels —
    pi_runs rows as purpose='drive:<step>', agent_runs rows in the real
    `step` column — via the EXISTING ingest paths."""
    import prism_service.services.pi_run_log as prl

    r = _drive(client, {"ask": "Nightly digest email for stale tasks"})
    assert r.status_code == 200, r.text
    task_id = r.json().get("task_id")
    assert task_id, r.text

    rows = prl.list_recent(limit=50, project="drivetest", task_id=task_id)
    assert rows, "no pi_runs rows for the drive"
    labels = {row.get("purpose", "") for row in rows}
    for step in ("draft_story", "story_gate", "verify_plan", "plan_gate"):
        assert f"drive:{step}" in labels, (step, sorted(labels))
    assert all(row.get("backend") == "drive" for row in rows), rows

    ar = client.get(
        "/api/agent-runs",
        params={"project": "drivetest", "task_id": task_id},
    )
    assert ar.status_code == 200, ar.text
    steps = {row.get("step") for row in ar.json().get("rows", [])}
    for step in ("draft_story", "story_gate", "verify_plan", "plan_gate"):
        assert step in steps, (step, sorted(s or "" for s in steps))


def test_drive_existing_task_id_no_creation(client):
    """A pre-created task is driven in place: created:false and the
    response task_id echoes the input."""
    ct = client.post(
        "/api/tasks",
        params={"project": "drivetest"},
        json={"title": "Pre-created drive target"},
    )
    assert ct.status_code == 200, ct.text
    task_id = (ct.json().get("task") or {}).get("id")
    assert task_id, ct.text

    r = _drive(client, {"task_id": task_id})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("created") is False, body
    assert body.get("task_id") == task_id, body
    assert body.get("ok") is True, body
    assert body.get("gate_state") == "passed", body


def test_missing_ask_and_task_id_is_422(client):
    """AC-3: neither ask nor task_id -> 422 (contract error)."""
    r = _drive(client, {})
    assert r.status_code == 422, r.text


def test_unknown_project_is_404(client):
    r = client.post(
        "/api/agent/drive",
        params={"project": "no-such-project"},
        json={"ask": "anything"},
    )
    assert r.status_code == 404, r.text


def test_unknown_task_id_is_structured_refusal(client):
    """AC-3: a nonexistent task_id is the engine's own structured
    refusal — 200 {ok:false, reason} naming the task, never a 500."""
    r = _drive(client, {"task_id": "does-not-exist"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is False, body
    assert "does-not-exist" in str(body.get("reason", "")), body
