"""Task dc815149: the planner cuts slices that RUN IN PARALLEL.

A slice exists for one reason -- to run N `claude -p` instances at the same
time and then to put the results back together. A split whose slices cannot
run at the same time has failed at its only job, and nothing in PRISM checks
the cut today.

Two helpers already exist and neither can answer this. `overlapping_allowed_
files` and `can_run_parallel` (services/conductor_service.py:756-772) compare
paths with a plain `set` intersection, so `api/` claimed by one slice and
`api/brain.py` claimed by another read as DISJOINT -- the live defect AC-4
pins. That file is a control_plane.POLICY_FILES entry, so this slice does not
touch it: the whole computation goes in a NEW pure module,
`prism_service/services/plan_partition.py`, and two CODIFIED routes below
`/api/workflows/steps/` expose it (same shape as workflow_step_red_test_ids,
api/workflows.py:2178).

CODIFIED means codified: neither node calls a model (AC-6), and each node
call writes one ZERO-TOKEN agent_runs row (AC-5) in the shape
task_runner._record_codified_run already uses, so the saving is visible in
the epic score instead of invisible.

Every test below is RED at the base commit cec813df: `plan_partition.py` and
the two routes are created by this slice, so nothing there answers any of
these questions. A run of these node ids at cec813df exits rc 4 -- that is
UNMEASURABLE, never a pass.
"""
from __future__ import annotations

import json
import types
from pathlib import Path


def _repo_root() -> Path:
    # tests/unit/<this file> -> unit -> tests -> prism-service -> services -> repo root
    return Path(__file__).resolve().parent.parent.parent.parent.parent


class _FakeTask:
    def __init__(self, id, allowed_files=None, title="", status="pending",
                 oracle=""):
        self.id = id
        self.title = title
        self.allowed_files = list(allowed_files or [])
        self.status = status
        self.oracle = oracle


class _FakeTaskSvc:
    """Serves children by parent_id and single rows by id, like TaskService."""

    def __init__(self, rows=None):
        self._rows = list(rows or [])

    def list(self, parent_id=None, **kw):
        return list(self._rows)

    def get(self, tid):
        for r in self._rows:
            if r.id == tid:
                return r
        return None


def _wire(monkeypatch, workflows_api, tmp_path, rows=None):
    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(
            _data_dir=tmp_path, task_svc=_FakeTaskSvc(rows)))


def _capture_runs(monkeypatch):
    from prism_service.services import agent_runs_data

    seen: list[dict] = []
    monkeypatch.setattr(
        agent_runs_data, "upsert_agent_run",
        lambda db, row: seen.append(dict(row)))
    return seen


# --- AC-1 -------------------------------------------------------------------

def test_two_children_sharing_a_file_are_not_parallel_safe():
    """Two proposed children whose allowed_files share ONE path cannot run at
    the same time, so the cut is refused. RED at cec813df: no code in the tree
    answers this at all."""
    from prism_service.services import plan_partition as pp

    result = pp.partition([
        pp.Slice(id="child-a", title="A",
                 allowed_files=["services/prism-service/prism_service/api/tasks.py",
                                "shared/util.py"]),
        pp.Slice(id="child-b", title="B",
                 allowed_files=["shared/util.py",
                                "services/prism-service/prism_service/api/brain.py"]),
    ])

    assert result.parallel_safe is False
    assert result.fan_out == 0, "zero slices can start together on an unsafe cut"
    assert result.collisions, "an unsafe cut must carry the rows that force it"
    assert result.to_dict()["parallel_safe"] is False


# --- AC-2 -------------------------------------------------------------------

def test_the_check_names_the_files_that_force_the_overlap():
    """The refusal names the EXACT file and the EXACT pair it joins, so the
    planner can RE-CUT instead of serialise -- serialising is what an operator
    already did by hand on 2026-08-30 and is the outcome this node exists to
    prevent. Literal expectations only: never partition() applied to its own
    input."""
    from prism_service.services import plan_partition as pp

    result = pp.partition([
        pp.Slice(id="alpha", title="A", allowed_files=["api/brain.py", "a_only.py"]),
        pp.Slice(id="beta", title="B", allowed_files=["api/brain.py", "b_only.py"]),
        pp.Slice(id="gamma", title="C", allowed_files=["c_only.py"]),
    ])

    assert result.parallel_safe is False
    rows = [(c.path, c.slice_a, c.slice_b) for c in result.collisions]
    assert rows == [("api/brain.py", "alpha", "beta")], rows
    assert "api/brain.py" in result.reason
    # gamma is disjoint from both, so it is never named in a collision row.
    assert all("gamma" not in (a, b) for _, a, b in rows)


# --- AC-3 -------------------------------------------------------------------

def test_a_disjoint_cut_reports_its_fan_out_width():
    """A disjoint cut is parallel-safe and reports the count of slices that can
    start at the same time."""
    from prism_service.services import plan_partition as pp

    result = pp.partition([
        pp.Slice(id="one", title="1", allowed_files=["pkg/one.py"]),
        pp.Slice(id="two", title="2", allowed_files=["pkg/two.py"]),
        pp.Slice(id="three", title="3", allowed_files=["pkg/three.py", "docs/x.md"]),
    ])

    assert result.parallel_safe is True
    assert result.fan_out == 3
    assert result.collisions == []
    assert sorted(result.slice_ids) == ["one", "three", "two"]


# --- AC-4 (the live defect) -------------------------------------------------

def test_a_directory_contains_a_file_and_counts_as_overlap():
    """A DIRECTORY claimed by one slice and a FILE below it claimed by another
    is an overlap. The shipped helper `overlapping_allowed_files`
    (conductor_service.py:756-772) intersects the two sets as plain strings and
    returns EMPTY here -- the wrong answer, on shipped code, at cec813df. The
    containment test runs in both directions, and normalises `./` and a
    trailing separator first."""
    from prism_service.services import plan_partition as pp

    forward = pp.partition([
        pp.Slice(id="dir-side", title="D", allowed_files=["api/"]),
        pp.Slice(id="file-side", title="F", allowed_files=["./api/brain.py"]),
    ])
    assert forward.parallel_safe is False
    assert [(c.path, c.slice_a, c.slice_b) for c in forward.collisions] == [
        ("api/brain.py", "dir-side", "file-side")]

    reversed_order = pp.partition([
        pp.Slice(id="file-side", title="F", allowed_files=["api/brain.py"]),
        pp.Slice(id="dir-side", title="D", allowed_files=["api"]),
    ])
    assert reversed_order.parallel_safe is False, (
        "containment must be tested in BOTH directions")

    # A sibling whose name merely starts with the same characters is NOT
    # contained: `api_v2/brain.py` is a different tree from `api/`.
    assert pp.partition([
        pp.Slice(id="d", title="D", allowed_files=["api/"]),
        pp.Slice(id="s", title="S", allowed_files=["api_v2/brain.py"]),
    ]).parallel_safe is True

    assert pp.contains("api", "api/brain.py") is True
    assert pp.contains("api/brain.py", "api") is False
    assert pp.normalise("./api//brain.py") == "api/brain.py"
    assert pp.normalise("api/") == "api"


# --- AC-5 -------------------------------------------------------------------

def test_every_orchestration_node_records_a_zero_token_run(tmp_path, monkeypatch):
    """Each node call writes ONE agent_runs row at zero tokens and zero cost,
    the shape task_runner._record_codified_run uses -- otherwise the
    orchestration stays invisible in the epic score and nobody can tell whether
    planning got faster."""
    from prism_service.api import workflows as workflows_api

    _wire(monkeypatch, workflows_api, tmp_path)
    seen = _capture_runs(monkeypatch)

    workflows_api.workflow_step_plan_cut(
        workflows_api.PlanCutRequest(
            task_id="epic-1",
            slices=[workflows_api.SliceSpec(id="a", allowed_files=["a.py"]),
                    workflows_api.SliceSpec(id="b", allowed_files=["b.py"])]),
        project="prism")
    workflows_api.workflow_step_plan_sew(
        workflows_api.PlanSewRequest(
            task_id="epic-1", assembler="a",
            slices=[workflows_api.SliceSpec(id="a", allowed_files=["a.py"]),
                    workflows_api.SliceSpec(id="b", allowed_files=["b.py"])]),
        project="prism")

    assert len(seen) == 2, f"one row per node call, got {seen}"
    assert [r["step"] for r in seen] == ["plan-cut", "plan-sew"]
    for row in seen:
        assert row["model"] == "codified"
        assert row["tokens"] == 0
        assert row["cost_usd"] == 0.0
        assert row["task_id"] == "epic-1"


# --- AC-6 -------------------------------------------------------------------

def test_the_orchestration_nodes_make_no_model_call(tmp_path, monkeypatch):
    """A codified node that calls a model puts the cost back into the very
    phase this exists to make free. A stub that RAISES if it is touched must
    never fire, and the pure module must import no model client at all."""
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    def _boom(*a, **kw):
        raise AssertionError("a codified orchestration node called a model")

    monkeypatch.setattr(claude_cli, "invoke", _boom)
    _wire(monkeypatch, workflows_api, tmp_path)
    _capture_runs(monkeypatch)

    slices = [workflows_api.SliceSpec(id="a", allowed_files=["a.py"]),
              workflows_api.SliceSpec(id="b", allowed_files=["b.py"])]
    workflows_api.workflow_step_plan_cut(
        workflows_api.PlanCutRequest(task_id="epic-1", slices=slices),
        project="prism")
    workflows_api.workflow_step_plan_sew(
        workflows_api.PlanSewRequest(task_id="epic-1", assembler="a",
                                     slices=slices),
        project="prism")

    src = (_repo_root() / "services/prism-service/prism_service/services"
           / "plan_partition.py").read_text(encoding="utf-8")
    for banned in ("claude_cli", "anthropic", "subprocess", "import os"):
        assert banned not in src, (
            f"plan_partition.py must be pure Python over its arguments; "
            f"found {banned!r}")


# --- AC-7 -------------------------------------------------------------------

def test_the_sew_node_reports_a_cut_with_no_assembler_as_incomplete(
        tmp_path, monkeypatch):
    """A cut proved parallel-safe with nobody assembling leaves N green slices
    and an undone parent (the 0784729f failure). The SEW half names the
    assembler and the clauses the PARENT must demonstrate itself."""
    from prism_service.api import workflows as workflows_api

    parent = _FakeTask(id="epic-1", oracle="Clause one.\n\nClause two.\n")
    _wire(monkeypatch, workflows_api, tmp_path, rows=[parent])
    _capture_runs(monkeypatch)

    slices = [workflows_api.SliceSpec(id="a", allowed_files=["a.py"]),
              workflows_api.SliceSpec(id="b", allowed_files=["b.py"])]

    missing = workflows_api.workflow_step_plan_sew(
        workflows_api.PlanSewRequest(task_id="epic-1", assembler="", slices=slices),
        project="prism")
    assert missing.complete is False
    assert missing.assembler == ""
    assert "assembler" in missing.reason.lower()

    named = workflows_api.workflow_step_plan_sew(
        workflows_api.PlanSewRequest(task_id="epic-1", assembler="a", slices=slices),
        project="prism")
    assert named.complete is True
    assert named.assembler == "a"
    assert named.parent_must_demonstrate == ["Clause one.", "Clause two."]

    # An assembler that is not one of the slices is not an assembler.
    stranger = workflows_api.workflow_step_plan_sew(
        workflows_api.PlanSewRequest(task_id="epic-1", assembler="zzz",
                                     slices=slices),
        project="prism")
    assert stranger.complete is False


# --- AC-8 -------------------------------------------------------------------

def test_both_nodes_render_below_conductor():
    """A mechanism that exists and renders nowhere is the built-but-unwired
    fault this node was made to catch. Source half of AC-8: both ids sit in
    `_CONDUCTOR_LINKED_BEHAVIOR_IDS` and each has a Behavior file below
    .prism/behaviors/conductor/. The rendered half is a screenshot at
    verify_green_state -- a source read proves wiring, never pixels."""
    root = _repo_root()
    src = (root / "services/prism-service/prism_service/api/workflows.py"
           ).read_text(encoding="utf-8")

    marker = "_CONDUCTOR_LINKED_BEHAVIOR_IDS = ("
    start = src.index(marker)
    end = src.index("\n    )\n", start)
    block = src[start:end]

    for node_id in ("plan-cut", "plan-sew"):
        assert f'"{node_id}"' in block, (
            f"{node_id} is not in _CONDUCTOR_LINKED_BEHAVIOR_IDS, so its card "
            f"never nests below Conductor")
        path = root / ".prism/behaviors/conductor" / f"{node_id}.json"
        assert path.exists(), f"no Behavior file for {node_id}"
        doc = json.loads(path.read_text(encoding="utf-8"))
        assert doc["id"] == node_id
        assert doc["botId"] == "conductor"
        assert doc["steps"][0]["kind"] == "http-callback"
        assert f"/api/workflows/steps/{node_id}" in doc["steps"][0]["url"]


# --- AC-9 -------------------------------------------------------------------

def test_the_2026_08_30_epic_replays_its_three_overlaps(tmp_path, monkeypatch):
    """The check replays the REAL case: epic 4c9b39e5, whose three overlaps a
    person found by hand on 2026-08-30. The recorded allowlists are held here
    so the contract holds even when the live rows move. Cancelled and deleted
    children are dropped before the cut, per the 72ccaf94 lesson."""
    from prism_service.api import workflows as workflows_api

    rows = [
        _FakeTask(id="0ee4dc98", title="brain api",
                  allowed_files=["services/prism-service/prism_service/api/brain.py",
                                 "services/prism-service/prism_service/web/src/"
                                 "pages/DashboardPage.tsx",
                                 "services/prism-service/prism_service/services/"
                                 "brain_health.py"]),
        _FakeTask(id="1edee95c", title="dashboard",
                  allowed_files=["services/prism-service/prism_service/api/brain.py",
                                 "services/prism-service/prism_service/web/src/"
                                 "pages/DashboardPage.tsx"]),
        _FakeTask(id="013c5197", title="brain health",
                  allowed_files=["services/prism-service/prism_service/services/"
                                 "brain_health.py"]),
        _FakeTask(id="cancelled-one", title="dead", status="cancelled",
                  allowed_files=["services/prism-service/prism_service/api/brain.py"]),
    ]
    _wire(monkeypatch, workflows_api, tmp_path, rows=rows)
    _capture_runs(monkeypatch)

    resp = workflows_api.workflow_step_plan_cut(
        workflows_api.PlanCutRequest(task_id="4c9b39e5", parent_id="4c9b39e5"),
        project="prism")

    assert resp.parallel_safe is False
    assert resp.fan_out == 0
    rows_out = [(c["path"], c["slice_a"], c["slice_b"]) for c in resp.collisions]
    assert rows_out == [
        ("services/prism-service/prism_service/api/brain.py",
         "0ee4dc98", "1edee95c"),
        ("services/prism-service/prism_service/services/brain_health.py",
         "013c5197", "0ee4dc98"),
        ("services/prism-service/prism_service/web/src/pages/DashboardPage.tsx",
         "0ee4dc98", "1edee95c"),
    ], rows_out
    assert "cancelled-one" not in resp.slice_ids, (
        "a cancelled child is not a live slice (task 72ccaf94)")
