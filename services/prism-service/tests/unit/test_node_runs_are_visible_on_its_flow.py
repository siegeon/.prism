"""A node's runs are visible on its flow (task 1cdf1d70).

THE PROBLEM. /workflows -> WRITE FAILING TESTS (under VERIFIER) draws four
nodes -- loop, write, run, commit -- and showed no history and no motion
for any of them. Measured live 2026-09-10 on task ab9166d5: a caller drove
write-test-file, run-pinned-suite and commit-tests-only through their own
routes directly (write-test-file wrote 9701 bytes, run-pinned-suite
returned rc=1, commit-tests-only made a real commit the conductor-
adjudicator accepted as the red anchor), and run_count stayed 0 for all
three the whole time.

TWO BLIND CHANNELS.

(1) HISTORY. `_attach_node_trend` (api/workflows.py) already reads
node_run_counts/node_recent_runs off agent_runs for a behaviour's own
sub-steps -- that machinery works. What was missing on the WRITE side:
write-test-file, run-pinned-suite and commit-tests-only never wrote an
agent_runs row of their own. A row only ever appeared when
task_runner's declared-chain dispatch called the route IN-PROCESS and
recorded it FOR the route (task_runner._record_codified_run) -- a caller
that hit the same route directly left nothing. This file pins the fix:
each route now self-records from inside the handler
(api/workflows._record_node_run), so the count means "this route ran",
not "task_runner drove it". task_runner's own dispatch loop stops
double-recording the same three routes now that the handler is the one
place both paths go through (task_runner._SELF_RECORDING_ROUTES).

(2) A count alone cannot say WHEN. agent_runs_data.node_last_run and the
`last_run_at` field carried onto each step (and through to the canvas's
TokenTrend.lastRunAt) answer that: "each of the four nodes shows its last
run time and its run total" (the task's own oracle wording).

The canvas's MOTION half (the ambient occupancy badge, `n.count > 0 ->
drawOccupancy`) already reads `running_now`/occupancy off the same
agent_runs spine `_attach_node_trend` computes -- fixing the write side
above is what makes that existing, already-wired mechanism light up for
these three nodes; nothing about the ambient-motion contract itself
("a still board is honestly still", WorkflowsPage.tsx) changes here.
"""

from __future__ import annotations

import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from prism_service.api import workflows as wf
from prism_service.services import agent_runs_data, task_runner

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parents[2]
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _read(*parts: str) -> str:
    path = _WEB.joinpath(*parts)
    assert path.exists(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


def _wire_scores_db(monkeypatch, tmp_path) -> Path:
    """Point every self-recorder at a scratch scores.db, so a test can
    read back real rows through the same functions the canvas reads."""
    db = tmp_path / "scores.db"
    monkeypatch.setattr(task_runner, "_scores_db_for", lambda project: str(db))
    return db


# ----------------------------------------------------------------------
# BACKEND: node_last_run -- the "when" half of the oracle
# ----------------------------------------------------------------------

def _seed(db: Path, rows: list[tuple[str, float]]) -> None:
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE agent_runs (step TEXT, started_at REAL)")
    conn.executemany(
        "INSERT INTO agent_runs (step, started_at) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()


def test_node_last_run_reads_the_most_recent_started_at(tmp_path):
    db = tmp_path / "scores.db"
    _seed(db, [("write-test-file", 100.0), ("write-test-file", 200.0),
              ("run-pinned-suite", 150.0)])

    out = agent_runs_data.node_last_run(
        str(db), ["write-test-file", "run-pinned-suite", "commit-tests-only"])

    assert out["write-test-file"] == 200.0, out
    assert out["run-pinned-suite"] == 150.0, out
    # never run: an honest None, never a fabricated 0
    assert out["commit-tests-only"] is None, out


def test_node_last_run_degrades_honestly_on_an_unreadable_db(tmp_path):
    out = agent_runs_data.node_last_run(
        str(tmp_path / "nope.db"), ["write-test-file"])
    assert out == {"write-test-file": None}


def test_attach_node_trend_stamps_last_run_at(tmp_path):
    db = tmp_path / "scores.db"
    _seed(db, [("write-test-file", 1700000000.0)])
    steps = [{"id": "write",
              "url": "http://x/api/workflows/steps/write-test-file?project=p"}]

    wf._attach_node_trend(db, steps)

    assert steps[0]["last_run_at"] == 1700000000.0, steps[0]
    assert steps[0]["run_count"] == 1, steps[0]


def test_attach_node_trend_reports_none_for_a_node_that_never_ran(tmp_path):
    steps = [{"id": "commit",
              "url": "http://x/api/workflows/steps/commit-tests-only?project=p"}]
    wf._attach_node_trend(tmp_path / "nope.db", steps)
    assert steps[0]["last_run_at"] is None, steps[0]


# ----------------------------------------------------------------------
# BACKEND: a DIRECT call to a build route is counted -- the misfire the
# task's own likely_misfire names
# ----------------------------------------------------------------------

def test_write_test_file_records_its_own_run_on_a_direct_call(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    db = _wire_scores_db(monkeypatch, tmp_path)

    body = wf.WriteTestFileRequest(
        task_id="t-1", test_file_path="test_x.py",
        test_code="def test_x():\n    assert True\n")
    out = wf.workflow_step_write_test_file(body, project="p")

    assert out["outcome"] == "ok", out
    counts = agent_runs_data.node_run_counts(str(db), ["write-test-file"])
    assert counts["write-test-file"] == 1, (
        "a direct call to the route must be counted exactly like a "
        f"task_runner-dispatched one: {counts}")


def test_write_test_file_records_a_refused_run_too(monkeypatch, tmp_path):
    """A refusal is still a REAL run of the node -- it happened, it just
    didn't succeed. Silently dropping it would make the count lie again,
    just in the other direction."""
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    db = _wire_scores_db(monkeypatch, tmp_path)

    body = wf.WriteTestFileRequest(
        task_id="t-1", test_file_path="not_a_test.py", test_code="x = 1\n")
    out = wf.workflow_step_write_test_file(body, project="p")

    assert out["outcome"] == "refused", out
    counts = agent_runs_data.node_run_counts(str(db), ["write-test-file"])
    assert counts["write-test-file"] == 1, counts


def test_run_pinned_suite_records_its_own_run_on_a_direct_call(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    db = _wire_scores_db(monkeypatch, tmp_path)

    body = wf.RunPinnedSuiteRequest(task_id="t-1", paths=["test_ok.py"])
    out = wf.workflow_step_run_pinned_suite(body, project="p")

    assert out["outcome"] == "ok", out
    counts = agent_runs_data.node_run_counts(str(db), ["run-pinned-suite"])
    assert counts["run-pinned-suite"] == 1, counts


def test_commit_tests_only_records_its_own_run_on_a_direct_call(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.example"],
                   cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True)
    (root / "test_x.py").write_text("def test_x():\n    assert True\n")
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    db = _wire_scores_db(monkeypatch, tmp_path)

    body = wf.CommitTestsOnlyRequest(task_id="t-1")
    out = wf.workflow_step_commit_tests_only(body, project="p")

    assert out["outcome"] == "ok", out
    counts = agent_runs_data.node_run_counts(str(db), ["commit-tests-only"])
    assert counts["commit-tests-only"] == 1, counts


def test_no_task_id_records_nothing(monkeypatch, tmp_path):
    """An adhoc call with no task has nothing a node card could key
    history on -- this must degrade quietly, never raise."""
    db = _wire_scores_db(monkeypatch, tmp_path)
    wf._record_node_run("p", "", "write-test-file", True, "adhoc")
    assert not db.exists()


# ----------------------------------------------------------------------
# task_runner must not DOUBLE-count a route that now self-records
# ----------------------------------------------------------------------

def test_task_runner_declares_exactly_the_three_build_routes_as_self_recording():
    """reason-loop stays OUT: it is a real model call (tokens, cost),
    unlike the three deterministic build routes, so folding it into the
    same zero-token self-recorder is separate follow-up work, not this
    ticket's measured misfire."""
    assert task_runner._SELF_RECORDING_ROUTES == frozenset(
        {"write-test-file", "run-pinned-suite", "commit-tests-only"})


def test_the_dispatch_loop_skips_self_recording_routes_before_recording_again():
    """_run_one_step's dispatched-rows loop is deep inside one large
    function with heavy external dependencies (claim service, budget,
    claude_cli) -- there is no seam to call it in isolation, so this pins
    the guard at the source, the same convention the project already uses
    for UI ACs it cannot exercise through a JS test runner.

    Without this guard, a task_runner-dispatched write_failing_tests chain
    would record each build route TWICE: once from the handler's own new
    self-record, once again from this loop -- doubling run_count for
    every chain-driven run.
    """
    import inspect

    src = inspect.getsource(task_runner)
    marker = "for row in dispatched:"
    assert marker in src, "task_runner no longer dispatches declared rows"
    loop = src[src.index(marker):]
    loop = loop[:loop.index("\n\n")]
    assert "_SELF_RECORDING_ROUTES" in loop and "continue" in loop, (
        "the dispatched-rows loop no longer skips a route that already "
        f"self-recorded:\n{loop}")


def test_the_slice_never_touches_a_control_plane_policy_file():
    from prism_service.services.control_plane import POLICY_FILES

    touched = {
        "services/prism-service/prism_service/services/agent_runs_data.py",
        "services/prism-service/prism_service/services/task_runner.py",
        "services/prism-service/prism_service/api/workflows.py",
        "services/prism-service/prism_service/web/src/lib/useWorkflowDef.ts",
        "services/prism-service/prism_service/web/src/live/workflowGraph.ts",
    }
    assert not (touched & set(POLICY_FILES)), (touched, POLICY_FILES)


# ----------------------------------------------------------------------
# UI (no JS test runner in this repo -- pin the ACTUAL TSX/TS source,
# same convention as tests/unit/test_conductor_page_animated_cleanup_ui.py
# and tests/unit/test_node_shows_multiplier_and_token_trend.py)
# ----------------------------------------------------------------------

def _trend_label_fn(src: str) -> str:
    marker = "function tokenTrendLabel"
    assert marker in src, "workflowGraph.ts declares no tokenTrendLabel()"
    body = src[src.index(marker):]
    rest = body[len(marker):]
    ends = [i for i in (rest.find("\nfunction "), rest.find("\nexport "),
                        rest.find("\nconst ")) if i != -1]
    return body[:len(marker) + min(ends)] if ends else body


def test_the_workflow_def_type_carries_last_run_at():
    src = _read("lib", "useWorkflowDef.ts")
    assert "last_run_at" in src, (
        "WorkflowDefStep has no last_run_at field -- the server can send "
        "it but the SPA type has nowhere to put it")


def test_the_canvas_carries_and_derives_last_run_at():
    src = _read("live", "workflowGraph.ts")
    assert "lastRunAt" in src, "TokenTrend has no lastRunAt field"
    assert "s.last_run_at" in src, (
        "setDef never reads the API's last_run_at field onto a node's "
        "own tokenTrend")


def test_the_trend_label_shows_when_a_codified_node_last_ran():
    """AC: 'each of the four nodes shows its last run time and its run
    total.' A run count alone answers only the second half."""
    src = _read("live", "workflowGraph.ts")
    label_fn = _trend_label_fn(src)
    assert "lastRunAt" in label_fn, (
        f"tokenTrendLabel never reads lastRunAt:\n{label_fn}")
    assert "relativeTime(" in label_fn, (
        f"the last-run time is not rendered as an age string:\n{label_fn}")


def test_an_indeterminate_node_still_never_paints_a_fabricated_number():
    """Regression guard (task 112dbb72's own stop_if, unaffected by this
    slice): the indeterminate branch must still short-circuit before any
    number formatting, and a genuinely-never-run node must still render
    the honest placeholder, never a computed figure."""
    src = _read("live", "workflowGraph.ts")
    label_fn = _trend_label_fn(src)
    assert re.search(r"indeterminate.*return", label_fn, re.S), label_fn
    assert "×?" in label_fn, label_fn


def test_the_relative_time_helper_is_imported_not_reimplemented():
    src = _read("live", "workflowGraph.ts")
    assert 'from "@/lib/relativeTime"' in src, (
        "the canvas must reuse the existing age-string helper "
        "(lib/relativeTime.ts), never a second ad hoc formatter")
