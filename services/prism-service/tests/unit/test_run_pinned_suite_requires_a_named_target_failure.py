"""A bare rc==1 is not enough to demonstrate red -- the FAILED line must
NAME one of this node's own pinned targets.

LIVE DEFECT (task a65c66e5, 2026-09-14, from the owner directly): the
runner reached write_failing_tests, drafted tests, and red_gate refused
THREE TIMES with "NOT red: the spec's tests PASS at the red-step commit
... (pytest_ids: tests/unit/test_plan_subject_tooth_ignores_commit_
shas.py ...)", then the rewind budget was spent and the task parked.
pytest returns rc=1 when ANY collected test in a run fails -- not
necessarily one of the PINNED targets task.verify names. A draft that
pads its target's file with an extra, unrelated failing assertion (or
whose pinned target is already satisfied by the current tree, sitting
beside a deliberately-broken dummy elsewhere in the same file) reads as
"red demonstrated" on rc alone while the actual target is already
green. Before this fix, that bad commit landed and red_gate only
discovered the problem one full round trip and a spent rewind later.

THE FIX. workflow_step_run_pinned_suite, when expected_rc=1 and the
measured rc is genuinely 1, additionally requires at least one "FAILED
<target>" line in the combined pytest output naming one of this node's
own pinned paths -- refusing (stop_chain) otherwise, exactly like an rc
mismatch, so commit-tests-only never anchors on a false red.

subprocess.run is monkeypatched to return CONTROLLED pytest-shaped
output rather than spawning a real pytest subprocess -- this sandbox's
bare `python3` on PATH has no pytest installed (confirmed pre-existing
against tests/unit/test_run_pinned_suite_declares_expected_rc.py's own
`suite` fixture, which hits the identical "No module named pytest"
wall), so a real subprocess run cannot be exercised here. This tests
the NEW substring logic directly and deterministically instead.
"""
from __future__ import annotations

import subprocess

import pytest

from prism_service.api import workflows as wf
from prism_service.services import task_runner


def _fake_completed(stdout: str, rc: int):
    return subprocess.CompletedProcess(
        args=["python3", "-m", "pytest"], returncode=rc, stdout=stdout,
        stderr="")


@pytest.fixture
def worktree(monkeypatch, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr(wf, "_task_worktree", lambda task_id: (root, ""))
    monkeypatch.setattr(task_runner, "_scores_db_for",
                        lambda project: str(tmp_path / "scores.db"))
    return root


def _run(monkeypatch, stdout: str, rc: int, **kw) -> dict:
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw2: _fake_completed(stdout, rc))
    body = wf.RunPinnedSuiteRequest(task_id="t-1", **kw)
    return wf.workflow_step_run_pinned_suite(body, project="p")


def test_the_pinned_target_itself_failing_is_ok(monkeypatch, worktree):
    """The common, correct case: the pinned target is the thing that
    failed. Must not regress."""
    stdout = (
        "F\n=== FAILURES ===\n"
        "___ test_the_target ___\n"
        "AssertionError: not implemented yet\n"
        "=== short test summary info ===\n"
        "FAILED test_target.py::test_the_target - AssertionError: "
        "not implemented yet\n"
        "1 failed in 0.01s\n")
    out = _run(monkeypatch, stdout, 1,
              paths=["test_target.py::test_the_target"], expected_rc=1)
    assert out["outcome"] == "ok", out
    assert out["rc"] == 1, out
    assert not out.get("stop_chain"), out


def test_an_unrelated_failure_beside_a_passing_target_is_refused(
        monkeypatch, worktree):
    """THE LIVE DEFECT: rc==1 because SOMETHING in the file failed, but
    not the pinned target itself -- the target is already green."""
    stdout = (
        ".F\n=== FAILURES ===\n"
        "___ test_an_unrelated_dummy ___\n"
        "AssertionError: a padding failure, not the real target\n"
        "=== short test summary info ===\n"
        "FAILED test_target.py::test_an_unrelated_dummy - AssertionError: "
        "a padding failure, not the real target\n"
        "1 failed, 1 passed in 0.01s\n")
    out = _run(monkeypatch, stdout, 1,
              paths=["test_target.py::test_the_target"], expected_rc=1)
    assert out["rc"] == 1, out
    assert out["outcome"] == "refused", (
        f"rc==1 alone must not be enough -- the pinned target itself "
        f"never failed: {out}")
    assert out["stop_chain"] is True, out
    assert "not red demonstrated" in out["reason"], out
    assert "test_target.py::test_the_target" in out["reason"], (
        "the refusal must name which pinned target(s) did not fail: "
        f"{out['reason']!r}")


def test_a_bare_file_target_is_satisfied_by_any_failure_inside_it(
        monkeypatch, worktree):
    """A pinned target with no ::test_name (a whole-file pin) is
    satisfied by ANY test inside that file failing -- "FAILED file.py"
    is already a substring of "FAILED file.py::test_name", so no split
    logic is needed for this to work correctly."""
    stdout = (
        ".F\n=== short test summary info ===\n"
        "FAILED test_target.py::test_b - AssertionError: this one fails\n"
        "1 failed, 1 passed in 0.01s\n")
    out = _run(monkeypatch, stdout, 1, paths=["test_target.py"],
              expected_rc=1)
    assert out["outcome"] == "ok", out
    assert not out.get("stop_chain"), out


def test_multiple_pinned_targets_any_one_failing_is_enough(
        monkeypatch, worktree):
    stdout = (
        ".F\n=== short test summary info ===\n"
        "FAILED test_b.py::test_y - AssertionError: red\n"
        "1 failed, 1 passed in 0.01s\n")
    out = _run(monkeypatch, stdout, 1,
              paths=["test_a.py::test_x", "test_b.py::test_y"],
              expected_rc=1)
    assert out["outcome"] == "ok", out


def test_the_stronger_check_only_applies_when_expected_rc_is_one(
        monkeypatch, worktree):
    """No declared expectation (expected_rc=None) stays report-only,
    exactly as before -- this landing must not tighten that contract."""
    stdout = (
        ".F\n=== short test summary info ===\n"
        "FAILED test_target.py::test_an_unrelated_dummy - "
        "AssertionError: padding\n"
        "1 failed, 1 passed in 0.01s\n")
    out = _run(monkeypatch, stdout, 1,
              paths=["test_target.py::test_the_target"])
    assert out["outcome"] == "ok", out
    assert not out.get("stop_chain"), out
