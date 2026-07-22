"""The green-gate machine seat must refuse a receipt measured on a tree that
does not contain the task's own pinned tests (task e0149f1f).

Regression: task 5a6837a0 reached status=done on
`adapter=http_probe, tree=c162b66` — a commit belonging to task 89e90d1a —
because its per-task scratch worktree was never advanced to the lane's work.
The receipt tooth compared the receipt's tree to the WORKTREE's tree, so both
agreed on the wrong tree and the gate closed on evidence that never saw the
code under review.
"""
from types import SimpleNamespace

from prism_service.services.conductor_service import ConductorService


def _task(verify):
    return SimpleNamespace(id="t1", verify=verify)


def test_pinned_paths_extracted_from_bare_paths():
    assert ConductorService.pinned_test_paths(
        _task(["services/prism-service/tests/unit/test_a.py"])
    ) == ["services/prism-service/tests/unit/test_a.py"]


def test_pinned_paths_extracted_from_full_commands():
    # task.verify is sometimes a whole command, not a path.
    got = ConductorService.pinned_test_paths(
        _task(["python -m pytest services/prism-service/tests/unit/test_a.py "
               "services/prism-service/tests/integration/test_b.py"]))
    assert got == ["services/prism-service/tests/integration/test_b.py",
                   "services/prism-service/tests/unit/test_a.py"]


def test_pinned_paths_survive_windows_separators():
    got = ConductorService.pinned_test_paths(
        _task([r"services\prism-service\tests\unit\test_a.py"]))
    assert got == ["services/prism-service/tests/unit/test_a.py"]


def test_no_pinned_tests_is_not_this_tooths_business():
    svc = ConductorService.__new__(ConductorService)
    assert svc._receipt_tree_missing_reason(
        _task([]), SimpleNamespace(tree_sha="deadbeef")) is None


def test_receipt_without_a_tree_is_not_this_tooths_business():
    svc = ConductorService.__new__(ConductorService)
    assert svc._receipt_tree_missing_reason(
        _task(["services/prism-service/tests/unit/test_a.py"]),
        SimpleNamespace(tree_sha="")) is None


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo_with_two_trees(tmp_path):
    """A real repo: commit ONE has no test file, commit TWO adds the pinned
    test. Mirrors the 5a6837a0 shape — a worktree parked on an older commit
    from other work, and the lane's real commit further along."""
    import os
    r = tmp_path / "repo"
    (r / "services/prism-service/tests/unit").mkdir(parents=True)
    _git(r.parent, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    (r / "other.txt").write_text("from another task\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "foreign task commit")
    foreign = _git(r, "rev-parse", "HEAD")
    (r / "services/prism-service/tests/unit/test_pinned.py").write_text(
        "def test_x():\n    assert True\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "the lane's real work")
    return r, foreign, _git(r, "rev-parse", "HEAD")


def test_foreign_tree_is_REFUSED_with_an_actionable_reason(tmp_path, monkeypatch):
    from prism_service.services import task_workspace
    repo, foreign, _real = _repo_with_two_trees(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    svc = ConductorService.__new__(ConductorService)
    reason = svc._receipt_tree_missing_reason(
        _task(["services/prism-service/tests/unit/test_pinned.py"]),
        SimpleNamespace(tree_sha=foreign))
    assert reason, "a tree lacking the pinned test must be REFUSED"
    assert foreign[:12] in reason and "test_pinned.py" in reason
    assert "distinct actor" in reason


def test_sound_tree_still_APPROVES(tmp_path, monkeypatch):
    # The tooth must NARROW machine adjudication, not disable it.
    from prism_service.services import task_workspace
    repo, _foreign, real = _repo_with_two_trees(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    svc = ConductorService.__new__(ConductorService)
    assert svc._receipt_tree_missing_reason(
        _task(["services/prism-service/tests/unit/test_pinned.py"]),
        SimpleNamespace(tree_sha=real)) is None
