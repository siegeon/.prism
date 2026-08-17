"""A task rendered as a live conductor drive must have actually been CLAIMED
by conductor_work (task 2dfa94bd).

BUG: ConductorService.managed_tasks() (conductor_service.py:4063-4072)
synthesizes workflow_step="intake" for ANY in_progress task with an empty
workflow_step and gate_state="none" — status flips only, no distinction
from a task conductor_work has genuinely started (which calls
task_workspace.ensure_workspace() at its very first PEEK, per
conductor_work's own docstring: "Call with NO outcome to START/PEEK ...
creates the task's git worktree"). Because of that synthesis, a task
someone flipped in_progress and linked a session to — but never drove
through conductor_work — renders the FULL SDLC-drive chrome on the real
/conductor tab: the "conductor task · SDLC drive" header (ConductorPage.tsx:214),
the 10-step <LabeledTimeline/> (line 221), the "driver active" pill sourced
from activity.state (lines 182-187), and a fabricated Handoff line
(lines 197-200, falls to "queued — awaiting first turn").

task_workspace.workspace_record(task_id) (task_workspace.py:405-415) is a
REAL, already-existing, side-effect-free "has conductor_work ever touched
this task" bit: it is None until ensure_workspace() has run for that task,
and stays a real dict thereafter (including while the worker is between
claims — no recency check, so a released-but-claimed task keeps rendering
chrome honestly per the likely_misfire).

FIX CONTRACT pinned by this suite (implementation lives in api/conductor.py,
NOT the policy file conductor_service.py, per this task's stop_if):
  - GET /api/conductor/state's managed_tasks rows carry a new additive
    "claimed" boolean: True iff task_workspace.workspace_record(task_id) is
    not None. api/conductor.py must reference it as `task_workspace.workspace_record`
    (module-attribute lookup, e.g. `from prism_service.services import
    task_workspace`) — NOT a `from ... import workspace_record` rebinding —
    so this suite's monkeypatch on the module attribute is observed.
  - ConductorPage.tsx's ManagedTask type carries `claimed?: boolean` and
    TaskTile gates the SDLC-drive chrome (the "conductor task · SDLC drive"
    header and the <LabeledTimeline/>) on it, with an honest alternate
    render when claimed is false — never a silent vanish (stop_if #2: do
    not hide active work to hide phantom drives; and the ring/timeline/
    handoff must NOT disappear for genuinely-claimed-but-idle tasks, since
    `claimed` does not encode recency).

ALL of these FAIL against current source: the API never emits "claimed",
and ConductorPage.tsx never reads task.claimed anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_CONDUCTOR_TSX = _SRC / "pages" / "ConductorPage.tsx"
# Task 40c29b83 (FR-1): ManagedTask's declaration moved out of ConductorPage
# into the shared hook both LiveBar and ConductorPage now consume.
_CONDUCTOR_STATE_HOOK = _SRC / "lib" / "useConductorState.ts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ===========================================================================
# BACKEND — GET /api/conductor/state must carry an honest per-task "claimed"
# bit, additive to the existing managed_tasks shape.
# ===========================================================================


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import conductor as conductor_api
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False, task_svc=task_svc)

    class _Ctx:
        conductor_svc = cond

    monkeypatch.setattr(conductor_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    return TestClient(app), task_svc, cond


def _flipped_raw(task_svc, title="undriven task"):
    """Reproduces task 2dfa94bd's repro exactly: create, flip in_progress,
    workflow_step/gate_state left untouched (empty/"none") — status flips
    only, conductor_work NEVER called for it."""
    t = task_svc.create(title=title)
    task_svc.update(t.id, status="in_progress")
    return t


def _genuinely_claimed(task_svc, title="driven task"):
    """A task conductor_work has actually advanced past intake — a real
    non-empty workflow_step, exactly what a genuine drive produces."""
    t = task_svc.create(title=title)
    task_svc.update(t.id, status="in_progress", workflow_step="write_failing_tests")
    return t


def test_unclaimed_inprogress_task_reports_claimed_false(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    from prism_service.services import task_workspace
    # No workspace record exists anywhere for anyone — the honest baseline.
    monkeypatch.setattr(task_workspace, "workspace_record", lambda task_id: None)

    t = _flipped_raw(task_svc)

    resp = client.get("/api/conductor/state", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    rows = {r["id"]: r for r in resp.json()["managed_tasks"]}
    assert t.id in rows, "the synthesized-intake row must still be present (visibility, not deletion)"
    row = rows[t.id]
    assert "claimed" in row, row.keys()
    assert row["claimed"] is False, (
        "a task nobody ran conductor_work on (no workspace record) must "
        f"report claimed:False, got {row!r}"
    )


def test_genuinely_claimed_task_reports_claimed_true(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    from prism_service.services import task_workspace

    t = _genuinely_claimed(task_svc)
    # Simulate the real effect of conductor_work's first PEEK: ensure_workspace
    # ran and left a record for THIS task id (and only this one).
    monkeypatch.setattr(
        task_workspace, "workspace_record",
        lambda task_id: {"path": "/fake/ws", "branch": "prism/ws/x"} if task_id == t.id else None,
    )

    resp = client.get("/api/conductor/state", params={"project": "prism"})
    assert resp.status_code == 200, resp.text
    row = {r["id"]: r for r in resp.json()["managed_tasks"]}[t.id]
    assert row["claimed"] is True, row


def test_released_worker_between_claims_stays_claimed_true(tmp_path, monkeypatch):
    """likely_misfire guard: a real claim (workspace_record exists) must
    NOT be revoked just because the task looks idle right now — workspace_
    record carries no recency, and this suite must not tempt an
    implementation into layering one on. A released-but-claimed task keeps
    claimed:True."""
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    from prism_service.services import task_workspace

    t = _genuinely_claimed(task_svc, title="idle-but-claimed task")

    monkeypatch.setattr(
        task_workspace, "workspace_record",
        lambda task_id: {"path": "/fake/ws", "branch": "prism/ws/y"} if task_id == t.id else None,
    )

    resp = client.get("/api/conductor/state", params={"project": "prism"})
    row = {r["id"]: r for r in resp.json()["managed_tasks"]}[t.id]
    assert row["claimed"] is True, (
        "a genuinely-claimed task must still report claimed:True while "
        f"between claims (no active driver right now), got {row!r}"
    )


def test_claimed_is_additive_existing_keys_unchanged(tmp_path, monkeypatch):
    client, task_svc, cond = _client(tmp_path, monkeypatch)
    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_record", lambda task_id: None)

    _flipped_raw(task_svc)
    resp = client.get("/api/conductor/state", params={"project": "prism"})
    body = resp.json()
    assert "managed_tasks" in body and "step_buckets" in body, body.keys()
    row = body["managed_tasks"][0]
    for key in ("id", "title", "status", "workflow_step", "gate_state", "activity"):
        assert key in row, f"existing managed_tasks key {key!r} must be unchanged, got {row.keys()}"


# ===========================================================================
# FRONTEND — the real /conductor tab (ConductorPage.tsx TaskTile) must gate
# the SDLC-drive chrome on task.claimed, with an honest alternate render for
# claimed:false (no silent vanish per stop_if).
# ===========================================================================


def test_managed_task_type_carries_claimed_field():
    # SUPERSEDED location (task 40c29b83, FR-1): ManagedTask no longer lives
    # as a private type in ConductorPage.tsx -- it moved to the shared
    # useConductorState hook that LiveBar.tsx now also consumes, so the real
    # invariant (TaskTile can read task.claimed) is pinned against ITS type
    # declaration rather than a copy ConductorPage no longer owns.
    src = _read(_CONDUCTOR_STATE_HOOK)
    type_block = src[src.index("type ManagedTask"): src.index("type ManagedTask") + 900]
    assert "claimed" in type_block, (
        "ManagedTask (lib/useConductorState.ts) must declare a `claimed` "
        "field so TaskTile can read task.claimed — not present today"
    )


def test_tile_header_gated_on_claimed():
    src = _read(_CONDUCTOR_TSX)
    tile = src[src.index("function TaskTile"): src.index("function TileHero")]
    assert "task.claimed" in tile, (
        "TaskTile must reference task.claimed somewhere in its render logic"
    )
    idx = tile.index('conductor task · SDLC drive')
    window = tile[max(0, idx - 500): idx]
    assert "claimed" in window, (
        "the 'conductor task · SDLC drive' header must sit inside a branch "
        "conditioned on claimed (an unclaimed task must not wear this "
        f"header) — no 'claimed' reference found in the 500 chars before "
        f"it:\n{window!r}"
    )


def test_labeled_timeline_gated_on_claimed():
    src = _read(_CONDUCTOR_TSX)
    tile = src[src.index("function TaskTile"): src.index("function TileHero")]
    idx = tile.index("<LabeledTimeline")
    window = tile[max(0, idx - 500): idx]
    assert "claimed" in window, (
        "the 10-step <LabeledTimeline/> must sit inside a branch conditioned "
        f"on claimed — no 'claimed' reference found in the 500 chars before "
        f"it:\n{window!r}"
    )


def test_unclaimed_tile_has_honest_alternate_render():
    """stop_if: never a silent vanish. An unclaimed row must still render
    SOMETHING legible and honest (e.g. 'not yet claimed by conductor'),
    not just an empty/absent tile."""
    src = _read(_CONDUCTOR_TSX)
    tile = src[src.index("function TaskTile"): src.index("function TileHero")]
    lowered = tile.lower()
    assert ("not claimed" in lowered or "not yet claimed" in lowered
            or "never claimed" in lowered or "no conductor drive" in lowered), (
        "an unclaimed task must render an honest label saying it was never "
        "driven through conductor_work, not just hide the chrome silently"
    )


def test_driver_active_pill_not_fabricated_for_unclaimed():
    """The activity-derived 'driver active'/'no active driver' pill and the
    Handoff strip's fabricated 'ready → <worker> on deck' / 'queued —
    awaiting first turn' lines must not be presented as real drive state for
    an unclaimed task."""
    src = _read(_CONDUCTOR_TSX)
    tile = src[src.index("function TaskTile"): src.index("function TileHero")]
    handoff_idx = tile.index('"paused at gate')
    window = tile[max(0, handoff_idx - 600): handoff_idx]
    assert "claimed" in window, (
        "the Handoff strip's fabricated status lines must be reachable only "
        "when claimed — no 'claimed' reference found in the 600 chars "
        f"before the handoff ternary:\n{window!r}"
    )


def test_prism_version_patch_bumped_from_7_10_44():
    from prism_service.__version__ import PRISM_VERSION
    parts = tuple(int(x) for x in PRISM_VERSION.split("."))
    assert parts >= (7, 10, 45), (
        f"PRISM_VERSION must be patch-bumped past 7.10.44 for this "
        f"user-visible /conductor fix; got {PRISM_VERSION}"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
