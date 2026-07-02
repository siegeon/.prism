"""RED scaffold — deterministic safe epic fan-out (task f6328e9e, C7 of
the PI-orchestration build, parent 81b23574 FR-6 / AC-6).

Pins: ``services/drive_fanout.py`` partitions an epic's children into
waves by allowed_files overlap — pairwise-DISJOINT children share a wave
and drive CONCURRENTLY through the injected engine (threads, bounded by
``max_parallel``); OVERLAPPING children are never in the same wave and
never observed running concurrently (the collision boundary). Each child
is gated on its OWN proof_type by its own engine walk; the module exposes
NO override surface and never passes override to any seam. One child's
failure (ok:false OR a raise) is isolated — siblings still run. After all
waves the epic ROLL-UP verdict comes from the existing pure
``conductor_service.epic_rollup_verdict`` over the children's fresh rows.

Real TaskService on a tmp db; instrumented ENGINE STUB (the drive-engine
seam: ``plan(task_id, session_id) -> {ok, final_step, gate_state,
stats}``) with a lock-guarded running counter proving genuine overlap —
no inference, no daemon, no sleeps beyond the overlap window.

FAILS today: prism_service.services.drive_fanout does not exist.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _fanout_mod():
    from prism_service.services import drive_fanout
    return drive_fanout


def _task_svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


STRONG_PROOF = ("pytest tests/unit/test_drive_fanout.py green — 5 passed; "
                "gate receipt recorded by the child drive")


class InstrumentedEngine:
    """Drive-engine seam stub with a concurrency meter.

    Simulates ``DriveEngine.plan``: holds the child in-flight for
    ``delay`` seconds (the overlap window), records the PEAK number of
    simultaneously running drives (lock-guarded), logs every call's
    kwargs (the no-override proof), gates the child on ITS OWN declared
    proof_type, and marks it done with a strong completion_proof so the
    roll-up has real rows to read.
    """

    def __init__(self, task_svc, *, delay=0.05, fail_ids=(), raise_ids=()):
        self.task_svc = task_svc
        self.delay = delay
        self.fail_ids = set(fail_ids)
        self.raise_ids = set(raise_ids)
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0
        self.calls: list[tuple[str, dict]] = []
        self.gated_proof_types: dict[str, str] = {}

    def plan(self, task_id: str, session_id: str = "", **kwargs) -> dict:
        with self._lock:
            self.calls.append((task_id, dict(kwargs)))
            self._running += 1
            self.peak = max(self.peak, self._running)
        try:
            time.sleep(self.delay)
            if task_id in self.raise_ids:
                raise RuntimeError("child exploded mid-drive")
            if task_id in self.fail_ids:
                return {"ok": False, "task_id": task_id,
                        "final_step": "story_gate", "gate_state": "failed",
                        "reason": "story_gate latched failed: rubric said no",
                        "stats": {"overrides": 0}}
            child = self.task_svc.get(task_id)
            self.gated_proof_types[task_id] = child.proof_type
            self.task_svc.update(task_id, status="done",
                                 completion_proof=STRONG_PROOF)
            return {"ok": True, "task_id": task_id,
                    "final_step": "plan_gate", "gate_state": "passed",
                    "stats": {"overrides": 0}}
        finally:
            with self._lock:
                self._running -= 1


def _seed_epic(task_svc, allowed, proof_types=None):
    """Create an epic + one child per allowed_files list. Returns
    (epic, [children])."""
    epic = task_svc.create(title="fan-out epic fixture")
    kids = []
    for i, files in enumerate(allowed):
        pt = (proof_types or [])[i] if proof_types and i < len(proof_types) \
            else "test"
        kids.append(task_svc.create(
            title=f"fan-out child {i}", parent_id=epic.id,
            allowed_files=list(files), proof_type=pt,
            oracle=f"child {i} oracle"))
    return epic, kids


# ── AC-1: disjoint children drive concurrently; roll-up passes ─────────

def test_disjoint_children_drive_concurrently_and_rollup_passes(tmp_path):
    df = _fanout_mod()
    svc = _task_svc(tmp_path)
    epic, kids = _seed_epic(
        svc, [["a.py"], ["b.py"], ["c.py"]],
        proof_types=["test", "demo", "artifact"])
    eng = InstrumentedEngine(svc)
    res = df.fan_out(epic.id, engine=eng, task_svc=svc,
                     session_id="fanout-test", max_parallel=3)
    assert res["ok"] is True, res
    assert res["epic_id"] == epic.id
    # One wave, all three children in it, genuine concurrent overlap.
    assert res["waves"] == [[k.id for k in kids]], res["waves"]
    assert eng.peak >= 2, f"no genuine overlap observed (peak={eng.peak})"
    assert res["stats"]["max_concurrent"] >= 2, res["stats"]
    # Every child's gate passed on its OWN declared proof_type.
    for k in kids:
        assert res["children"][k.id]["ok"] is True, res["children"][k.id]
        assert res["children"][k.id]["gate_state"] == "passed"
    assert eng.gated_proof_types == {
        kids[0].id: "test", kids[1].id: "demo", kids[2].id: "artifact"}
    # Epic roll-up passes via conductor_service.epic_rollup_verdict.
    assert res["rollup"]["ok"] is True, res["rollup"]
    assert "roll-up" in res["rollup"]["reason"]
    # Zero self-overrides anywhere.
    assert res["stats"]["overrides"] == 0
    assert all(not k.get("override") for _, k in eng.calls), eng.calls


# ── AC-2: overlapping children are the collision boundary ──────────────

def test_overlapping_children_are_serialized(tmp_path):
    df = _fanout_mod()
    svc = _task_svc(tmp_path)
    epic, kids = _seed_epic(
        svc, [["shared.py", "x.py"], ["shared.py", "y.py"]])
    eng = InstrumentedEngine(svc)
    res = df.fan_out(epic.id, engine=eng, task_svc=svc, max_parallel=3)
    assert res["ok"] is True, res
    # Never the same wave, never observed concurrent.
    assert res["waves"] == [[kids[0].id], [kids[1].id]], res["waves"]
    assert eng.peak == 1, f"colliding pair overlapped (peak={eng.peak})"
    assert all(not k.get("override") for _, k in eng.calls), eng.calls


# ── AC-3: one child's failure never aborts siblings ────────────────────

def test_child_failure_is_isolated(tmp_path):
    df = _fanout_mod()
    svc = _task_svc(tmp_path)
    epic, kids = _seed_epic(svc, [["a.py"], ["b.py"], ["c.py"]])
    eng = InstrumentedEngine(
        svc, fail_ids={kids[0].id}, raise_ids={kids[1].id})
    res = df.fan_out(epic.id, engine=eng, task_svc=svc, max_parallel=3)
    # ALL children were attempted — no sibling abort.
    assert len(eng.calls) == 3, eng.calls
    failed = res["children"][kids[0].id]
    assert failed["ok"] is False
    assert "rubric said no" in failed["reason"]
    raised = res["children"][kids[1].id]
    assert raised["ok"] is False
    assert "child exploded mid-drive" in raised["reason"]
    healthy = res["children"][kids[2].id]
    assert healthy["ok"] is True, healthy
    assert svc.get(kids[2].id).status == "done"
    # Epic is not green: the roll-up reports it, fan-out is not ok.
    assert res["ok"] is False
    assert res["rollup"]["ok"] is False
    assert "not done" in res["rollup"]["reason"], res["rollup"]
    assert all(not k.get("override") for _, k in eng.calls), eng.calls


# ── AC-4: no override surface exists at all ────────────────────────────

def test_no_override_surface():
    df = _fanout_mod()
    assert "override" not in inspect.signature(df.fan_out).parameters


# ── AC-5: partitioning is deterministic and conservative ───────────────

def test_wave_partition_deterministic_and_conservative():
    df = _fanout_mod()
    kids = [
        SimpleNamespace(id="c1", allowed_files=["a.py"]),
        SimpleNamespace(id="c2", allowed_files=["b.py"]),
        SimpleNamespace(id="c3", allowed_files=["a.py", "z.py"]),
        SimpleNamespace(id="c4", allowed_files=[]),  # empty = collides all
        SimpleNamespace(id="c5", allowed_files=["q.py"]),
    ]
    first = [[c.id for c in wave] for wave in df.partition_waves(kids)]
    second = [[c.id for c in wave] for wave in df.partition_waves(kids)]
    assert first == second, "partitioning must be deterministic"
    # c3 collides with c1 (a.py) -> later wave; c4 (empty) rides ALONE.
    waves_of = {cid: i for i, wave in enumerate(first) for cid in wave}
    assert waves_of["c1"] != waves_of["c3"]
    solo_wave = first[waves_of["c4"]]
    assert solo_wave == ["c4"], f"empty allowed_files must ride alone: {first}"
    # Disjoint c1/c2/c5 share the first wave (concurrency preserved).
    assert waves_of["c1"] == waves_of["c2"] == waves_of["c5"] == 0, first


# ── max_parallel bounds concurrency even for disjoint children ─────────

def test_max_parallel_one_serializes_a_disjoint_wave(tmp_path):
    df = _fanout_mod()
    svc = _task_svc(tmp_path)
    epic, _ = _seed_epic(svc, [["a.py"], ["b.py"], ["c.py"]])
    eng = InstrumentedEngine(svc, delay=0.02)
    res = df.fan_out(epic.id, engine=eng, task_svc=svc, max_parallel=1)
    assert res["ok"] is True, res
    assert eng.peak == 1, f"max_parallel=1 must serialize (peak={eng.peak})"
