"""Scoring service — Layer-A quantitative composite score for task outcomes.

The core :func:`composite_score` is a pure function (LL-02). The daemon
helpers in LL-04 (:func:`detect_revert`, :func:`detect_churn`,
:func:`detect_followup_fixes`, :func:`score_merged_tasks`) wrap git
subprocess calls + task graph lookups; they do I/O but are still
deterministic given the same filesystem and task DB.

Parent task: 37932f3f · LL-02 + LL-04.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


# Penalty coefficients. Kept as module constants so LL-05's CUPED layer
# can reason about expected score distributions without reimplementing
# the math.
_GATE_RETRY_PENALTY = 0.10         # per retry, capped at 5 retries
_GATE_RETRY_CAP = 5
_TESTS_RED_PENALTY = 0.30          # tests red on merge: big hit
_CHURN_PENALTY_PER_FILE = 0.05     # per file re-edited within 14d, capped at 10
_CHURN_CAP = 10
_FOLLOWUP_PENALTY_PER_TASK = 0.15  # per follow-up fix task within 14d, capped at 3
_FOLLOWUP_CAP = 3
_REVERT_FLOOR = 0.1                # hard negative — revert proves it didn't work


def composite_score(components: dict[str, Any]) -> Optional[float]:
    """Return a Layer-A quality score in [0, 1] or ``None`` if unscorable.

    Expected keys in ``components``:
      * merged_to_main (bool) — binary gate. False returns ``None``; an
        unmerged task has no outcome yet.
      * reverted_within_14d (bool) — hard negative. A revert is strong
        evidence the code didn't survive contact with the codebase.
      * gate_retry_count (int) — linear penalty per retry, capped.
      * tests_green_on_merge (bool) — red tests on merge knock a chunk
        off the score.
      * files_re_edited_within_14d (int) — churn proxy. Each file
        re-edited reduces the score linearly, capped.
      * followup_fix_tasks_within_14d (int) — the strongest "it didn't
        stick" signal. Each follow-up drops the score linearly, capped.

    Pure: no DB access, no clock, no side effects.
    """
    if not components.get("merged_to_main"):
        return None

    # Revert is a hard negative that dominates any other positive signal.
    # A reverted PR with perfect retries+tests still scores below
    # ``_REVERT_FLOOR`` — that's the whole point.
    if components.get("reverted_within_14d"):
        return _REVERT_FLOOR

    score = 1.0

    retries = min(
        int(components.get("gate_retry_count") or 0), _GATE_RETRY_CAP
    )
    score -= _GATE_RETRY_PENALTY * retries

    if not components.get("tests_green_on_merge", True):
        score -= _TESTS_RED_PENALTY

    churn = min(
        int(components.get("files_re_edited_within_14d") or 0), _CHURN_CAP
    )
    score -= _CHURN_PENALTY_PER_FILE * churn

    followups = min(
        int(components.get("followup_fix_tasks_within_14d") or 0),
        _FOLLOWUP_CAP,
    )
    score -= _FOLLOWUP_PENALTY_PER_TASK * followups

    return max(0.0, min(1.0, score))


# ======================================================================
# LL-04 — git walker + composite scorer wiring
# ======================================================================

# GitClear / DORA standard: a change is "durable" if it wasn't reverted
# or substantially rewritten within 14 days of landing.
_DURABILITY_WINDOW_DAYS = 14


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _git(repo_path: str, *args: str) -> Optional[str]:
    """Run git with captured output. Returns stdout on success,
    ``None`` on any failure — callers treat a missing/corrupt repo as
    "no data" rather than a crash."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=repo_path,
            capture_output=True, text=True, check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return out.stdout


def _files_at_sha(repo_path: str, sha: str) -> list[str]:
    """Return the list of file paths touched by a single commit."""
    raw = _git(repo_path, "show", "--pretty=format:", "--name-only", sha)
    if raw is None:
        return []
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _is_git_repo(repo_path: str) -> bool:
    return _git(repo_path, "rev-parse", "--git-dir") is not None


def detect_revert(
    repo_path: str, merge_sha: str, merged_at: datetime, now: datetime,
) -> bool:
    """Was *this specific commit* reverted within 14 days of merge?

    ``git revert`` writes a standard line into the revert commit body:
    ``This reverts commit <sha>``. Grep for that within the window.
    """
    if not _is_git_repo(repo_path):
        return False
    window_end = merged_at + timedelta(days=_DURABILITY_WINDOW_DAYS)
    # Only consider the window ending at min(now, merged_at+14d)
    until = min(now, window_end)
    if until <= merged_at:
        return False
    raw = _git(
        repo_path, "log",
        f"--since={_iso(merged_at)}",
        f"--until={_iso(until)}",
        f"--grep=^This reverts commit {merge_sha}",
        "--format=%H",
    )
    return bool(raw and raw.strip())


def detect_churn(
    repo_path: str, merge_sha: str, merged_at: datetime, now: datetime,
) -> int:
    """Count files touched by ``merge_sha`` that were re-edited in any
    commit within the 14-day durability window (exclusive of the merge
    commit itself)."""
    if not _is_git_repo(repo_path):
        return 0
    merge_files = set(_files_at_sha(repo_path, merge_sha))
    if not merge_files:
        return 0
    window_end = merged_at + timedelta(days=_DURABILITY_WINDOW_DAYS)
    until = min(now, window_end)
    if until <= merged_at:
        return 0
    raw = _git(
        repo_path, "log",
        f"--since={_iso(merged_at)}",
        f"--until={_iso(until)}",
        f"^{merge_sha}",        # exclude the merge commit itself
        "HEAD",
        "--name-only",
        "--pretty=format:",
    )
    if raw is None:
        return 0
    touched: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if line and line in merge_files:
            touched.add(line)
    return len(touched)


def detect_followup_fixes(
    task_svc,
    merged_files: list[str],
    merged_at: datetime,
    now: datetime,
    exclude_task_id: Optional[str] = None,
) -> int:
    """Count PRISM tasks created within the 14-day window whose title or
    description mentions at least one of the merged files. Follow-up
    signal: after this task landed, new work got filed against the same
    code, suggesting the solution didn't fully stick."""
    if not merged_files:
        return 0
    window_end = merged_at + timedelta(days=_DURABILITY_WINDOW_DAYS)
    until = min(now, window_end)
    if until <= merged_at:
        return 0

    merge_file_set = set(merged_files)
    count = 0
    for t in task_svc.list():
        if exclude_task_id and t.id == exclude_task_id:
            continue
        try:
            created = datetime.fromisoformat(t.created_at)
        except ValueError:
            continue
        if created <= merged_at or created > until:
            continue
        blob = f"{t.title}\n{t.description}"
        if any(mf in blob for mf in merge_file_set):
            count += 1
    return count


# ----------------------------------------------------------------------
# Orchestrator — the piece the daemon in main.py actually calls
# ----------------------------------------------------------------------


def _gather_components(
    task_id: str,
    merge_sha: str,
    merged_at: datetime,
    repo_path: str,
    task_svc,
    now: datetime,
) -> dict[str, Any]:
    merge_files = _files_at_sha(repo_path, merge_sha) if _is_git_repo(repo_path) else []
    return {
        "merged_to_main": True,
        "reverted_within_14d": detect_revert(repo_path, merge_sha, merged_at, now),
        "files_re_edited_within_14d": detect_churn(repo_path, merge_sha, merged_at, now),
        "followup_fix_tasks_within_14d": detect_followup_fixes(
            task_svc, merge_files, merged_at, now, exclude_task_id=task_id,
        ),
        # Defaults for signals git can't provide; overridden by LL-10
        # when workflow state has them.
        "gate_retry_count": 0,
        "tests_green_on_merge": True,
        "merge_files": merge_files,
    }


def score_merged_tasks(
    tasks_svc,
    scores_db: str,
    repo_path: str,
    now: Optional[datetime] = None,
) -> list[str]:
    """Score every merged task that isn't yet in ``task_quality_rollup``.

    Returns the list of task_ids that received a fresh score on this
    pass. Idempotent: rescores only new rows, never clobbers existing
    ones on the default path.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Find merged tasks not yet scored. Reach past the ORM because
    # ``tasks`` has the new merge_sha/merged_at columns that the Task
    # dataclass doesn't expose yet (LL-03 scope was embedding only).
    rows = tasks_svc._db.execute(
        "SELECT id, merge_sha, merged_at FROM tasks "
        "WHERE merge_sha IS NOT NULL AND merged_at IS NOT NULL"
    ).fetchall()
    if not rows:
        return []

    # If the repo path isn't a git working tree, there's no truth to
    # score against — skip the pass entirely. Logged by the daemon
    # wrapper in main.py so operators see why nothing moved.
    if not _is_git_repo(repo_path):
        return []

    scores_conn = sqlite3.connect(scores_db)
    try:
        already = {
            r[0] for r in scores_conn.execute(
                "SELECT task_id FROM task_quality_rollup "
                "WHERE quality_score IS NOT NULL"
            ).fetchall()
        }
        scored: list[str] = []
        for r in rows:
            if r["id"] in already:
                continue
            try:
                merged_at = datetime.fromisoformat(r["merged_at"])
            except ValueError:
                continue
            components = _gather_components(
                task_id=r["id"],
                merge_sha=r["merge_sha"],
                merged_at=merged_at,
                repo_path=repo_path,
                task_svc=tasks_svc,
                now=now,
            )
            score = composite_score(components)
            if score is None:
                continue
            scores_conn.execute(
                "INSERT INTO task_quality_rollup "
                "(task_id, quality_score, components_json, scored_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "quality_score=excluded.quality_score, "
                "components_json=excluded.components_json, "
                "scored_at=excluded.scored_at",
                (
                    r["id"], score, json.dumps(components), _iso(now),
                ),
            )
            scored.append(r["id"])
        scores_conn.commit()
        return scored
    finally:
        scores_conn.close()
