"""LL-04 tests — start_quality_timer daemon + git walker + composite scorer wiring.

Parent task: 37932f3f · Sub-task LL-04.

These tests stand up a real tmp git repo via subprocess so the revert
detector, churn detector, and follow-up detector exercise the same git
plumbing the daemon will use in production.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ----------------------------------------------------------------------
# git fixture — tiny helper that makes timestamped commits
# ----------------------------------------------------------------------


class _GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self._run("init", "-q")
        self._run("config", "user.email", "t@t")
        self._run("config", "user.name", "t")
        self._run("config", "commit.gpgsign", "false")
        # Initial empty commit so HEAD exists.
        self._run("commit", "--allow-empty", "-m", "root",
                  env_time=datetime(2026, 1, 1, tzinfo=timezone.utc))

    def _run(self, *args, env_time: datetime | None = None) -> str:
        env = os.environ.copy()
        if env_time is not None:
            iso = env_time.isoformat()
            env["GIT_AUTHOR_DATE"] = iso
            env["GIT_COMMITTER_DATE"] = iso
        out = subprocess.run(
            ["git", *args], cwd=str(self.path), env=env,
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip()

    def commit(
        self, files: dict[str, str], msg: str, when: datetime,
    ) -> str:
        for rel, content in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            self._run("add", rel)
        self._run("commit", "-m", msg, env_time=when)
        return self._run("rev-parse", "HEAD")

    def revert(self, sha: str, when: datetime) -> str:
        self._run("revert", "--no-edit", sha, env_time=when)
        return self._run("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path) -> _GitRepo:
    return _GitRepo(tmp_path / "repo")


# ----------------------------------------------------------------------
# Revert detection
# ----------------------------------------------------------------------


def test_revert_detection(repo):
    """Revert committed within 14d of merge flips the flag."""
    from app.services.scoring_service import detect_revert

    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merge_sha = repo.commit(
        {"src/a.py": "x = 1\n"}, "feat: add x", merged_at
    )
    repo.revert(merge_sha, merged_at + timedelta(days=5))

    now = merged_at + timedelta(days=10)
    assert detect_revert(repo.path, merge_sha, merged_at, now) is True


def test_revert_outside_window_ignored(repo):
    """A revert at t=15d is past the 14-day window — flag stays False."""
    from app.services.scoring_service import detect_revert

    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merge_sha = repo.commit(
        {"src/a.py": "x = 1\n"}, "feat: add x", merged_at
    )
    repo.revert(merge_sha, merged_at + timedelta(days=15))

    now = merged_at + timedelta(days=20)
    assert detect_revert(repo.path, merge_sha, merged_at, now) is False


# ----------------------------------------------------------------------
# Churn detection
# ----------------------------------------------------------------------


def test_churn_detection(repo):
    """Files touched by the merge get re-edited within 14d → churn>0."""
    from app.services.scoring_service import detect_churn

    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merge_sha = repo.commit(
        {"src/a.py": "x = 1\n", "src/b.py": "y = 1\n"},
        "feat: add ab", merged_at,
    )
    # Re-edit a.py at day 7 (within 14d), b.py never re-touched
    repo.commit(
        {"src/a.py": "x = 2\n"}, "chore: tweak a",
        merged_at + timedelta(days=7),
    )
    now = merged_at + timedelta(days=10)
    churned = detect_churn(repo.path, merge_sha, merged_at, now)
    assert churned == 1


def test_churn_outside_window_ignored(repo):
    """Re-edit at day 15 — outside window — doesn't count."""
    from app.services.scoring_service import detect_churn

    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merge_sha = repo.commit(
        {"src/a.py": "x = 1\n"}, "feat: add a", merged_at
    )
    repo.commit(
        {"src/a.py": "x = 2\n"}, "chore: much later",
        merged_at + timedelta(days=15),
    )
    now = merged_at + timedelta(days=20)
    assert detect_churn(repo.path, merge_sha, merged_at, now) == 0


# ----------------------------------------------------------------------
# Follow-up task detection
# ----------------------------------------------------------------------


def test_followup_task_detection(tmp_path):
    """A task created within 14d of the merge that names one of the
    merged files counts as a follow-up fix."""
    from app.services.scoring_service import detect_followup_fixes
    from app.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))
    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merged_files = ["src/a.py"]

    # Create a follow-up task AFTER the merge, mentioning the file.
    fu = svc.create(
        title="Fix regression in src/a.py",
        description="The change broke the nightly run; investigate.",
    )
    # Patch created_at to a post-merge time.
    svc._db.execute(
        "UPDATE tasks SET created_at=? WHERE id=?",
        ((merged_at + timedelta(days=3)).isoformat(), fu.id),
    )
    svc._db.commit()

    now = merged_at + timedelta(days=10)
    count = detect_followup_fixes(svc, merged_files, merged_at, now,
                                  exclude_task_id=None)
    assert count == 1


def test_followup_requires_file_overlap(tmp_path):
    """A task that doesn't mention any merged file is NOT a follow-up."""
    from app.services.scoring_service import detect_followup_fixes
    from app.services.task_service import TaskService

    svc = TaskService(str(tmp_path / "tasks.db"))
    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merged_files = ["src/a.py"]

    fu = svc.create(
        title="Add dashboard analytics",
        description="New widget in ui/dashboard.py — unrelated.",
    )
    svc._db.execute(
        "UPDATE tasks SET created_at=? WHERE id=?",
        ((merged_at + timedelta(days=3)).isoformat(), fu.id),
    )
    svc._db.commit()

    now = merged_at + timedelta(days=10)
    assert detect_followup_fixes(svc, merged_files, merged_at, now,
                                 exclude_task_id=None) == 0


# ----------------------------------------------------------------------
# End-to-end scoring + timer
# ----------------------------------------------------------------------


def _mk_brain_scores(tmp_path):
    """Init Brain so scores.db has the LL-01 schema."""
    from app.engines.brain_engine import Brain

    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )


def test_end_to_end_scoring(tmp_path, repo):
    """Seed a merged task, run the scorer once, verify rollup populated."""
    from app.services.scoring_service import score_merged_tasks
    from app.services.task_service import TaskService

    _mk_brain_scores(tmp_path)
    tasks_svc = TaskService(str(tmp_path / "tasks.db"))

    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merge_sha = repo.commit(
        {"src/main.py": "print(1)\n"}, "feat: hello", merged_at
    )
    t = tasks_svc.create(title="Add greeter",
                        description="Adds print(1) in src/main.py.")
    tasks_svc._db.execute(
        "UPDATE tasks SET merge_sha=?, merged_at=? WHERE id=?",
        (merge_sha, merged_at.isoformat(), t.id),
    )
    tasks_svc._db.commit()

    now = merged_at + timedelta(days=10)
    scored = score_merged_tasks(
        tasks_svc=tasks_svc,
        scores_db=str(tmp_path / "scores.db"),
        repo_path=str(repo.path),
        now=now,
    )
    assert t.id in scored

    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "scores.db"))
    row = conn.execute(
        "SELECT quality_score, components_json FROM task_quality_rollup "
        "WHERE task_id=?",
        (t.id,),
    ).fetchone()
    conn.close()
    assert row is not None
    # No retries, green tests implied, no churn, no followups → ~1.0
    assert row[0] is not None and row[0] >= 0.9


def test_timer_idempotent(tmp_path, repo):
    """Second pass skips tasks already in rollup."""
    from app.services.scoring_service import score_merged_tasks
    from app.services.task_service import TaskService

    _mk_brain_scores(tmp_path)
    tasks_svc = TaskService(str(tmp_path / "tasks.db"))

    merged_at = datetime(2026, 4, 1, tzinfo=timezone.utc)
    merge_sha = repo.commit(
        {"src/a.py": "x = 1\n"}, "feat: a", merged_at
    )
    t = tasks_svc.create(title="Add a", description="src/a.py")
    tasks_svc._db.execute(
        "UPDATE tasks SET merge_sha=?, merged_at=? WHERE id=?",
        (merge_sha, merged_at.isoformat(), t.id),
    )
    tasks_svc._db.commit()

    now = merged_at + timedelta(days=10)
    first = score_merged_tasks(
        tasks_svc=tasks_svc,
        scores_db=str(tmp_path / "scores.db"),
        repo_path=str(repo.path),
        now=now,
    )
    second = score_merged_tasks(
        tasks_svc=tasks_svc,
        scores_db=str(tmp_path / "scores.db"),
        repo_path=str(repo.path),
        now=now,
    )
    assert t.id in first
    assert t.id not in second, (
        "already-scored task must not be re-scored on the second pass"
    )


def test_timer_skips_unmerged_tasks(tmp_path, repo):
    """Tasks without a merge_sha aren't scored."""
    from app.services.scoring_service import score_merged_tasks
    from app.services.task_service import TaskService

    _mk_brain_scores(tmp_path)
    tasks_svc = TaskService(str(tmp_path / "tasks.db"))
    t = tasks_svc.create(title="Draft", description="unmerged")

    now = datetime(2026, 4, 15, tzinfo=timezone.utc)
    scored = score_merged_tasks(
        tasks_svc=tasks_svc,
        scores_db=str(tmp_path / "scores.db"),
        repo_path=str(repo.path),
        now=now,
    )
    assert t.id not in scored


def test_timer_handles_missing_git_repo(tmp_path):
    """A configured repo path that doesn't exist doesn't crash the timer."""
    from app.services.scoring_service import score_merged_tasks
    from app.services.task_service import TaskService

    _mk_brain_scores(tmp_path)
    tasks_svc = TaskService(str(tmp_path / "tasks.db"))
    t = tasks_svc.create(title="Add x", description="src/x.py")
    tasks_svc._db.execute(
        "UPDATE tasks SET merge_sha=?, merged_at=? WHERE id=?",
        ("deadbeef", "2026-04-01T00:00:00+00:00", t.id),
    )
    tasks_svc._db.commit()

    # Point at a path that is *not* a git repo
    not_a_repo = str(tmp_path / "nope")
    (tmp_path / "nope").mkdir()

    # Must not raise
    scored = score_merged_tasks(
        tasks_svc=tasks_svc,
        scores_db=str(tmp_path / "scores.db"),
        repo_path=not_a_repo,
        now=datetime(2026, 4, 15, tzinfo=timezone.utc),
    )
    # Task wasn't scored (no git data to score from), but no crash
    assert t.id not in scored
