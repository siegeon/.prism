"""RED scaffold — UI-FIRST conductor gate + admin surfaces (task 3c706250).

Pins the USER-FACING seams end-to-end, not unit contracts:

Half A (backend gate/advance HTTP):
  * POST /api/conductor/gate exists in api/conductor.py, accepts
    {task_id, action, reason, override?}, delegates to the SAME
    ConductorService.gate_decide the MCP tool uses, and the decision +
    reason PERSIST (a SEPARATE GET /api/conductor/state shows the task's
    gate_state/gate_reason flipped).
  * POST /api/conductor/advance exists and delegates to advance_task.

Half B/C (frontend raw-dump + dead-page hygiene) — asserted by scanning
the real web/src render paths on disk:
  * ConsolidationPage.tsx no longer JSON.stringify(scope) nor <pre> for
    the reflection result.
  * MemoryDetailPage.tsx description not a raw <pre>; evidence not
    JSON.stringify.
  * TaskDetailPage.tsx renders Approve/Reject + a REQUIRED reason
    textarea + override checkbox wired to /api/conductor/gate, replacing
    the bare status PATCH.
  * BrainPage.tsx and GraphPage.tsx are DELETED; App.tsx imports none of
    them; /graph and /explore still resolve (redirect to /brain).

Half D (admin surfaces):
  * A drift/staleness card with a re-sync action exists and is wired to
    api/staleness.py.
  * MemoryDetailPage has edit/retire/supersede actions.
  * LearningPage Tier-3 knobs are editable (not view-only).
  * ConductorPage has a 'create task -> enter conductor' affordance.

ALL must FAIL today: the HTTP routes 404, the pages still dump raw
JSON/<pre>, BrainPage/GraphPage still exist, and the admin affordances
are absent.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
# .../services/prism-service/tests/integration/<file> -> service root is 3 up.
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_PAGES = _WEB_SRC / "pages"


# =====================================================================
# Half A — backend gate/advance HTTP seam (TestClient against the real
# conductor router, delegating to a real ConductorService + TaskService).
# =====================================================================


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"),
        enable_engine=False,
        task_svc=task_svc,
    )
    return task_svc, cond


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import conductor as conductor_api

    task_svc, cond = _services(tmp_path)

    class _Ctx:
        conductor_svc = cond

    monkeypatch.setattr(conductor_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    return TestClient(app), task_svc, cond


def _drive_to_red_gate(task_svc, cond, task_id):
    """Walk a fresh task up to the red_gate step (gate_state='pending'),
    clearing the earlier story/plan gates (task 8579d49e) with a plain
    approve (no verifier attached -> trust-caller)."""
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this admin-surface walk (unrelated to premise content) can
    # leave review_previous_notes.
    task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising the conductor gate/advance "
        "admin surfaces, not a real premise claim - UNVERIFIED\n"))
    for _ in range(20):
        t = task_svc.get(task_id)
        if t.workflow_step == "red_gate":
            return
        if t.gate_state == "pending":
            cond.gate_decide(task_id, action="approve",
                             reason="walk intermediate")
            continue
        out = cond.advance_task(task_id)
        if not out.get("ok"):
            break
    t = task_svc.get(task_id)
    assert t.workflow_step == "red_gate", (
        f"expected red_gate, got {t.workflow_step!r}"
    )


def test_post_conductor_gate_approve_persists_decision(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    t = task_svc.create(title="gate me")
    _drive_to_red_gate(task_svc, cond, t.id)

    # No verifier attached -> override=True so approve releases the gate.
    resp = client.post(
        "/api/conductor/gate",
        params={"project": "prism"},
        json={
            "task_id": t.id,
            "action": "approve",
            # Proof-carrying contract (task 3826dac3): an override red_gate
            # close must carry a real failing-test trace.
            "reason": "red trace looked good; independent re-run: pytest -> 1 failed",
            "override": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True, body
    assert body.get("gate_state") == "passed", body

    # SEPARATE call: the state endpoint must reflect the persisted decision.
    state = client.get("/api/conductor/state", params={"project": "prism"})
    assert state.status_code == 200, state.text
    rows = {row["id"]: row for row in state.json().get("managed_tasks", [])}
    assert t.id in rows, "approved task must show in managed_tasks"
    assert rows[t.id]["gate_state"] != "pending"


def test_post_conductor_gate_reject_stores_reason(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    t = task_svc.create(title="reject me")
    _drive_to_red_gate(task_svc, cond, t.id)

    resp = client.post(
        "/api/conductor/gate",
        params={"project": "prism"},
        json={
            "task_id": t.id,
            "action": "reject",
            "reason": "trace did not fail for the right reason",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Superseded by test_gate_reject_rewinds_to_producing_step.py: reject
    # now rewinds to the gate's producing step instead of dead-ending at
    # gate_state="failed".
    assert body.get("gate_state") == "none", body
    assert body.get("rewound_to") == "write_failing_tests", body

    # Reason persisted onto the task row (read back via the service).
    fetched = task_svc.get(t.id)
    assert fetched.workflow_step == "write_failing_tests"
    assert fetched.gate_state == "none"
    assert fetched.gate_reason == "trace did not fail for the right reason"


def test_post_conductor_advance_moves_task(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    t = task_svc.create(title="advance me")  # workflow_step=''

    resp = client.post(
        "/api/conductor/advance",
        params={"project": "prism"},
        # session_id: the conductor session gate (ef81fc15) refuses a
        # sessionless ENTRY into the workflow — an advance that hands the
        # task to the conductor must carry the driving session.
        json={"task_id": t.id, "validation": "ok", "session_id": "S-adm"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("ok") is True, body
    assert body.get("to_step") == "review_previous_notes", body

    # Persisted: a separate fetch shows the task moved off step 0-empty.
    assert task_svc.get(t.id).workflow_step == "review_previous_notes"


def test_managed_tasks_surface_engaged_children_only(tmp_path, monkeypatch):
    """Conductor visibility follows ENGAGEMENT, not tree position.

    Re-pinned for the ui-redesign LIVE bar (owner 2026-07-14: 'I cannot see
    the live task'): a child the conductor is actively engaged with (a live
    workflow step or a gate awaiting decision) MUST surface in managed_tasks
    — epic workstreams are children, and the LIVE bar's promise is 'who's
    working now + what's stuck at a gate'. An IDLE child (no step, no gate)
    still stays under its parent's detail page — the original clutter
    regression stays covered.
    """
    _, task_svc, cond = _client(tmp_path, monkeypatch)

    parent = task_svc.create(title="parent epic")
    _drive_to_red_gate(task_svc, cond, parent.id)

    child = task_svc.create(title="verify subtask", parent_id=parent.id)
    _drive_to_red_gate(task_svc, cond, child.id)

    idle_child = task_svc.create(title="idle subtask", parent_id=parent.id)

    ids = {row["id"] for row in cond.managed_tasks()}
    assert parent.id in ids, "top-level task must show on /conductor"
    assert child.id in ids, \
        "an ENGAGED child (live step/gate) must surface — LIVE bar promise"
    assert idle_child.id not in ids, \
        "an idle child must NOT show as a standalone tile"

    # step_buckets must agree: both engaged tasks count at red_gate.
    assert cond.step_buckets().get("red_gate") == 2, cond.step_buckets()


# =====================================================================
# Half B/C — frontend raw-dump + dead-page hygiene (disk scan of the
# real SPA source). These pin the USER-FACING render, not a unit shim.
# =====================================================================


def _read(page: str) -> str:
    p = _PAGES / page
    assert p.exists(), f"{page} missing"
    return p.read_text(encoding="utf-8")


def test_consolidation_page_no_raw_scope_or_reflection_dump():
    src = _read("ConsolidationPage.tsx")
    assert "JSON.stringify(preview.scope" not in src, (
        "scope must render as a structured tree, not JSON.stringify"
    )
    # The reflection result <pre> at ~:581 must be gone.
    assert "<pre" not in src, (
        "ConsolidationPage must not render raw <pre> dumps"
    )


def test_memory_detail_page_no_raw_pre_or_json_dump():
    src = _read("MemoryDetailPage.tsx")
    assert "<pre" not in src, (
        "MemoryDetailPage description/evidence must not use raw <pre>"
    )
    assert "JSON.stringify(entry.evidence" not in src, (
        "evidence must render as a key/value table with links"
    )


def test_brain_and_graph_pages_deleted():
    assert not (_PAGES / "BrainPage.tsx").exists(), "BrainPage.tsx must be deleted"
    assert not (_PAGES / "GraphPage.tsx").exists(), "GraphPage.tsx must be deleted"
    app = (_WEB_SRC / "App.tsx").read_text(encoding="utf-8")
    assert "BrainPage" not in app, "App.tsx must not import BrainPage"
    assert "GraphPage" not in app, "App.tsx must not import GraphPage"
    # /graph and /explore must still resolve (redirect to /brain).
    assert '/explore' in app and '/graph' in app and '/brain' in app


# =====================================================================
# Half A (frontend) — TaskDetail gate controls
# =====================================================================


def test_task_detail_page_has_operable_gate_controls():
    # v6.7.30 folded the gate controls into PlanView's Implementation tab:
    # TaskDetailPage still owns the /api/conductor/gate WIRING (gateDecide) and
    # delegates the operable Approve/Reject + required reason textarea + override
    # checkbox to the render owner PlanView via the `gate` prop. Re-pointed the
    # control assertions to PlanView, keeping the endpoint wiring on TaskDetail.
    plan = (_WEB_SRC / "components" / "plan" / "PlanView.tsx").read_text(
        encoding="utf-8"
    )
    # Approve / Reject controls.
    assert re.search(r"[Aa]pprove", plan), "missing Approve control"
    assert re.search(r"[Rr]eject", plan), "missing Reject control"
    # A reason textarea (required) + an override checkbox.
    assert "textarea" in plan.lower(), "missing required reason textarea"
    assert re.search(r"override", plan, re.IGNORECASE), "missing override checkbox"
    # Wired to the new HTTP endpoint, not the bare status PATCH.
    detail = _read("TaskDetailPage.tsx")
    assert "/api/conductor/gate" in detail or "conductor/gate" in detail, (
        "gate controls must POST to the conductor gate endpoint"
    )


# =====================================================================
# Half D — admin surfaces
# =====================================================================


def test_staleness_drift_card_exists_and_wired():
    """A drift/staleness card somewhere in web/src that hits /api/staleness."""
    hits = []
    for p in _WEB_SRC.rglob("*.tsx"):
        txt = p.read_text(encoding="utf-8", errors="ignore")
        if "/api/staleness" in txt or "api/staleness" in txt:
            hits.append(p)
    assert hits, "no SPA surface fetches /api/staleness (orphaned re-sync)"
    # And a re-sync affordance lives alongside it.
    joined = "\n".join(p.read_text(encoding="utf-8", errors="ignore") for p in hits)
    assert re.search(r"re-?sync", joined, re.IGNORECASE), (
        "staleness surface must offer a re-sync action"
    )


def test_memory_detail_has_edit_retire_supersede_actions():
    src = _read("MemoryDetailPage.tsx")
    for verb in ("edit", "retire", "supersede"):
        assert re.search(verb, src, re.IGNORECASE), (
            f"MemoryDetailPage missing a {verb} action"
        )


def test_learning_page_tier3_knobs_editable():
    src = _read("LearningPage.tsx")
    # Editable = a write path (input/save/POST), not just a read render.
    has_write = (
        re.search(r"<input", src, re.IGNORECASE)
        or re.search(r"method:\s*['\"]POST", src)
        or "onSave" in src
    )
    assert has_write, "LearningPage Tier-3 knobs must be editable, not view-only"


def test_conductor_page_is_purely_visual_work_progress():
    """v6.3.19 redesigned /conductor as flat tiles that are PURELY the visual
    representation of work happening to each task — the v6.3.17 'create task ->
    enter conductor' onboarding affordance (and the KPI counts header) were
    deliberately removed (commit 1b57471). Pin that the page renders the work
    tiles and no longer carries the create-task affordance, so the stale
    v6.3.17 contract can't silently creep back in."""
    src = _read("ConductorPage.tsx")
    # The page still renders the conductor work surface (tiles / managed tasks).
    assert re.search(r"conductor", src, re.IGNORECASE), src[:200]
    # The removed create-task affordance must stay gone (only `created_at`
    # field reads are allowed — those are not the affordance).
    assert not re.search(r"create[\s_-]{0,3}task", src, re.IGNORECASE), (
        "ConductorPage create-task affordance was intentionally removed in "
        "v6.3.19 (purely-visual work-progress page) — it must not return"
    )
