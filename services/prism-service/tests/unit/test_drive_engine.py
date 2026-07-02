"""RED scaffold — deterministic drive engine over the planning half
(task a7d96437, C1 of the PI-orchestration build, parent 81b23574 FR-1).

Pins: a server-side state machine walks review_previous_notes ->
draft_story -> story_gate -> verify_plan -> plan_gate via IN-PROCESS
ConductorService calls; zero model round-trips on mechanical steps and
one slot per authoring step; desync responses (ok:false "gate pending"
/ "already past") never halt the drive; NO override code path exists;
a failed rubric gate stops the drive with the scorer's reason.

Real TaskService + ConductorService on tmp dbs; stub verifier; seeded
tmp principle store; stub models only — no inference, no daemon.

FAILS today: prism_service.services.drive_engine does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _engine_mod():
    from prism_service.services import drive_engine
    return drive_engine


class StubVerifier:
    """Always-pass shell verifier: the RUBRIC (not this stub) must decide
    story/plan gates — the thin-story test proves the rubric verdict wins."""

    def run(self, **kwargs: object) -> dict:
        return {"status": "pass", "tier0": "pass", "tier1": "not-run",
                "tier2": "skipped", "summary": "stub pass"}


class StubModel:
    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        return '{"value": "Stubbed slot text for the drive engine walk"}'


class GateSpy:
    """Duck-typed conductor proxy recording every gate_decide kwargs —
    the proof no call ever carried override=True."""

    def __init__(self, inner):
        self.inner = inner
        self.gate_calls: list[dict] = []

    def advance_task(self, *a, **k):
        return self.inner.advance_task(*a, **k)

    def gate_decide(self, *a, **k):
        self.gate_calls.append(dict(k))
        return self.inner.gate_decide(*a, **k)


class DesyncConductor(GateSpy):
    """Injects canned advance refusals ahead of the real conductor."""

    def __init__(self, inner, canned):
        super().__init__(inner)
        self.canned = list(canned)

    def advance_task(self, *a, **k):
        if self.canned:
            return self.canned.pop(0)
        return self.inner.advance_task(*a, **k)


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.memory_service import MemoryService
    from prism_service.services import arc_governance as gov
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False,
        task_svc=task_svc, verifier_svc=StubVerifier())
    mem = MemoryService(str(tmp_path / "mulch"))
    gov.seed_default_principles(mem)
    cond.attach_memory_service(mem, "unit")
    return task_svc, cond


GOOD_DOC = """## Summary

A drive-engine walk fixture story with enough body to satisfy the rubric.

## Requirements

FR-1: the fixture story satisfies the story rubric

## Acceptance Criteria

- AC-1: the walk reaches plan_gate - oracle: pytest tests/unit/test_drive_engine.py
- AC-2: gates pass on rubric merit - oracle: pytest tests/unit/test_drive_engine.py
"""

GOOD_DIAGRAM = ("flowchart TD\n  flow_a[\"start\"]\n  flow_b[\"finish\"]\n"
                "  flow_a --> flow_b\n")


def _slot_story_author(model):
    """Injected authoring seam: exactly ONE pi_slots fill per call."""
    from prism_service.inference import pi_slots

    def author(task, ctx):
        line = pi_slots.fill_slot("summary", ctx, model=model).value
        return GOOD_DOC.replace(
            "A drive-engine walk fixture story", line[:60] or "story"), 1
    return author


def _slot_diagram_author(model):
    from prism_service.inference import pi_slots

    def author(task, ctx):
        pi_slots.fill_slot("title", ctx, model=model)
        return GOOD_DIAGRAM, 1
    return author


# ── AC-1: full planning walk, model-calls == authoring-steps, 0 overrides ──

def test_plan_walks_to_plan_gate_passed(tmp_path):
    de = _engine_mod()
    task_svc, cond = _services(tmp_path)
    spy = GateSpy(cond)
    t = task_svc.create(title="drive engine walk fixture")
    stub = StubModel()
    eng = de.DriveEngine(spy, task_svc,
                         story_author=_slot_story_author(stub),
                         diagram_author=_slot_diagram_author(stub))
    res = eng.plan(t.id, session_id="drive-engine-test")
    assert res["ok"] is True, res
    assert res["final_step"] == "plan_gate"
    assert res["gate_state"] == "passed"
    assert res["stats"]["authoring_steps"] == 2
    assert res["stats"]["model_calls"] == 2, res["stats"]
    assert res["stats"]["overrides"] == 0
    assert stub.calls == 2, "one slot per authoring step, none elsewhere"
    assert all(not k.get("override") for k in spy.gate_calls), spy.gate_calls


# ── AC-2: default authors (plan_scaffold) also clear both gates ────────

def test_default_authors_reach_plan_gate(tmp_path):
    de = _engine_mod()
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="default author drive fixture")
    eng = de.DriveEngine(cond, task_svc, model=StubModel())
    res = eng.plan(t.id)
    assert res["ok"] is True, res
    assert res["gate_state"] == "passed"
    assert res["stats"]["model_calls"] <= 2, res["stats"]
    walked = task_svc.get(t.id)
    assert "## Acceptance Criteria" in walked.plan_doc
    assert walked.plan_diagram.strip().startswith("flowchart")


# ── AC-3/AC-4: desync responses never halt the drive ───────────────────

def test_desync_gate_pending_folds_into_gate_handler(tmp_path):
    de = _engine_mod()
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="desync gate pending fixture")
    stub = StubModel()
    desync = DesyncConductor(cond, [
        {"ok": False, "task_id": t.id, "from_step": "story_gate",
         "to_step": "story_gate", "gate_state": "pending",
         "reason": "gate 'story_gate' is pending; call gate_decide "
                   "before advancing"},
    ])
    eng = de.DriveEngine(desync, task_svc,
                         story_author=_slot_story_author(stub),
                         diagram_author=_slot_diagram_author(stub))
    res = eng.plan(t.id)
    assert res["ok"] is True, res
    assert res["gate_state"] == "passed"


def test_desync_already_past_continues(tmp_path):
    de = _engine_mod()
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="desync already past fixture")
    stub = StubModel()
    desync = DesyncConductor(cond, [
        {"ok": False, "task_id": t.id, "reason":
         "task is already past step review_previous_notes"},
    ])
    eng = de.DriveEngine(desync, task_svc,
                         story_author=_slot_story_author(stub),
                         diagram_author=_slot_diagram_author(stub))
    res = eng.plan(t.id)
    assert res["ok"] is True, res
    assert res["gate_state"] == "passed"


# ── AC-5: rubric failure stops the drive; no override ever ─────────────

def test_thin_story_stops_with_reason_never_overrides(tmp_path):
    de = _engine_mod()
    task_svc, cond = _services(tmp_path)
    spy = GateSpy(cond)
    t = task_svc.create(title="thin story fixture")
    eng = de.DriveEngine(
        spy, task_svc,
        story_author=lambda task, ctx: ("free prose, no sections", 0),
        diagram_author=lambda task, ctx: (GOOD_DIAGRAM, 0))
    res = eng.plan(t.id)
    assert res["ok"] is False, res
    assert "story_complete" in str(res.get("reason", "")), res
    walked = task_svc.get(t.id)
    assert walked.workflow_step == "story_gate"
    assert walked.gate_state == "failed"
    assert res["stats"]["overrides"] == 0
    assert all(not k.get("override") for k in spy.gate_calls), spy.gate_calls


# ── AC-6: iteration guard names the stuck step ─────────────────────────

def test_iteration_guard_stops_stuck_drive(tmp_path):
    de = _engine_mod()
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="stuck conductor fixture")

    class StuckConductor(GateSpy):
        def advance_task(self, *a, **k):
            return {"ok": True, "task_id": t.id,
                    "from_step": "review_previous_notes",
                    "to_step": "review_previous_notes",
                    "gate_state": "none"}

    eng = de.DriveEngine(
        StuckConductor(cond), task_svc,
        story_author=lambda task, ctx: (GOOD_DOC, 0),
        diagram_author=lambda task, ctx: (GOOD_DIAGRAM, 0))
    res = eng.plan(t.id)
    assert res["ok"] is False, res
    assert "review_previous_notes" in str(res.get("reason", "")), res
