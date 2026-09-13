"""Per-task memo helper for gate_adjudicator.sweep_once (task: adjmemo,
2026-09-13).

Measured live at 7.13.338 with ZERO in_progress tasks: sweep_once ran
136.8s, then 94s+, on EVERY cadence, over 26 tasks parked at pending gates
none of which had changed. Two costs stacked: (1) every sweep re-fetched
and reconstructed every task row in every project (946 rows) just to find
the same ~26 candidates, and (2) the existing exponential backoff still
re-ran a full adjudication (rubric scoring, oracle probes, git reads) every
time its delay expired, even though nothing about the task had moved.

This module answers ONE question cheaply: has anything a gate decision
could depend on changed since the seat last looked at this task? The
adjudication KEY is (task.updated_at, gate_state, workflow_step, workspace
HEAD sha). The first three already live on the task row a caller already
has in hand — free. The fourth catches the one thing a task row can miss:
a workspace whose tree moved WITHOUT a matching row write (an external
push, a merge landed by another process, task fdc07eb6-style). It is
cached by the workspace's own `.git/HEAD` mtime, so a settled workspace
costs one `stat()` per sweep, never a `git rev-parse` subprocess once its
HEAD stops moving.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_HEAD_CACHE: dict[str, tuple[float, str]] = {}


def workspace_head_sha(task_id: str) -> str:
    """This task's workspace HEAD sha, cached by the workspace's own
    `.git/HEAD` mtime. "" when the task has no workspace on disk (nothing
    to cache, nothing to shell out for) — never raises."""
    if not task_id:
        return ""
    try:
        from prism_service.services import task_workspace
        ws = task_workspace.workspace_path(task_id)
    except Exception:
        return ""
    if not ws:
        return ""
    try:
        mtime = (Path(ws) / ".git" / "HEAD").stat().st_mtime
    except OSError:
        return ""
    cached = _HEAD_CACHE.get(ws)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    sha = ""
    try:
        out = subprocess.run(
            ["git", "-C", ws, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            sha = out.stdout.strip()
    except Exception:
        sha = ""
    _HEAD_CACHE[ws] = (mtime, sha)
    return sha


def adjudication_key(task_id: str, updated_at: str, gate_state: str,
                      workflow_step: str) -> tuple:
    """The full key one gate decision for `task_id` depends on this sweep.
    Two calls with an identical key mean the seat has nothing new to
    decide — the caller may skip every rubric/oracle/git step entirely."""
    return (str(updated_at or ""), str(gate_state or ""),
            str(workflow_step or ""), workspace_head_sha(task_id))


def reset_for_tests() -> None:
    """Test-only: drop the HEAD-sha cache between tests that reuse paths."""
    _HEAD_CACHE.clear()
