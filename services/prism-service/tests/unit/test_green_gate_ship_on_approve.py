"""Owner Approve at green_gate TRIGGERS the ship — task 5b6aefc1.

This is the task's own pinned oracle: "the owner clicks Approve once with
visual evidence; without any further human action the branch is pushed, a PR
opens, CI passes, the merge lands, the unshipped tooth re-check passes, and
the task completes."

Two invariants this suite exists to defend, both named in the task's
`likely_misfire`:

  AC-1  the human decision is RECORDED FIRST. "Shipping BEFORE recording the
        human decision (a ship failure then eats the approval)" is the
        pre-declared misfire; the record must land before any subprocess.
  AC-4  the bot LANDS CODE, it never DECIDES A GATE. The gate that finally
        passes is credited to the owner's own actor identity, so the
        distinct-actor rule is preserved and `conductor-shipper` never
        becomes an approver of its own work.

Plus the two anti-widenings:
  - default OFF leaves today's refusal byte-identical (no silent behavior
    change for environments that never opted in);
  - a proof_type=test task, which the machine adjudicator already owns, is
    NOT swept into the ship path.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import control_plane as cp  # noqa: E402

sys.path.insert(0, str(_HERE.parent))
from test_ship_worker import (  # noqa: E402
    FakeGh, _git, _shipped, _unshipped_workspace, _write,
)

OWNER = "siegeon@gmail.com"
DEMO_PROOF = "pytest tests/unit/test_ship_worker.py -q -> 12 passed"


def _human_actor(raw):
    from prism_service.models.actor import Actor, ActorKind
    return Actor(id=f"user:{OWNER}", kind=ActorKind.HUMAN,
                 display_name=OWNER, user_id=OWNER)


def _clean_daemon_repo(tmp: Path) -> Path:
    repo = tmp / "daemon"
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q")
    _write(repo, "README.md", "# ok\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    return repo


def _setup(tmp_path, monkeypatch, *, proof_type="demo"):
    from prism_service.services import conductor_service as cs
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    t = task_svc.create(
        title="ship on approve", tags=["conductor"], proof_type=proof_type,
        oracle="Approve once; the branch lands with no further human action")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    completion_proof=DEMO_PROOF)

    origin, work, branch = _unshipped_workspace(tmp_path, t.id)

    daemon_root = tmp_path / "_daemon"
    daemon_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(cp, "_prism_repo_root",
                        lambda: _clean_daemon_repo(daemon_root))
    import prism_service.services.task_workspace as tw
    rec = {"task_id": t.id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(cs, "_resolve_actor_identity", _human_actor)
    return task_svc, cond, t, origin, work, branch


def _attr(row, name: str) -> str:
    """TaskService.history() yields TaskHistory objects; tolerate dict rows
    too so this suite does not care which the store returns."""
    if isinstance(row, dict):
        return str(row.get(name) or "")
    return str(getattr(row, name, "") or "")


def _ship_rows(task_svc, task_id) -> list[str]:
    return [_attr(r, "details") for r in task_svc.history(task_id)
            if _attr(r, "action") == "gate_decide"]


# ---------------------------------------------------------------------------
# THE pinned oracle — one click, no further human action, the branch lands
# ---------------------------------------------------------------------------


def test_approve_triggers_ship_bot(tmp_path, monkeypatch):
    from prism_service.services import ship_worker

    monkeypatch.setenv("PRISM_SHIP_ON_APPROVE", "1")
    task_svc, cond, t, origin, work, branch = _setup(tmp_path, monkeypatch)
    assert not _shipped(work, t.id), "fixture must start UNshipped"

    # ---- the owner's ONE click -------------------------------------------
    res = cond.gate_decide(t.id, "approve", reason="looks right — ship it",
                           actor=OWNER, session_id="owner-session")

    # AC-1: the decision is RECORDED, immediately, before anything ships.
    rows = _ship_rows(task_svc, t.id)
    assert any("ship=queued" in r for r in rows), (
        "the owner's approve must be recorded the instant it is made — a "
        f"later ship failure must not be able to eat it. rows={rows}")
    assert any(OWNER in r for r in rows), rows
    assert not _shipped(work, t.id), (
        "gate_decide must NOT ship inline — the pipeline is the worker's job "
        "so a CI wait never blocks the owner's HTTP request")
    assert res.get("gate_state") == "pending", res
    assert "shipping" in str(res.get("reason", "")).lower(), (
        "the card must say the ship is under way, not tell the owner to go "
        f"land the branch by hand. got: {res.get('reason')!r}")

    # ---- no further human action: the seat does the rest ------------------
    gh = FakeGh(origin, branch)
    out = ship_worker.ship_task(t.id, runner=gh, poll_interval_s=0,
                                task_svc=task_svc, cond=cond)
    assert out["ok"] is True, out
    assert _shipped(work, t.id), "the branch must be landed on origin/main"

    # AC-4: the gate passes, credited to the HUMAN — never to the bot.
    live = task_svc.get(t.id)
    assert str(live.gate_state) == "passed", (
        f"gate_state={live.gate_state!r} reason={live.gate_reason!r}")

    passing = [r for r in task_svc.history(t.id)
               if _attr(r, "action") == "gate_decide"
               and "ship=queued" not in _attr(r, "details")
               and "refused" not in _attr(r, "details")]
    assert passing, "no passing gate_decide row was recorded"
    assert not any(_attr(r, "actor") == ship_worker.SEAT_ID
                   for r in passing), (
        "the ship bot must never be the APPROVER of the gate — it lands "
        "code; the owner's preserved decision is what passes the gate")


# ---------------------------------------------------------------------------
# anti-widening 1 — default OFF is byte-identical to today
# ---------------------------------------------------------------------------


def test_default_off_keeps_todays_refusal_verbatim(tmp_path, monkeypatch):
    monkeypatch.delenv("PRISM_SHIP_ON_APPROVE", raising=False)
    task_svc, cond, t, origin, work, branch = _setup(tmp_path, monkeypatch)

    res = cond.gate_decide(t.id, "approve", reason="ship it", actor=OWNER,
                           session_id="owner-session")

    assert res["ok"] is False, res
    assert res["gate_state"] == "pending", res
    assert "is not yet reachable from origin/main" in str(res["reason"]), res
    assert "DONE means SHIPPED" in str(res["reason"]), res
    rows = _ship_rows(task_svc, t.id)
    assert any("shipped-ness=not-yet" in r for r in rows), rows
    assert not any("ship=queued" in r for r in rows), (
        "with the env unset nothing may be queued for shipping")


# ---------------------------------------------------------------------------
# anti-widening 2 — the machine-adjudicated lane is not swept in
# ---------------------------------------------------------------------------


def test_proof_type_test_is_not_swept_into_the_ship_path(
        tmp_path, monkeypatch):
    """`likely_misfire` names "running the ship bot for proof_type=test tasks
    where the machine adjudicator already owns green" — the trigger asserts
    the demo/review branch explicitly so a future refactor cannot widen it."""
    monkeypatch.setenv("PRISM_SHIP_ON_APPROVE", "1")
    task_svc, cond, t, origin, work, branch = _setup(
        tmp_path, monkeypatch, proof_type="test")

    cond.gate_decide(t.id, "approve", reason="ship it", actor=OWNER,
                     session_id="owner-session")

    rows = _ship_rows(task_svc, t.id)
    assert not any("ship=queued" in r for r in rows), (
        "a machine-graded (proof_type=test) task must keep its existing "
        f"adjudication path, not enter the ship queue. rows={rows}")
