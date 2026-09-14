"""Live incident, task a65c66e5, 2026-09-14: a rewound task sat genuinely
eligible (in_progress, non-gate step, `wake()` already fired) for minutes
with `/api/system/activity` reporting NOTHING running -- indistinguishable
from task_runner being dead. Root cause: `_loop` called
`wakeups.wait_out_startup_warmup()` (up to PRISM_WORKER_WARMUP_S, default
120s) BEFORE entering the `system_activity.pass_` context every real sweep
tick uses, so a worker_host that restarts mid-warmup (a deploy landing, a
crash) re-enters this same blind window on every restart with nothing to
show for it. `_wait_out_warmup_visibly()` wraps the wait in the same
`pass_` context so a live check during warmup shows task_runner alive and
waiting, not absent.
"""
from prism_service.services import system_activity, task_runner as tr


def test_warmup_is_visible_in_system_activity(monkeypatch):
    system_activity._reset_for_tests()
    seen: dict = {}

    def _fake_warmup() -> None:
        # Captured WHILE the wait is still "running", the same moment a
        # live /api/system/activity poll would land during a real warmup.
        seen["snapshot"] = system_activity.snapshot()

    monkeypatch.setattr(
        "prism_service.services.wakeups.wait_out_startup_warmup",
        _fake_warmup)

    tr._wait_out_warmup_visibly()

    running = seen.get("snapshot", {}).get("running", [])
    matches = [e for e in running
               if e.get("kind") == "task_runner" and e.get("detail") == "warmup"]
    assert matches, (
        "task_runner's startup warmup must show up in system_activity's "
        "running list while it is in progress -- a silent warmup reads as "
        "a dead worker, not one that will sweep the instant it clears "
        f"(saw running={running!r})")

    # After warmup completes the entry must be gone from 'running' -- it
    # never masquerades as still in flight once the real sweep is about to
    # start.
    after = system_activity.snapshot()
    assert not any(e.get("kind") == "task_runner" and e.get("detail") == "warmup"
                   for e in after.get("running", [])), (
        "a completed warmup pass must not linger in the running list")
