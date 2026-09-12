"""A restarted daemon drops its own dead leases at startup (task b490fabc,
2026-09-11).

LIVE INCIDENT: `prism-task-runner` leased a task at 06:14:59 UTC (a 30-min
lease, MAX_LEASE_S) and was killed by a daemon restart before releasing --
`released_at` stayed NULL. The FRESH process that started seconds later
inherited that row as if it were a live hold under its own seat id, and
`resume_actuator` deferred to it for the task's entire ~30-minute
remaining TTL. `task_runner.release_stale_seat_leases()` runs once at
daemon startup (main.py's lifespan, before either seat's thread starts)
and clears exactly this: any unreleased claim held by THIS process's own
seat identities (`prism-task-runner` / `prism-resume-actuator`), because
neither seat existed yet before this point in startup so neither can be
a live holder. A lease held by any OTHER holder_id -- a live external
session -- must survive untouched.
"""

from __future__ import annotations


def _claim_svc(tmp_path):
    from prism_service.services.claim_service import ClaimService

    return ClaimService(db_path=str(tmp_path / "claims.db"))


def test_startup_release_clears_both_own_seats_but_not_a_foreign_holder(
    tmp_path, monkeypatch
):
    from prism_service.services import resume_actuator as ra
    from prism_service.services import task_runner as tr

    svc = _claim_svc(tmp_path)
    svc.acquire("task-runner-owned", holder_id=tr.SEAT_ID, ttl_s=1800)
    svc.acquire("resume-owned", holder_id=ra.SEAT, ttl_s=1800)
    svc.acquire("external-session-owned", holder_id="human-session-42", ttl_s=1800)

    monkeypatch.setattr(
        "prism_service.project_context.get_all_projects", lambda: ["proj-a"])
    monkeypatch.setattr(tr, "_claim_service", lambda project: svc)

    released = tr.release_stale_seat_leases()

    assert released == 2, f"expected exactly 2 stale leases released, got {released}"
    assert svc.holder_of("task-runner-owned") is None, (
        "task_runner's own dead lease must be released at startup")
    assert svc.holder_of("resume-owned") is None, (
        "resume_actuator's own dead lease must be released at startup")
    assert svc.holder_of("external-session-owned") == "human-session-42", (
        "a foreign holder's live claim must survive a startup release "
        "untouched")


def test_startup_release_is_harmless_with_nothing_to_release(tmp_path, monkeypatch):
    from prism_service.services import task_runner as tr

    svc = _claim_svc(tmp_path)
    monkeypatch.setattr(
        "prism_service.project_context.get_all_projects", lambda: ["proj-a"])
    monkeypatch.setattr(tr, "_claim_service", lambda project: svc)

    assert tr.release_stale_seat_leases() == 0


def test_release_by_holder_only_touches_its_own_holder_id(tmp_path):
    svc = _claim_svc(tmp_path)
    svc.acquire("t1", holder_id="seat-a", ttl_s=900)
    svc.acquire("t2", holder_id="seat-a", ttl_s=900)
    svc.acquire("t3", holder_id="seat-b", ttl_s=900)

    n = svc.release_by_holder("seat-a")

    assert n == 2
    assert svc.holder_of("t1") is None
    assert svc.holder_of("t2") is None
    assert svc.holder_of("t3") == "seat-b"


def test_lifespan_calls_release_before_starting_the_seats():
    """Wired, not just built (mirrors every other seat's AC-7 pin in this
    codebase): if main.py never calls this, the fix never runs against a
    live restart."""
    from pathlib import Path

    import prism_service.main as m

    src = Path(m.__file__).read_text(encoding="utf-8")
    i_release = src.index("release_stale_seat_leases")
    i_task_runner_start = src.index("start_task_runner()")
    i_resume_start = src.index("start_resume_actuator()")

    assert "release_stale_seat_leases" in src
    assert i_release < i_task_runner_start, (
        "stale leases must be released BEFORE task_runner's own thread "
        "starts, or a fresh sweep can race the release")
    assert i_release < i_resume_start, (
        "stale leases must be released BEFORE resume_actuator's own "
        "thread starts, or a fresh sweep can race the release")
