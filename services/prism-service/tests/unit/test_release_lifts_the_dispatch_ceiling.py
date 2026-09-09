""""Parked for a person" must mean a person can unpark it - task 1bcb2b24.

_park_looping writes "Parked for a person", and resume_actuator.release
calls itself the human's "the cause is fixed, try again" signal. But
release cleared only the per-pass attempt budget, while the ceiling read
EVERY dispatch ever recorded. So the next sweep hit the same total, and
re-parked. A task that reached the ceiling was unreachable for ever, by
anyone, however thoroughly a person fixed the cause.

Observed on task 1bcb2b24 on 2026-09-08: 12 dispatches spent while two
real defects blocked the drive (2433fa8a, 21043e38). Both shipped, and the
task still could not be re-driven.

  AC-1  the ceiling counts dispatches AFTER the last human release.
  AC-2  with no release on file the count is unchanged (the backstop).
  AC-3  only a release row moves the start - a park row does not.
  AC-4  the last release wins when several are on file.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from prism_service.services import resume_actuator as ra


def _row(action):
    return SimpleNamespace(action=action)


@pytest.fixture()
def rows(monkeypatch):
    """Patch the history the counter reads, with no project store."""
    store = {"rows": []}

    class _Svc:
        def history(self, task_id):
            return list(store["rows"])

    monkeypatch.setattr(
        "prism_service.project_context.get_project",
        lambda project: SimpleNamespace(task_svc=_Svc()))
    return store


def test_the_backstop_still_counts_without_a_release(rows):
    """AC-2: nothing changes for an oscillating task nobody released. The
    ceiling exists for exactly that case and must keep firing."""
    rows["rows"] = [_row(ra.DISPATCH_ACTION) for _ in range(12)]
    assert ra._total_dispatches("prism", "t1") == 12
    assert 12 >= ra._max_total_dispatches()


def test_a_release_restarts_the_count(rows):
    """AC-1: the whole point. After a person releases, the ceiling must
    measure the NEW attempt, not the exhausted history behind it."""
    rows["rows"] = ([_row(ra.DISPATCH_ACTION) for _ in range(12)]
                    + [_row(ra.RELEASED_ACTION)]
                    + [_row(ra.DISPATCH_ACTION)])
    assert ra._total_dispatches("prism", "t1") == 1, (
        "a released task must be drivable again")
    assert ra._total_dispatches("prism", "t1") < ra._max_total_dispatches()


def test_a_park_row_does_not_restart_the_count(rows):
    """AC-3 (the likely_misfire guard): only a HUMAN release lifts the
    backstop. The seat's own park row must not, or an oscillating task
    would reset itself every time it parked."""
    rows["rows"] = ([_row(ra.DISPATCH_ACTION) for _ in range(12)]
                    + [_row(ra.PARKED_ACTION)]
                    + [_row(ra.DISPATCH_ACTION)])
    assert ra._total_dispatches("prism", "t1") == 13


def test_the_last_release_wins(rows):
    """AC-4: a task released twice counts from the SECOND release."""
    rows["rows"] = ([_row(ra.DISPATCH_ACTION) for _ in range(5)]
                    + [_row(ra.RELEASED_ACTION)]
                    + [_row(ra.DISPATCH_ACTION) for _ in range(4)]
                    + [_row(ra.RELEASED_ACTION)]
                    + [_row(ra.DISPATCH_ACTION) for _ in range(2)])
    assert ra._total_dispatches("prism", "t1") == 2


def test_the_park_message_promises_what_release_delivers():
    """The park says "Parked for a person". That sentence is only true if
    release actually returns the task to the drive."""
    import inspect
    src = inspect.getsource(ra._park_looping)
    assert "Parked for a person" in src
    assert "RELEASED_ACTION" in inspect.getsource(ra._total_dispatches), (
        "the ceiling must read the release marker, or the promise is empty")
