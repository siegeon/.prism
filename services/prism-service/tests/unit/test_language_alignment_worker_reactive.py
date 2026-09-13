"""Pins language_alignment_worker._loop's production path (stop_event=None)
as event-driven, not a fixed-clock poll (owner 2026-09-13: a reactive suite
has no timers of its own). Before this fix the production branch called
`time.sleep(interval_s)` unconditionally between ticks, so the worker kept
ticking every `interval_s` even when no task had changed. It must instead
block on `wakeups.wait(["task_changed"], ...)`: zero extra passes while
nothing signals, and exactly one extra pass per signal."""

import threading
import time

from prism_service.services import language_alignment_worker as law
from prism_service.services import wakeups


def setup_function(_fn) -> None:
    wakeups._reset_for_tests()


class _StopLoop(Exception):
    pass


def test_loop_makes_no_extra_pass_without_a_signal_and_exactly_one_per_signal(
    monkeypatch,
) -> None:
    monkeypatch.setattr(wakeups, "wait_out_startup_warmup", lambda: None)
    monkeypatch.setattr(law, "run_once_for", lambda project: {"skipped": "test"})

    tick_count = {"n": 0}

    def fake_projects_in_scope():
        tick_count["n"] += 1
        if tick_count["n"] > 2:
            raise _StopLoop()
        return ["prism"]

    monkeypatch.setattr(law, "_projects_in_scope", fake_projects_in_scope)

    t = threading.Thread(target=law._loop, args=(5.0, None), daemon=True)
    t.start()

    # Tick 1 fires immediately on start; give it time to land, then confirm
    # NO further tick happens while nothing signals (well inside the 5s
    # fallback) -- this is the "zero passes with no signal" contract.
    time.sleep(0.3)
    assert tick_count["n"] == 1, "worker ticked again without a signal"

    # One task_changed signal must produce exactly one more pass, promptly
    # (in-process wakeups.wait wakes within ~50ms, not the 5s fallback).
    wakeups.signal("task_changed", "*")
    deadline = time.time() + 1.0
    while tick_count["n"] < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert tick_count["n"] == 2, "one signal should trigger exactly one pass"

    time.sleep(0.3)
    assert tick_count["n"] == 2, "worker ticked again without a second signal"

    # End the loop deterministically: the 3rd tick raises _StopLoop, which
    # is unhandled inside _loop (raised outside its per-project try/except)
    # and ends the thread.
    wakeups.signal("task_changed", "*")
    t.join(timeout=2.0)
    assert not t.is_alive(), "loop thread never stopped"
    assert tick_count["n"] == 3
