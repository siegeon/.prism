"""Pins prism_service.services.wakeups -- the in-process event bus that
lets a background worker wait() on a REAL mutation instead of polling a
fixed interval regardless of whether anything changed (owner 2026-09-13:
nine standing workers ticked forever on an idle system and the Live page's
System Activity panel read that as churn)."""

import threading
import time

from prism_service.services import wakeups


def setup_function(_fn) -> None:
    wakeups._reset_for_tests()


def test_wait_wakes_within_50ms_of_a_matching_signal() -> None:
    woke_at = {}
    started_waiting = threading.Event()

    def waiter() -> None:
        started_waiting.set()
        ok = wakeups.wait(["task_changed"], project="prism", timeout=5.0)
        woke_at["t"] = time.time()
        woke_at["ok"] = ok

    t = threading.Thread(target=waiter)
    t.start()
    assert started_waiting.wait(timeout=1.0), "waiter never started"
    time.sleep(0.02)  # let the waiter actually enter its wait()

    signalled_at = time.time()
    wakeups.signal("task_changed", "prism", task_id="t1")
    t.join(timeout=1.0)

    assert not t.is_alive(), "waiter never returned"
    assert woke_at.get("ok") is True
    latency = woke_at["t"] - signalled_at
    assert latency < 0.05, f"wait() took {latency * 1000:.1f}ms to wake"


def test_wait_times_out_when_nothing_signals() -> None:
    started = time.time()
    ok = wakeups.wait(["task_changed"], project="prism", timeout=0.05)
    elapsed = time.time() - started
    assert ok is False
    assert elapsed >= 0.05


def test_signal_before_wait_within_the_since_window_still_counts() -> None:
    # A worker passes `since` as the timestamp it last finished checking,
    # so a signal that landed WHILE it was still working (before it calls
    # wait()) must not be missed.
    since = time.time()
    time.sleep(0.01)
    wakeups.signal("task_changed", "prism")
    ok = wakeups.wait(["task_changed"], project="prism", timeout=0.5, since=since)
    assert ok is True


def test_signal_after_since_is_not_seen() -> None:
    wakeups.signal("task_changed", "prism")
    since = time.time()  # baseline AFTER the only signal
    ok = wakeups.wait(["task_changed"], project="prism", timeout=0.05, since=since)
    assert ok is False


def test_wait_ignores_a_signal_for_a_different_project() -> None:
    since = time.time()
    wakeups.signal("task_changed", "other-project")
    ok = wakeups.wait(["task_changed"], project="prism", timeout=0.05, since=since)
    assert ok is False


def test_wildcard_signal_wakes_every_project_scoped_waiter() -> None:
    since = time.time()
    wakeups.signal("shipped", "*")
    ok = wakeups.wait(["shipped"], project="prism", timeout=0.5, since=since)
    assert ok is True


def test_wait_ignores_a_signal_of_a_different_kind() -> None:
    since = time.time()
    wakeups.signal("task_changed", "prism")
    ok = wakeups.wait(["shipped"], project="prism", timeout=0.05, since=since)
    assert ok is False


def test_last_signal_at_reports_zero_when_never_signalled() -> None:
    assert wakeups.last_signal_at("task_changed", "prism") == 0.0
    wakeups.signal("task_changed", "prism")
    assert wakeups.last_signal_at("task_changed", "prism") > 0.0


# ---------------------------------------------------------------------------
# Startup warmup + serialization (owner 2026-09-13, live measurement: 6+
# standing workers all fired their first tick within the same ~20s window
# after a restart, CPU sat at 150%, and ordinary HTTP routes took 10-25s).
# ---------------------------------------------------------------------------

def test_worker_warmup_s_defaults_to_120(monkeypatch) -> None:
    monkeypatch.delenv("PRISM_WORKER_WARMUP_S", raising=False)
    assert wakeups.worker_warmup_s() == 120.0


def test_worker_warmup_s_honors_the_env_override(monkeypatch) -> None:
    monkeypatch.setenv("PRISM_WORKER_WARMUP_S", "5")
    assert wakeups.worker_warmup_s() == 5.0


def test_worker_warmup_s_falls_back_on_a_bad_value(monkeypatch) -> None:
    monkeypatch.setenv("PRISM_WORKER_WARMUP_S", "not-a-number")
    assert wakeups.worker_warmup_s() == 120.0


def test_wait_out_startup_warmup_returns_immediately_once_elapsed(monkeypatch) -> None:
    # Process "started" long ago relative to a tiny warmup window.
    monkeypatch.setattr(wakeups, "_PROCESS_START", time.time() - 3600)
    monkeypatch.setenv("PRISM_WORKER_WARMUP_S", "1")
    started = time.time()
    wakeups.wait_out_startup_warmup()
    assert time.time() - started < 0.1


def test_wait_out_startup_warmup_blocks_for_the_remaining_window(monkeypatch) -> None:
    monkeypatch.setattr(wakeups, "_PROCESS_START", time.time())
    monkeypatch.setenv("PRISM_WORKER_WARMUP_S", "0.1")
    started = time.time()
    wakeups.wait_out_startup_warmup()
    elapsed = time.time() - started
    assert elapsed >= 0.09


def test_lower_thread_priority_never_raises() -> None:
    wakeups.lower_thread_priority()  # no-op-safe even if os.nice is unsupported


def test_serial_slot_allows_only_one_holder_at_a_time() -> None:
    order = []
    entered_first = threading.Event()

    def holder() -> None:
        with wakeups.serial_slot():
            order.append("first-enter")
            entered_first.set()
            time.sleep(0.05)
            order.append("first-exit")

    t = threading.Thread(target=holder)
    t.start()
    assert entered_first.wait(timeout=1.0)

    with wakeups.serial_slot():
        order.append("second-enter")
    t.join(timeout=1.0)

    # The second acquirer must not enter until the first has fully exited.
    assert order.index("first-exit") < order.index("second-enter")
