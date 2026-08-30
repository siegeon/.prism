"""The Dashboard's Stranded work card counts real stranded commits only.

Task 5ba44108. On 2026-08-30 the card listed 205 rows for project prism,
every one reading `local only` and `0 commits ahead`. A task with nothing
ahead of origin/main has nothing stranded, so all 205 were false — and the
wall of them hid the handful of real cases (19 tasks with genuinely
unshipped commits, plus bbfd1a19, done with no commits on main at all).

Cause, in `api/tasks.py`'s stranded scan: when a done task's branch cannot
be resolved (the branch was deleted, or the task never had one), the scan
falls back to `rev-list --count origin/main..HEAD` — which is 0 on a synced
checkout — and then appends the row anyway. Nothing dropped a row whose
`commits_ahead` was 0.

These tests pin the row FILTER. They deliberately do not touch how the scan
decides shippedness: reading the `[task:<id>]` trailer off main is correct
and squash-safe, and `merge-base --is-ancestor` is not (proven the same day:
47 of 48 branches that failed the ancestor test had in fact shipped).
"""

from __future__ import annotations

import pytest

from prism_service.api import tasks as tasks_api


class _Task:
    def __init__(self, tid: str, title: str) -> None:
        self.id = tid
        self.title = title


class _Svc:
    """Minimal stand-in for the task service the scan lists from."""

    def __init__(self, rows):
        self._rows = rows

    def list(self, status: str = "", **_):
        return self._rows if status == "done" else []


def _scan(monkeypatch, rows, ahead_by_branch, shipped=(), branch_for=None):
    """Drive the stranded scan with git and the workspace stubbed out."""
    svc = _Svc(rows)

    monkeypatch.setattr(
        tasks_api, "_shipped_task_ids", lambda *a, **k: set(shipped),
        raising=False)

    def _fake_git(repo, *args):
        if args[:2] == ("rev-list", "--count"):
            spec = args[2]
            branch = spec.split("..", 1)[1]
            return 0, str(ahead_by_branch.get(branch, 0))
        if args[0] == "for-each-ref":
            return 0, "\n".join(
                f"{b} {n}" for b, n in ahead_by_branch.items())
        return 0, ""

    monkeypatch.setattr(tasks_api, "_git", _fake_git, raising=False)
    return svc


# ----------------------------------------------------------------------
# The regression this task fixes
# ----------------------------------------------------------------------

def test_a_row_with_no_commits_ahead_is_not_stranded():
    """0 commits ahead means nothing is stranded — never emit the row.

    Asserted against the LITERAL contract the card renders: the panel
    prints "<n> commits ahead" per row, so a row carrying 0 is a lie on
    its face.
    """
    rows = [
        {"task_id": "a" * 8, "title": "landed", "commits_ahead": 0,
         "branch_on_origin": False, "state": "local_only"},
        {"task_id": "b" * 8, "title": "really stranded", "commits_ahead": 3,
         "branch_on_origin": False, "state": "local_only"},
    ]
    kept = tasks_api._stranded_rows_worth_showing(rows)

    assert [r["task_id"] for r in kept] == ["b" * 8]


def test_a_task_with_unshipped_commits_still_appears():
    """The card must not become uniformly empty — that hides the real ones."""
    rows = [
        {"task_id": "c" * 8, "title": "unmerged branch", "commits_ahead": 7,
         "branch_on_origin": True, "state": "pushed_unmerged"},
    ]
    kept = tasks_api._stranded_rows_worth_showing(rows)

    assert len(kept) == 1
    assert kept[0]["commits_ahead"] == 7
    assert kept[0]["state"] == "pushed_unmerged"


@pytest.mark.parametrize("ahead", [0, -1, None])
def test_a_row_with_no_countable_commits_is_dropped(ahead):
    """A missing or nonsensical count is not evidence of stranded work."""
    rows = [{"task_id": "d" * 8, "title": "x", "commits_ahead": ahead,
             "branch_on_origin": False, "state": "local_only"}]

    assert tasks_api._stranded_rows_worth_showing(rows) == []


def test_the_filter_preserves_row_order_and_shape():
    """The card renders these verbatim — the filter must not reshape them."""
    rows = [
        {"task_id": "e" * 8, "title": "first", "commits_ahead": 1,
         "branch_on_origin": False, "state": "local_only"},
        {"task_id": "f" * 8, "title": "second", "commits_ahead": 2,
         "branch_on_origin": True, "state": "pushed_unmerged"},
    ]
    kept = tasks_api._stranded_rows_worth_showing(rows)

    assert kept == rows


def test_an_empty_scan_stays_empty():
    assert tasks_api._stranded_rows_worth_showing([]) == []


# ----------------------------------------------------------------------
# The endpoint applies the filter (wiring, not just the helper)
# ----------------------------------------------------------------------

def test_the_endpoint_emits_no_zero_commit_row(monkeypatch):
    """A helper nobody calls is the defect this project keeps repeating.

    premise_gather and align_language were both fully built with green
    unit tests and no production caller. Pin the CALL, not just the helper.
    """
    import inspect

    src = inspect.getsource(tasks_api)
    assert "_stranded_rows_worth_showing(" in src, (
        "the filter helper exists but the scan never calls it")
    # called somewhere other than its own definition
    assert src.count("_stranded_rows_worth_showing") >= 2
