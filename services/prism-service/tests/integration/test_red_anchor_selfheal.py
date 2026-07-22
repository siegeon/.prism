"""Red-anchor self-heal — mx-6decaa (owner 2026-07-20).

A test-proof drive that commits its failing tests to the WORKING BRANCH (not
the per-task scratch worktree) used to strand red_gate forever: mint_red_evidence
stamped the anchor from the lagging worktree HEAD, landing ONE COMMIT EARLY (at a
pre-tests commit), so the machine seat ran pytest where the test file did not yet
exist -> rc=4 'no tests ran' -> never a clean red.

The fix anchors red on the committed red-tests commit itself:

  AC-1  _red_tests_commit finds the newest tests-only [task:<id8>] commit.
  AC-2  _commit_is_tests_only rejects a pre-tests (non-tests-only) commit.
  AC-3  _red_step_sha IGNORES a stale recorded red_step_sha row that points at a
        pre-tests commit and self-heals to the real red-tests commit.
"""

from __future__ import annotations

import subprocess


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def _repo_with_tests_commit(tmp_path, tid8):
    """A repo with a baseline (non-test) commit followed by a tests-only
    commit trailered [task:<tid8>]. Returns (repo, baseline_sha, tests_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: implementation scaffold")
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                              capture_output=True, text=True).stdout.strip()
    tdir = repo / "tests"
    tdir.mkdir()
    (tdir / "test_feature.py").write_text("def test_it():\n    assert False\n",
                                          encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"test(feature): red anchor [task:{tid8}]")
    tests_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                               capture_output=True, text=True).stdout.strip()
    return repo, baseline, tests_sha


def _world(tmp_path, monkeypatch):
    import prism_service.services.task_workspace as tw
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="red anchor", oracle="tests fail then pass",
                        proof_type="test")
    task_svc.update(t.id, verify=["tests/test_feature.py"],
                    workflow_step="red_gate", gate_state="pending")
    repo, baseline, tests_sha = _repo_with_tests_commit(tmp_path, t.id[:8])
    ws = {"path": str(repo), "baseline": baseline, "repo_root": str(repo)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws)
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    return cond, task_svc, t.id, repo, baseline, tests_sha


def test_red_tests_commit_finds_the_tests_commit(tmp_path, monkeypatch):
    cond, _svc, tid, repo, baseline, tests_sha = _world(tmp_path, monkeypatch)
    sha, found_repo = cond._red_tests_commit(tid)
    assert sha == tests_sha and found_repo == str(repo)


def test_commit_is_tests_only_rejects_pre_tests_commit(tmp_path, monkeypatch):
    cond, _svc, _tid, repo, baseline, tests_sha = _world(tmp_path, monkeypatch)
    assert cond._commit_is_tests_only(str(repo), tests_sha) is True
    assert cond._commit_is_tests_only(str(repo), baseline) is False


def test_red_step_sha_self_heals_a_stale_recorded_row(tmp_path, monkeypatch):
    cond, task_svc, tid, _repo, baseline, tests_sha = _world(tmp_path,
                                                             monkeypatch)
    # A mint stamped from the lagging worktree recorded the PRE-TESTS commit.
    task_svc.record_history(tid, action="red_step_sha", details=baseline,
                            actor="conductor")
    assert baseline != tests_sha
    # Self-heal: the stale row is ignored in favour of the real red-tests commit.
    assert cond._red_step_sha(tid) == tests_sha
