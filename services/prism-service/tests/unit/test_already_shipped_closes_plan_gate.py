"""An already-shipped ticket is closed at plan_gate by the machine seat
(task a65c66e5, 2026-09-14).

Live: the ticket's fix had been landed by hand (f547a892 on origin/main
carries its `[task:` trailer), so its pinned suite was green at base and
no AC could be red. The machine still spent two form rewinds, a red-at-
base rewind, the whole rewind budget and an hour of the single engine slot,
then parked it for a person. Nothing asked whether the work was on main.

Pins:
- AC-1  plan_gate_checks.already_shipped reports a finding only when the
        trailer is on origin/main AND the pinned suite is green at base.
- AC-2  run_check("already_shipped") is a closer: ok=True, close=True with
        the finding; ok=True, close=False without it.
- AC-3  gate_adjudicator.close_if_already_shipped concludes the task in one
        write (green_gate / passed / done) with a gate_decide row naming
        the seat, and returns None when there is nothing to close.
- AC-4  plan-gate-check.json runs `already_shipped` as its first step.
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from prism_service.services import gate_adjudicator as ga
from prism_service.services import plan_gate_checks as pgc

_NODE = (Path(__file__).resolve().parents[4]
         / ".prism" / "behaviors" / "conductor" / "plan-gate-check.json")
_TID = "a65c66e5-b8a7-44b4-a223-f1342cfaaa14"
_PINNED = "services/prism-service/tests/unit/test_plan_subject_tooth_ignores_commit_shas.py"


def _task(verify=(_PINNED,)):
    return SimpleNamespace(id=_TID, verify=list(verify), plan_doc="- AC-1: x\n  - oracle: y")


def _wire(monkeypatch, sha="f547a892abc", rc=0, tmp_path=None):
    import prism_service.api.tasks as _tasks
    import prism_service.services.task_workspace as _tw
    monkeypatch.setattr(_tasks, "_shipped_sha_on_main", lambda repo, tid: sha)
    monkeypatch.setattr(_tw, "_prism_repo_root", lambda: tmp_path or Path("/tmp"))
    monkeypatch.setattr(pgc, "repo_root_for", lambda task, project: Path("/tmp"))
    monkeypatch.setattr(pgc, "base_ref_for", lambda task, root: "0123456789ab")
    monkeypatch.setattr(pgc, "measurement_enabled", lambda: True)
    return lambda root, rev, targets, **kw: rc


# AC-1 ------------------------------------------------------------------
def test_a_trailer_on_main_and_a_green_pinned_suite_is_a_finding(monkeypatch):
    runner = _wire(monkeypatch)
    finding = pgc.already_shipped(_task(), "prism", runner=runner)
    assert finding.startswith("already shipped: f547a892 on origin/main")
    assert "passes on origin/main" in finding and _PINNED in finding


def test_no_trailer_or_a_red_suite_or_no_pins_is_no_finding(monkeypatch):
    runner = _wire(monkeypatch, sha="")
    assert pgc.already_shipped(_task(), "prism", runner=runner) == ""
    runner = _wire(monkeypatch, rc=1)
    assert pgc.already_shipped(_task(), "prism", runner=runner) == ""
    runner = _wire(monkeypatch)
    assert pgc.already_shipped(_task(verify=()), "prism", runner=runner) == ""


def test_an_unmeasurable_run_never_closes(monkeypatch):
    runner = _wire(monkeypatch, rc=None)
    assert pgc.already_shipped(_task(), "prism", runner=runner) == ""


# AC-2 ------------------------------------------------------------------
def test_run_check_reports_the_closer_shape(monkeypatch):
    runner = _wire(monkeypatch)
    out = pgc.run_check("already_shipped", _task(), "prism", runner=runner)
    assert out["ok"] is True and out["close"] is True
    assert out["reason"].startswith("already shipped")
    runner = _wire(monkeypatch, sha="")
    out = pgc.run_check("already_shipped", _task(), "prism", runner=runner)
    assert out["ok"] is True and out["close"] is False and out["reason"] == ""
    assert "already_shipped" not in pgc.CHECKS, "a closer is never a refusal"


# AC-3 ------------------------------------------------------------------
class _Svc:
    def __init__(self):
        self.updates: list[dict] = []
        self.history: list[dict] = []

    def update(self, task_id, **fields):
        self.updates.append({"task_id": task_id, **fields})

    def record_history(self, task_id, action, details="", actor=""):
        self.history.append({"task_id": task_id, "action": action,
                             "details": details, "actor": actor})


def test_the_adjudicator_closes_the_task_in_one_write(monkeypatch):
    monkeypatch.setattr(pgc, "already_shipped",
                        lambda task, project, **kw: "already shipped: f547a892 on origin/main ...")
    svc = _Svc()
    ctx = SimpleNamespace(task_svc=svc)
    res = ga.close_if_already_shipped(ctx, _TID, "prism", _task())
    assert res and res["ok"] is True and res["closed"] is True
    assert svc.updates == [{"task_id": _TID, "workflow_step": "green_gate",
                            "gate_state": "passed", "status": "done",
                            "blocked_reason": "",
                            "gate_reason": "already shipped: f547a892 on origin/main ..."}]
    assert svc.history[0]["action"] == "gate_decide"
    assert svc.history[0]["actor"] == "conductor-adjudicator"
    assert "already shipped" in svc.history[0]["details"]


def test_the_adjudicator_leaves_an_unshipped_task_alone(monkeypatch):
    monkeypatch.setattr(pgc, "already_shipped", lambda task, project, **kw: "")
    svc = _Svc()
    assert ga.close_if_already_shipped(SimpleNamespace(task_svc=svc), _TID, "prism", _task()) is None
    assert svc.updates == [] and svc.history == []
    assert ga.close_if_already_shipped(SimpleNamespace(task_svc=svc), _TID, "prism", None) is None


# AC-5 (round 2, task 83dcd479) -- a shipped-direct tag counts as shipped
def test_a_shipped_direct_tag_on_main_is_a_finding_without_a_trailer(monkeypatch):
    runner = _wire(monkeypatch, sha="")
    import prism_service.api.tasks as _tasks

    def _git(repo, *args):
        if args[0] == "rev-parse":
            return 0, "572c3f75aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        if args[0] == "merge-base":
            return 0, ""
        return 1, ""
    monkeypatch.setattr(_tasks, "_git", _git)
    task = _task()
    task.tags = ["ci", "shipped-direct-572c3f75"]
    finding = pgc.already_shipped(task, "prism", runner=runner)
    assert finding.startswith("already shipped: 572c3f75 on origin/main is the commit its shipped-direct tag names")


def test_a_shipped_direct_tag_not_on_main_is_no_finding(monkeypatch):
    runner = _wire(monkeypatch, sha="")
    import prism_service.api.tasks as _tasks
    monkeypatch.setattr(_tasks, "_git", lambda repo, *a: (0, "abc\n") if a[0] == "rev-parse" else (1, ""))
    task = _task()
    task.tags = ["shipped-direct-abc1234"]
    assert pgc.already_shipped(task, "prism", runner=runner) == ""


# AC-6 (round 2, task 6bc3e6c2) -- the rewind reader hears the coverage rubric
def test_the_plan_gate_rewind_reader_falls_back_to_the_rubric_refusal(monkeypatch):
    from prism_service.services import plan_rewind
    monkeypatch.setattr(pgc, "refusal", lambda task, project: "")
    reason = "plan_coverage: story carries no AC-<n> ids to diff coverage against"
    conductor = SimpleNamespace(
        _validation_for_gate=lambda step: {"rubric": "plan_coverage"},
        _verify_rubric_gate=lambda task, validation: {"verified": False, "reason": reason})
    ctx = SimpleNamespace(conductor_svc=conductor)
    assert plan_rewind._refusal_for(ctx, _task(), "plan_gate", "prism") == reason
    conductor._verify_rubric_gate = lambda task, validation: {"verified": True}
    assert plan_rewind._refusal_for(ctx, _task(), "plan_gate", "prism") == ""
    monkeypatch.setattr(pgc, "refusal", lambda task, project: "plan_checks: tooth first")
    assert plan_rewind._refusal_for(ctx, _task(), "plan_gate", "prism") == "plan_checks: tooth first"


# AC-4 ------------------------------------------------------------------
def test_the_plan_gate_node_asks_the_closer_first():
    node = json.loads(_NODE.read_text(encoding="utf-8"))
    first = node["steps"][0]
    assert first["id"] == "already_shipped"
    assert "plan-gate-check-one" in first["url"]
    assert '"check": "already_shipped"' in first["body"]
