r"""Red tests for task 013c5197 -- "A finished play indexes what it wrote".

TRACE. Each test names the acceptance criterion it pins, the measurement
that is RED at the base commit c7f72150, and the file the fix lands in.
The task's `verify` pins exactly the three test functions in this file.

  AC-1 a finished play indexes what it wrote, INCREMENTALLY
       RED AT BASE: `git ls-files
       prism_service/services/brain_health.py` -> no match; the module is
       in no commit, so `import prism_service.services.brain_health` in
       the test body raises ModuleNotFoundError.
       FIX LANDS IN: prism_service/services/brain_health.py
  AC-2 coverage below the floor is reported, never only logged
       RED AT BASE: same absent module, AND independently
       `grep -n "brain_health\|CoverageBelowFloor"
       prism_service/services/ship_worker.py` -> no match, so the second
       half (a raise becomes a VISIBLE audit row) fails by construction.
       FIX LANDS IN: prism_service/services/brain_health.py and
       prism_service/services/ship_worker.py
  AC-3 the node makes no model call, and runs no thread or timer
       RED AT BASE: the source file to scan is in no commit.
       FIX LANDS IN: prism_service/services/brain_health.py

Every symbol under test is reached LAZILY inside a test body, mirroring
tests/unit/test_brain_health_coverage.py, so a run against the base tree
is a genuine RED (rc==1, real FAILUREs) rather than a collection ERROR
(rc==2), which is what the red_gate machine seat requires.

THE SEAM THIS FILE PINS in ship_worker.py is
`_brain_health_after_land(task_svc, task_id, project)` -- the exact shape
`_reap_after_land` already has at the same pipeline position
(ship_worker.py:635,640). AC-2 drives that function, so the wiring is
proven by behaviour (a history row was written) and not by a source scan.

The stores are DISPOSABLE sqlite/JSONL trees under tmp_path built by the
real MemoryService and BrainService -- never the live /home/siegeon/.prism
store -- so the rule is proven generically.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MODULE = _ROOT / "prism_service" / "services" / "brain_health.py"


class _StubTaskSvc:
    """The two collaborators the node and its ship_worker call site use.

    `sessions_for_task` is TaskService's own contract (task_service.py:1530,
    a list of dicts). `record_history` is what ship_worker._audit calls
    (ship_worker.py:255-262); every row it receives is kept so a test can
    assert the fall was made visible.
    """

    def __init__(self, sessions: list[str]) -> None:
        self._sessions = [{"session_id": s} for s in sessions]
        self.history: list[dict] = []

    def sessions_for_task(self, task_id: str) -> list[dict]:
        return list(self._sessions)

    def record_history(self, task_id, action="", details="", actor="") -> None:
        self.history.append({"task_id": task_id, "action": action,
                             "details": details, "actor": actor})


class _StubCtx:
    """What get_project(project) hands the node: two real services and the
    data dir holding scores.db. Same three attributes the node reads on the
    real ProjectContext (project_context.py:37,84 and `_data_dir`)."""

    def __init__(self, data_dir: Path, memory_svc, brain_svc) -> None:
        self._data_dir = data_dir
        self.memory_svc = memory_svc
        self.brain_svc = brain_svc


# MemoryService.store dedups on >85% description similarity
# (memory_service.py:225), so each seeded memory needs a body that shares
# almost no text with the others. Near-identical bodies collapse the store
# to one active entry and the coverage ratio then reads 1.0 by accident.
_BODIES = [
    "The conductor pipeline lands a branch on origin main and then reaps.",
    "Sigma renders the graph viewer with WebGL on a dark canvas surface.",
    "Simplified Technical English keeps every hedge in an instruction.",
    "A gate is decided by an actor that did not produce the evidence.",
]


def _seed(tmp_path: Path, memory_names: list[str], written_by_play: int):
    """A disposable project: N real memories, the first `written_by_play`
    of them stamped as written by this play's own session.

    Returns (ctx, task_svc, entries) where `entries` is the stored
    ExpertiseEntry list in the same order as `memory_names`. NOTHING is
    indexed into Brain here -- the store starts at zero coverage, so any
    row the node writes is unambiguously the node's own work.
    """
    from prism_service.services.brain_service import BrainService
    from prism_service.services.memory_service import MemoryService

    data_dir = tmp_path / "proj"
    data_dir.mkdir(parents=True, exist_ok=True)
    memory_svc = MemoryService(mulch_dir=str(data_dir / "mulch"))
    brain_svc = BrainService(
        brain_db=str(data_dir / "brain.db"),
        graph_db=str(data_dir / "graph.db"),
        scores_db=str(data_dir / "scores.db"),
    )
    entries = [
        memory_svc.store(domain="decision", name=n,
                         description=_BODIES[i], type="pattern",
                         classification="architecture")
        for i, n in enumerate(memory_names)
    ]
    conn = sqlite3.connect(str(data_dir / "scores.db"), timeout=5.0)
    conn.execute("CREATE TABLE IF NOT EXISTS task_sessions ("
                 "task_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                 "started_at TEXT, ended_at TEXT, "
                 "PRIMARY KEY (task_id, session_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS memory_meta ("
                 "memory_id TEXT PRIMARY KEY, session_id TEXT, status TEXT)")
    conn.execute("INSERT OR REPLACE INTO task_sessions (task_id, session_id) "
                 "VALUES (?, ?)", ("task-013c", "sess-play"))
    for entry in entries[:written_by_play]:
        conn.execute("INSERT OR REPLACE INTO memory_meta "
                     "(memory_id, session_id, status) VALUES (?, ?, ?)",
                     (entry.id, "sess-play", "active"))
    conn.commit()
    conn.close()
    return (_StubCtx(data_dir, memory_svc, brain_svc),
            _StubTaskSvc(["sess-play"]), entries)


def _indexed_source_files(brain_db: Path) -> set[str]:
    """Every expertise memory path present in the Brain store, read straight
    out of `docs` -- the same pre-chunk `source_file` column
    BrainService.expertise_coverage counts (brain_service.py:649)."""
    conn = sqlite3.connect(str(brain_db), timeout=5.0)
    try:
        rows = conn.execute("SELECT DISTINCT source_file FROM docs "
                            "WHERE domain = 'expertise'").fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}


def test_a_finished_play_indexes_what_it_wrote(tmp_path, monkeypatch):
    """AC-1. The play's OWN memory becomes searchable; the store's other
    memory is left untouched, so the pass is incremental and never a
    full-store rebuild (the task's first likely_misfire)."""
    from prism_service.services import brain_health

    ctx, task_svc, entries = _seed(
        tmp_path, ["the play wrote this one", "an older unrelated memory"],
        written_by_play=1)
    monkeypatch.setattr(brain_health, "get_project", lambda project: ctx)

    before = _indexed_source_files(ctx._data_dir / "brain.db")
    assert before == set(), "the scratch store must start with nothing indexed"

    verdict = brain_health.index_finished_play(
        "task-013c", "proj", task_svc=task_svc, floor=0.0)

    after = _indexed_source_files(ctx._data_dir / "brain.db")
    mine = f"memory/{entries[0].domain}/{entries[0].id}"
    other = f"memory/{entries[1].domain}/{entries[1].id}"
    assert mine in after, f"the play's own memory {mine} was not indexed"
    assert other not in after, (
        "a memory this play never wrote was indexed too -- that is a "
        "full-store pass, which the task forbids")
    assert verdict["reindexed"] == 1
    assert verdict["node_id"] == brain_health.HEALTH_NODE
    assert verdict["outcome"] == "pass"


def test_coverage_below_the_floor_is_reported_not_swallowed(tmp_path,
                                                            monkeypatch):
    """AC-2. Under the floor the node RAISES with the real numbers, and the
    ship_worker call site turns that raise into a history row a person can
    read -- never a bare `except: pass` (the task's second likely_misfire)."""
    from prism_service.services import brain_health, ship_worker

    ctx, task_svc, _ = _seed(
        tmp_path, ["play memory a", "store memory b", "store memory c",
                   "store memory d"],
        written_by_play=1)
    monkeypatch.setattr(brain_health, "get_project", lambda project: ctx)

    try:
        brain_health.index_finished_play("task-013c", "proj",
                                         task_svc=task_svc)
    except brain_health.CoverageBelowFloor as exc:
        assert exc.entries == 4
        assert exc.indexed == 1
        assert exc.ratio == 0.25
        assert exc.floor == brain_health.DEFAULT_COVERAGE_FLOOR == 0.5
        assert "1/4" in str(exc) and "25" in str(exc)
    else:
        raise AssertionError(
            "coverage of 1/4 is under the 0.5 floor and the node returned "
            "quietly -- a number nobody acts on is the failure this task "
            "exists to stop")

    reporting = _StubTaskSvc(["sess-play"])

    def _fall(task_id, project, **kw):
        raise brain_health.CoverageBelowFloor(entries=4, indexed=1,
                                              ratio=0.25, floor=0.5)

    monkeypatch.setattr(brain_health, "index_finished_play", _fall)
    ship_worker._brain_health_after_land(reporting, "task-013c", "proj")

    said = " ".join(r["details"] for r in reporting.history)
    assert reporting.history, (
        "ship_worker swallowed the fall -- no history row reached the task")
    assert "1/4" in said, f"the audit row hides the numbers: {said!r}"


def test_the_node_makes_no_model_call(tmp_path):
    """AC-3, plus the task's first two stop_if lines. The node is
    deterministic python: no inference import, no thread, no timer, no
    polling loop -- it never becomes a background sweeper."""
    assert _MODULE.exists(), f"{_MODULE} is not on disk"
    src = _MODULE.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in src.splitlines()]
    imports = [ln for ln in lines
               if ln.startswith("import ") or ln.startswith("from ")]
    offenders = [ln for ln in imports if "prism_service.inference" in ln]
    assert offenders == [], f"the node reaches for a model: {offenders}"
    for banned in ("import threading", "from threading", "import sched",
                   "Thread(", "Timer(", "while True"):
        assert banned not in src, (
            f"{banned!r} in brain_health.py -- the node must not run a "
            "thread, a timer or a polling loop")
