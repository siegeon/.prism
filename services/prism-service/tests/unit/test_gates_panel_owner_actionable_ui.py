"""Gates panel lists only gates you can act on (task edf38154).

The /live board's docked "GATES WAITING (n)" panel renders every
gate_state=="pending" node with one identical alarm treatment, so a
rollup-blocked epic (structurally NOT actionable by the owner) reads
exactly like a genuinely actionable green gate. Owner, 2026-08-17: "this
says im waiting on gates but im not, there is nothing i can do for them".

This pins the fix: work.py derives owner_actionable/waiting_on by CALLING
gate_readiness (api/conductor.py:276) -- never re-implementing its teeth
(mx-d6c1df) -- only for gate_state=="pending" nodes, and gatepanel.ts
splits the panel into a "YOUR REVIEW" section and a dim "waiting on
others" section keyed on that field.

Half drives the REAL work_graph() handler over a real TaskService +
ConductorService (same pattern as test_api_work_graph.py); half reads
the TS source directly, since the SPA has no JS test runner (same
convention as test_conductor_page_animated_cleanup_ui.py /
test_live_graph_visual_grammar_ui.py).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import MagicMock

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_LIVE = _SRC / "live"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Backend: /api/work/graph carries owner_actionable + waiting_on
# ---------------------------------------------------------------------------

def _seeded_ctx(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    conductor = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.conductor_svc = conductor
    ctx.task_svc = task_svc
    ctx._data_dir = tmp_path
    return ctx, task_svc


def _client(ctx, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import work as work_api

    monkeypatch.setattr(work_api, "get_project", lambda p: ctx)
    app = FastAPI()
    app.include_router(work_api.router, prefix="/api/work")
    return TestClient(app)


def _pending_root(task_svc, workflow_step="story_gate"):
    root = task_svc.create(title="Root at a pending gate")
    task_svc.update(
        root.id, status="in_progress",
        workflow_step=workflow_step, gate_state="pending")
    return root


def _patched_readiness(monkeypatch, retval=None, side_effect=None):
    """Patches the SAME symbol work.py's function-local import resolves
    (prism_service.api.conductor.gate_readiness) and returns the mock so
    call-count/args can be asserted."""
    import prism_service.api.conductor as conductor_api
    mock = MagicMock()
    if side_effect is not None:
        mock.side_effect = side_effect
    else:
        mock.return_value = retval
    monkeypatch.setattr(conductor_api, "gate_readiness", mock)
    return mock


def test_pending_node_payload_carries_owner_actionable_and_waiting_on(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc)
    _patched_readiness(monkeypatch, {
        "receipt_ok": True, "manual_review": True,
        "receipt": {"adapter": "human", "status": "your_review"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    root_node = nodes[root.id]
    assert "owner_actionable" in root_node, f"got {root_node!r}"
    assert "waiting_on" in root_node, f"got {root_node!r}"
    assert root_node["owner_actionable"] is True, f"got {root_node!r}"
    assert root_node["waiting_on"] == "you", f"got {root_node!r}"


def test_derivation_calls_gate_readiness_and_owns_no_second_implementation(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc)
    mock = _patched_readiness(monkeypatch, {
        "receipt_ok": True, "manual_review": True,
        "receipt": {"adapter": "human", "status": "your_review"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    assert mock.called, "work.py must derive owner_actionable by CALLING gate_readiness"
    call_args, call_kwargs = mock.call_args
    assert call_kwargs.get("task_id") == root.id or root.id in call_args, (
        f"gate_readiness must be called for the pending node's task id; "
        f"got call_args={mock.call_args!r}")

    work_src = _read(_SERVICE_ROOT / "prism_service" / "api" / "work.py")
    for forbidden in ("epic_rollup_verdict", "oracle_spec", "arc_governance"):
        assert forbidden not in work_src, (
            f"work.py must not re-implement gate_readiness's teeth by "
            f"importing {forbidden!r} -- call gate_readiness instead (mx-d6c1df)")


def test_readiness_is_called_once_per_pending_node_and_never_otherwise(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    _pending_root(task_svc, workflow_step="story_gate")
    not_pending = task_svc.create(title="Root not at a gate")
    task_svc.update(not_pending.id, status="in_progress",
                     workflow_step="implement_tasks", gate_state="none")
    also_not_pending = task_svc.create(title="Another non-gated root")
    task_svc.update(also_not_pending.id, status="in_progress",
                     workflow_step="verify_green_state", gate_state="none")
    mock = _patched_readiness(monkeypatch, {
        "receipt_ok": True, "manual_review": True,
        "receipt": {"adapter": "human", "status": "your_review"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    assert mock.call_count == 1, (
        f"gate_readiness must be called exactly once, for the ONE pending "
        f"node, never for non-pending nodes; got {mock.call_count} calls")


def test_rollup_blocked_epic_waits_on_its_children_not_on_you(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    epic = _pending_root(task_svc, workflow_step="green_gate")
    _patched_readiness(monkeypatch, {
        "receipt_ok": False, "manual_review": True,
        "receipt_refusal": "6 child task(s) not done",
        "blocking_children": [{"id": f"c{i}", "title": f"child {i}"} for i in range(6)],
        "receipt": {"adapter": "epic-rollup", "status": "rollup_blocked"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    node = {n["id"]: n for n in resp.json()["nodes"]}[epic.id]
    assert node["owner_actionable"] is False, (
        f"a rollup-blocked epic must never be owner_actionable, even "
        f"though its readiness carries manual_review=true; got {node!r}")
    assert "6" in node["waiting_on"], (
        f"waiting_on must name the unfinished-children count; got {node!r}")


def test_red_gate_is_never_owner_actionable(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc, workflow_step="red_gate")
    _patched_readiness(monkeypatch, {
        "receipt_ok": False,
        "receipt_refusal": (
            "no RED receipt, and NO machine sweep is coming: the machine "
            "gate seat is off in this environment. This gate needs a "
            "distinct actor's decision."),
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200
    node = {n["id"]: n for n in resp.json()["nodes"]}[root.id]
    assert node["owner_actionable"] is False, (
        f"red_gate must never be owner_actionable, whatever the refusal "
        f"prose says about a distinct actor; got {node!r}")
    assert node["waiting_on"] == "machine seat", f"got {node!r}"


def test_human_your_review_gate_is_owner_actionable(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc, workflow_step="green_gate")
    _patched_readiness(monkeypatch, {
        "receipt_ok": True, "manual_review": True,
        "receipt": {"adapter": "human", "status": "your_review"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    node = {n["id"]: n for n in resp.json()["nodes"]}[root.id]
    assert node["owner_actionable"] is True, f"got {node!r}"
    assert node["waiting_on"] == "you", f"got {node!r}"


def test_unapproved_design_packet_on_a_root_task_is_owner_actionable(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc, workflow_step="plan_gate")
    _patched_readiness(monkeypatch, {
        "receipt_ok": False, "manual_review": True,
        "receipt_refusal": (
            "plan_gate needs an explicit owner approval of the design "
            "packet (plan_doc + plan_diagram + prototype) before it can "
            "clear - no approval is on file yet"),
        "receipt": {"adapter": "design-packet", "status": "pending"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    node = {n["id"]: n for n in resp.json()["nodes"]}[root.id]
    assert node["owner_actionable"] is True, (
        f"an unapproved design packet on a ROOT plan_gate task is the "
        f"owner's own action (D-4/FR-12/AC-15), not the driver's; got {node!r}")
    assert node["waiting_on"] == "you", f"got {node!r}"


def test_driver_owned_refusal_reads_as_driver(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc, workflow_step="green_gate")
    _patched_readiness(monkeypatch, {
        "receipt_ok": False, "manual_review": True,
        "receipt_refusal": "attach a screenshot to completion_proof",
        "receipt": {"adapter": "human", "status": "needs_visual_evidence"},
    })
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    node = {n["id"]: n for n in resp.json()["nodes"]}[root.id]
    assert node["owner_actionable"] is False, f"got {node!r}"
    assert node["waiting_on"] != "you", (
        f"needs_visual_evidence is the DRIVER's to clear, not the "
        f"owner's; got {node!r}")
    assert node["waiting_on"] != "machine seat", f"got {node!r}"


def test_readiness_failure_degrades_one_node_and_never_the_graph(tmp_path, monkeypatch):
    ctx, task_svc = _seeded_ctx(tmp_path)
    root = _pending_root(task_svc, workflow_step="story_gate")
    _patched_readiness(monkeypatch, side_effect=RuntimeError("boom"))
    client = _client(ctx, monkeypatch)
    resp = client.get("/api/work/graph?project=gamify")
    assert resp.status_code == 200, (
        "a per-node readiness failure must degrade that node's label, "
        "never 500 the whole /live boot snapshot")
    node = {n["id"]: n for n in resp.json()["nodes"]}[root.id]
    assert node["owner_actionable"] is False, f"got {node!r}"
    assert node["waiting_on"] == "", f"got {node!r}"


# ---------------------------------------------------------------------------
# Frontend wire + client state: GraphNode / makeNode / reconcile
# ---------------------------------------------------------------------------

def test_graph_node_type_mirrors_the_payload():
    src = _read(_LIVE / "types.ts")
    assert "owner_actionable" in src, (
        "GraphNode must gain owner_actionable, mirroring api/work.py's "
        "payload 1:1 per the file's own header contract")
    assert "waiting_on" in src, "GraphNode must gain waiting_on"


def test_make_node_and_reconcile_carry_the_fields():
    src = _read(_LIVE / "graphState.ts")
    assert "owner_actionable" in src and "waiting_on" in src, (
        "graphState.ts must carry the new fields onto LiveNode")
    make_node_start = src.index("private makeNode(")
    make_node_body = src[make_node_start:src.index("\n  }", make_node_start)]
    assert "owner_actionable" in make_node_body, (
        f"makeNode must assign owner_actionable on the returned LiveNode; "
        f"got body:\n{make_node_body}")
    assert "waiting_on" in make_node_body, (
        f"makeNode must assign waiting_on on the returned LiveNode; "
        f"got body:\n{make_node_body}")
    reconcile_idx = src.index("existing.gate_waiting_s")
    reconcile_window = src[reconcile_idx:reconcile_idx + 800]
    assert "existing.owner_actionable" in reconcile_window, (
        f"reconcile must refresh existing.owner_actionable beside "
        f"existing.gate_waiting_s; got window:\n{reconcile_window}")
    assert "existing.waiting_on" in reconcile_window, (
        f"reconcile must refresh existing.waiting_on beside "
        f"existing.gate_waiting_s; got window:\n{reconcile_window}")


# ---------------------------------------------------------------------------
# gatepanel.ts: two sections, dim non-actionable, no click affordance
# ---------------------------------------------------------------------------

def test_gate_panel_splits_owner_review_from_waiting_on_others():
    src = _read(_LIVE / "gatepanel.ts")
    fn_start = src.index("export function drawGatePanel(")
    body = src[fn_start:]
    assert re.search(r"\.filter\([^\n]*owner_actionable", body), (
        "drawGatePanel must partition the pending list on n.owner_actionable "
        f"into an actionable subset; got body head:\n{body[:1200]}")
    assert "YOUR REVIEW" in body, (
        "the owner section heading must read YOUR REVIEW (D-7)")
    assert "waiting on others" in body, (
        "the non-actionable section heading must read 'waiting on "
        "others' (D-7)")


def test_waiting_on_others_is_dim_and_uncapped():
    src = _read(_LIVE / "gatepanel.ts")
    fn_start = src.index("export function drawGatePanel(")
    body = src[fn_start:]
    others_idx = body.index("waiting on others")
    others_section = body[others_idx:]
    assert "PALETTE.textDim" in others_section, (
        "the 'waiting on others' rows must render in PALETTE.textDim, "
        f"never the owner section's magenta; got:\n{others_section[:600]}")
    window = others_section[:600]
    assert not re.search(r"\.slice\(0,\s*\d+\)", window), (
        "the non-actionable partition must be drawn in FULL, never "
        f"sliced or capped; got:\n{window}")


def test_non_actionable_row_shows_its_waiting_on_label():
    src = _read(_LIVE / "gatepanel.ts")
    assert "n.waiting_on" in src, (
        "a non-actionable row's fillText must render n.waiting_on "
        "instead of the bare workflow_step alarm line")


def test_panel_adds_no_click_region():
    src = _read(_LIVE / "gatepanel.ts")
    assert "export function drawGatePanel(" in src, (
        "drawGatePanel's exported signature must be unchanged (owned by "
        "task d56f3b25's click-affordance slice, out of scope here)")
    assert "addEventListener" not in src
    assert "onclick" not in src.lower()
    assert "hitTest" not in src and "hit_test" not in src

    draw_src = _read(_LIVE / "draw.ts")
    assert "drawGatePanel(ctx, state.nodes, now, width, height);" in draw_src, (
        "draw.ts:297's call site must stay unchanged (d56f3b25's boundary)")
