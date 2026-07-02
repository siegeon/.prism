"""RED scaffold — thread-safe task storage for concurrent drives
(task 0584addb, follow-up to the C7 fan-out build f6328e9e / PR #196).

Pins: TaskService must survive genuinely CONCURRENT reads/writes from
N threads against one tasks.db file. Today __init__ binds ONE shared
sqlite3 connection (check_same_thread=False) used by every method, so
concurrent drives interleave statements/commits on a single handle —
observed None reads and "cannot commit - no transaction is active"
(memory mx-655f08). The fix is per-thread connections via a
thread-local connection factory (WAL + busy_timeout stay as
complements, NOT the fix); explicitly NOT a global serialize-everything
lock and NOT WAL-only.

FAILS today: the shared connection races under the hammer (AC-1), the
un-shimmed fan-out engine stub races (AC-2), and both threads observe
the SAME connection object (AC-3).
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _task_svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name))


# Enough contention to make the shared-connection race deterministic:
# rounds x threads x ops keeps the budget bounded (~seconds) while the
# single-handle code trips within the first round in practice.
ROUNDS = 6
THREADS = 8
OPS = 25


def _hammer(svc, thread_idx: int, errors: list, barrier) -> None:
    """One worker: create -> get (must not be None) -> update -> get,
    recording every anomaly instead of raising so the main thread can
    assert on the full picture."""
    barrier.wait()
    for i in range(OPS):
        label = f"t{thread_idx}-op{i}"
        try:
            task = svc.create(title=f"hammer {label}")
            got = svc.get(task.id)
            if got is None:
                errors.append(f"{label}: None read of a row just created")
                continue
            upd = svc.update(task.id, priority=i, description=label)
            if upd is None:
                errors.append(f"{label}: update() returned None for an "
                              "existing row")
                continue
            again = svc.get(task.id)
            if again is None:
                errors.append(f"{label}: None read after update")
            elif again.description != label:
                errors.append(f"{label}: torn read — description "
                              f"{again.description!r} != {label!r}")
        except Exception as exc:  # noqa: BLE001 — the race IS the finding
            errors.append(f"{label}: {type(exc).__name__}: {exc}")


# ── AC-1: N threads hammer one TaskService with zero races ─────────────

def test_concurrent_reads_writes_race_free(tmp_path):
    for rnd in range(ROUNDS):
        svc = _task_svc(tmp_path, name=f"tasks-{rnd}.db")
        errors: list[str] = []
        barrier = threading.Barrier(THREADS)
        threads = [
            threading.Thread(target=_hammer, args=(svc, t, errors, barrier))
            for t in range(THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == [], (
            f"round {rnd}: {len(errors)} race(s) under {THREADS} threads "
            f"x {OPS} ops; first 5: {errors[:5]}")
        # Consistency: every created row must be present and readable.
        rows = svc.list()
        assert len(rows) == THREADS * OPS, (
            f"round {rnd}: expected {THREADS * OPS} rows, "
            f"found {len(rows)} — lost writes")


# ── AC-2: 3-child fan_out with NO serialization shim ───────────────────

class UnshimmedEngine:
    """Engine stub that touches the SHARED TaskService concurrently with
    NO db lock — unlike test_drive_fanout's InstrumentedEngine shim, the
    db work here happens raw on the drive thread (production shape)."""

    def __init__(self, task_svc, ops: int = 15):
        self.task_svc = task_svc
        self.ops = ops
        self.errors: list[str] = []

    def plan(self, task_id: str, session_id: str = "", **kwargs) -> dict:
        try:
            for i in range(self.ops):
                got = self.task_svc.get(task_id)
                if got is None:
                    self.errors.append(f"{task_id}: None mid-drive read")
                self.task_svc.update(task_id, priority=i)
            self.task_svc.update(
                task_id, status="done",
                completion_proof="unshimmed drive walked the row "
                                 f"{self.ops}x and finished it")
        except Exception as exc:  # noqa: BLE001 — the race IS the finding
            self.errors.append(f"{task_id}: {type(exc).__name__}: {exc}")
            return {"ok": False, "task_id": task_id,
                    "reason": f"db race: {exc}", "stats": {"overrides": 0}}
        return {"ok": True, "task_id": task_id, "final_step": "plan_gate",
                "gate_state": "passed", "stats": {"overrides": 0}}


def test_fanout_with_unserialized_task_service(tmp_path):
    from prism_service.services import drive_fanout as df
    for rnd in range(ROUNDS):
        svc = _task_svc(tmp_path, name=f"fanout-{rnd}.db")
        epic = svc.create(title="unshimmed fan-out epic")
        kids = [
            svc.create(title=f"unshimmed child {i}", parent_id=epic.id,
                       allowed_files=[f"file_{i}.py"], proof_type="test",
                       oracle=f"child {i} oracle")
            for i in range(3)
        ]
        eng = UnshimmedEngine(svc)
        res = df.fan_out(epic.id, engine=eng, task_svc=svc,
                         session_id="unshimmed-test", max_parallel=3)
        assert eng.errors == [], (
            f"round {rnd}: db races inside concurrent drives; "
            f"first 5: {eng.errors[:5]}")
        assert res["ok"] is True, f"round {rnd}: {res}"
        assert res["stats"]["max_concurrent"] >= 2, (
            f"round {rnd}: no genuine overlap — the concurrency this "
            f"test exists to prove did not happen: {res['stats']}")
        for k in kids:
            row = svc.get(k.id)
            assert row is not None and row.status == "done", (
                f"round {rnd}: child {k.id} not done after fan_out")


# ── AC-3: connections are per-thread ───────────────────────────────────

def test_connections_are_per_thread(tmp_path):
    svc = _task_svc(tmp_path)
    seen: dict[str, list] = {"main": [], "worker": []}

    # Same thread twice -> the SAME connection (cached, not per-call).
    seen["main"].append(id(svc._db))
    seen["main"].append(id(svc._db))
    assert seen["main"][0] == seen["main"][1], (
        "a single thread must reuse its own connection")

    def grab():
        seen["worker"].append(id(svc._db))
        # The worker's connection must WORK (reads real rows).
        assert svc.list() == []

    t = threading.Thread(target=grab)
    t.start()
    t.join(timeout=30)
    assert seen["worker"], "worker thread never observed a connection"
    assert seen["worker"][0] != seen["main"][0], (
        "distinct threads must observe DISTINCT sqlite connections — a "
        "single shared handle is the race PR #196 documented")
