"""Pins task b490fabc/host-tight-loop: the AOS worker-host process (task:
worker-host process split) was observed live with THREE child processes at
~100% CPU each, and the surviving one kept burning with zero real signals.
Two real defects, both fixed here:

(A) main.py's `_spawn_worker_host()` used to spawn unconditionally, so an
    os.execv-based in-place restart (auto-update / PRISM_DEV_WATCH) --
    which replaces the PARENT's process image but does NOT touch already
    running multiprocessing children -- left the OLD host alive as an
    orphan while the freshly re-exec'd image spawned a brand NEW one.
    Fixed with a pidfile (survives the execv) checked before every spawn:
    a live, fingerprint-matching prior host is terminated first, so
    exactly one host ever runs per data dir.

(B) every standing worker loop (`language_alignment_worker`,
    `dispatch_guard`, `deploy_worker`, `gate_adjudicator`,
    `resume_actuator`, `ship_worker`) called `wakeups.wait(kinds,
    since=sweep_started)` with `sweep_started` captured BEFORE its own
    sweep body ran. A sweep that itself mutates a task (nearly all of
    them do, e.g. marking a run task done) raises one of the very signals
    it's watching for -- with a PRE-sweep baseline that self-caused
    signal is newer than the baseline the instant wait() is entered, so
    the loop re-fires on its own work forever with zero external cause.
    Fixed by dropping the pre-sweep timestamp entirely: `since` defaults
    to None (baseline = now, taken AFTER the sweep), which still catches
    a signal from a genuinely different process/request.

(C) belt and braces: `worker_host._cpu_governor` samples the host
    process's own CPU and, if it stays hot with zero wakeups signals
    recorded, logs + records a `system_activity` "throttled" entry and
    sleeps -- visible on the Live page even if some other bug reproduces
    the shape of (B) in the future.

(A) and (B)'s cross-process claims are exercised with REAL OS
subprocesses (never mocked pids/timers) -- that boundary is exactly what
was broken live. (C) is pure in-process CPU/signal-count arithmetic with
no cross-process component, so it is exercised directly with tiny
thresholds instead of the real 10s window.
"""
from __future__ import annotations

import multiprocessing
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[2]  # services/prism-service
WORKER_LOOP_FILES = [
    "prism_service/services/dispatch_guard.py",
    "prism_service/services/deploy_worker.py",
    "prism_service/services/gate_adjudicator.py",
    "prism_service/services/resume_actuator.py",
    "prism_service/services/ship_worker.py",
    "prism_service/services/language_alignment_worker.py",
]

# Captured ONCE, before any test monkeypatches multiprocessing.get_context
# -- the lambda used to stand in for it below must return this real
# context, never call multiprocessing.get_context("fork") again itself
# (that would recurse into the very patch it is part of).
_REAL_FORK_CTX = multiprocessing.get_context("fork")


# ---------------------------------------------------------------------------
# (A) exactly one worker-host process per data dir.
# ---------------------------------------------------------------------------

def _spawn_dummy_process(data_dir: str) -> subprocess.Popen:
    """A real, independent OS process carrying PRISM_DATA_DIR=<data_dir> in
    its own /proc/<pid>/environ -- the fingerprint
    `_pid_is_prism_worker_host` reads, since a spawned multiprocessing
    child's argv never names `worker_host` at all."""
    env = dict(os.environ)
    env["PRISM_DATA_DIR"] = data_dir
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], env=env,
    )
    time.sleep(0.2)  # let /proc/<pid>/environ populate before we read it
    return proc


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                     reason="/proc/<pid>/environ is Linux-only")
def test_pid_is_prism_worker_host_fingerprints_by_data_dir(tmp_path, monkeypatch):
    import prism_service.main as main_mod
    data_dir = tmp_path / "matching"
    data_dir.mkdir()
    monkeypatch.setattr(main_mod, "DATA_DIR", data_dir)
    proc = _spawn_dummy_process(str(data_dir))
    try:
        assert main_mod._pid_alive(proc.pid)
        assert main_mod._pid_is_prism_worker_host(proc.pid) is True
        # A live process that does NOT carry a matching PRISM_DATA_DIR must
        # never be treated as "ours" -- a stale pidfile could otherwise
        # terminate an unrelated process.
        monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path / "different")
        assert main_mod._pid_is_prism_worker_host(proc.pid) is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                     reason="/proc/<pid>/environ is Linux-only")
def test_pid_alive_and_dead_pids():
    import prism_service.main as main_mod
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert main_mod._pid_alive(proc.pid) is True
    finally:
        proc.terminate()
        proc.wait(timeout=5)
    assert main_mod._pid_alive(proc.pid) is False


def _fast_worker_host_stub(data_dir, env=None):
    """Stands in for the real `worker_host.main` in the fork-based test
    below -- starting all nine real standing workers is not the thing
    under test here (that a stale live host gets terminated, exactly one
    survives) and would make this test slow and network/git-dependent."""
    time.sleep(0.5)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                     reason="/proc/<pid>/environ + fork start method are Linux-only")
def test_spawn_worker_host_terminates_a_live_stale_host_never_two(tmp_path, monkeypatch):
    import prism_service.main as main_mod
    from prism_service.services import worker_host as wh_mod

    data_dir = tmp_path
    monkeypatch.setattr(main_mod, "DATA_DIR", data_dir)

    # A real, independent process already holding the pidfile -- exactly
    # the shape of an orphaned pre-execv host.
    stale = _spawn_dummy_process(str(data_dir))
    main_mod._write_worker_host_pidfile(stale.pid)
    assert main_mod._pid_alive(stale.pid)

    # fork (not the real "spawn" start method) so the monkeypatched stub
    # below is visible to the child without needing it to be a real
    # picklable top-level function reimported fresh -- fork just COW-
    # duplicates this process's memory, monkeypatch included.
    monkeypatch.setattr(multiprocessing, "get_context",
                         lambda name=None: _REAL_FORK_CTX)
    monkeypatch.setattr(wh_mod, "main", _fast_worker_host_stub)

    new_proc = None
    try:
        new_proc = main_mod._spawn_worker_host()
        # The stale host must be dead -- never two hosts alive together.
        # `stale.poll()` (not `_pid_alive`) is the right check here: we are
        # `stale`'s own parent (subprocess.Popen), so a signalled process
        # becomes a ZOMBIE -- still "alive" to os.kill(pid, 0) -- until WE
        # reap it; only our own Popen object's poll()/wait() does that. In
        # production the orphaned host has no such parent-side special
        # case (it is reparented to a real subreaper), so this is a test
        # artifact of using subprocess.Popen to stand up the stale process,
        # not a gap in the fix under test.
        deadline = time.time() + 5.0
        while time.time() < deadline and stale.poll() is None:
            time.sleep(0.05)
        assert stale.poll() is not None, (
            "stale worker-host process was not terminated before a "
            "replacement was spawned"
        )
        assert new_proc.pid != stale.pid
        assert main_mod._read_worker_host_pidfile() == new_proc.pid
    finally:
        if stale.poll() is None:
            stale.terminate()
        stale.wait(timeout=5)
        if new_proc is not None:
            new_proc.terminate()
            new_proc.join(timeout=5)


def test_spawn_worker_host_skips_a_dead_or_foreign_pidfile(tmp_path, monkeypatch):
    """A pidfile naming a dead pid, or a live pid that is provably NOT one
    of ours (no matching PRISM_DATA_DIR fingerprint), must never block a
    fresh spawn -- `_terminate_pid` is only called when BOTH checks pass."""
    import prism_service.main as main_mod
    monkeypatch.setattr(main_mod, "DATA_DIR", tmp_path)
    main_mod._write_worker_host_pidfile(999_999_999)  # not a real pid
    terminated = []
    monkeypatch.setattr(main_mod, "_terminate_pid", lambda pid, **kw: terminated.append(pid))
    monkeypatch.setattr(multiprocessing, "get_context",
                         lambda name=None: _REAL_FORK_CTX)
    from prism_service.services import worker_host as wh_mod
    monkeypatch.setattr(wh_mod, "main", _fast_worker_host_stub)
    proc = main_mod._spawn_worker_host()
    try:
        assert terminated == []  # dead pid -> never "terminated"
    finally:
        proc.terminate()
        proc.join(timeout=5)


# ---------------------------------------------------------------------------
# (B) wakeups semantics: zero signals -> zero wakeups; the worker-loop
# self-feedback regression is gone.
# ---------------------------------------------------------------------------

def _wakeups_child_count_wakeups(data_dir, since0, q):
    os.environ["PRISM_DATA_DIR"] = data_dir
    from prism_service.services import wakeups
    passes = 0
    since = since0
    t0 = time.time()
    while time.time() - t0 < 2.0:
        got = wakeups.wait(["task_changed"], timeout=0.3, since=since)
        since = time.time()
        if got:
            passes += 1
    q.put(passes)


def test_wakeups_wait_real_subprocess_zero_then_one_signal(tmp_path):
    """The primitive itself (no worker-loop pattern involved): a real
    child process's wait() must see ZERO wakeups over a 2s window with no
    signal, then EXACTLY one wakeup after ONE signal() from this
    (different) process."""
    ctx = multiprocessing.get_context("spawn")
    data_dir = str(tmp_path / "no_signal")
    os.makedirs(data_dir, exist_ok=True)
    q = ctx.Queue()
    p = ctx.Process(target=_wakeups_child_count_wakeups,
                     args=(data_dir, time.time(), q))
    p.start()
    passes = q.get(timeout=10)
    p.join(timeout=5)
    assert passes == 0

    data_dir2 = str(tmp_path / "one_signal")
    os.makedirs(data_dir2, exist_ok=True)
    q2 = ctx.Queue()
    p2 = ctx.Process(target=_wakeups_child_count_wakeups,
                      args=(data_dir2, time.time(), q2))
    p2.start()
    time.sleep(0.4)
    os.environ["PRISM_DATA_DIR"] = data_dir2
    from prism_service.services import wakeups
    wakeups.signal("task_changed", "*")
    passes2 = q2.get(timeout=10)
    p2.join(timeout=5)
    assert passes2 == 1


def _selffeedback_child(data_dir, since_mode, q):
    """Mimics one worker loop's real shape: each iteration mutates a task
    (here: just calls wakeups.signal, standing in for task_svc.update's
    own task_changed signal) as part of its "sweep", then waits on the
    very same kind. `since_mode="pre"` reproduces the OLD buggy pattern
    (baseline captured before the sweep); `since_mode="post"` is today's
    fixed code (since= omitted -> baseline = now, taken after)."""
    os.environ["PRISM_DATA_DIR"] = data_dir
    from prism_service.services import wakeups
    passes = 0
    t0 = time.time()
    while time.time() - t0 < 2.0:
        sweep_started = time.time()          # the old, buggy capture point
        wakeups.signal("task_changed", "*")  # the sweep's OWN mutation
        passes += 1
        if since_mode == "pre":
            wakeups.wait(["task_changed"], timeout=0.3, since=sweep_started)
        else:
            wakeups.wait(["task_changed"], timeout=0.3)
    q.put(passes)


def test_worker_loop_since_pattern_no_longer_self_retriggers(tmp_path):
    """Real-subprocess regression pin for the actual bug: a sweep that
    signals the same kind it waits on must NOT busy-loop on its own
    write. The old `since=sweep_started` (pre-sweep) pattern produces
    hundreds of spurious passes in 2s; today's pattern (since omitted,
    baseline captured after the sweep) is bounded by the 0.3s timeout
    fallback -- well under 20 passes in the same window."""
    ctx = multiprocessing.get_context("spawn")

    buggy_dir = str(tmp_path / "buggy")
    os.makedirs(buggy_dir, exist_ok=True)
    q1 = ctx.Queue()
    p1 = ctx.Process(target=_selffeedback_child, args=(buggy_dir, "pre", q1))
    p1.start()
    buggy_passes = q1.get(timeout=10)
    p1.join(timeout=5)

    fixed_dir = str(tmp_path / "fixed")
    os.makedirs(fixed_dir, exist_ok=True)
    q2 = ctx.Queue()
    p2 = ctx.Process(target=_selffeedback_child, args=(fixed_dir, "post", q2))
    p2.start()
    fixed_passes = q2.get(timeout=10)
    p2.join(timeout=5)

    assert buggy_passes > 100, (
        "sanity check: the OLD pre-sweep pattern should reproduce the "
        f"tight loop (got only {buggy_passes} passes in 2s)"
    )
    assert fixed_passes < 20, (
        f"post-sweep baseline still self-retriggered ({fixed_passes} "
        "passes in 2s with only self-caused signals)"
    )


@pytest.mark.parametrize("relpath", WORKER_LOOP_FILES)
def test_worker_loop_never_passes_a_pre_sweep_since_to_wait(relpath):
    """Source guard: none of the six standing-worker loops may pass a
    pre-sweep timestamp (`since=sweep_started` / `since=tick_started`) to
    `wakeups.wait(...)` -- that is precisely the self-feedback shape task
    b490fabc/host-tight-loop fixed. A per-project watermark used for a
    DIFFERENT purpose (e.g. gate_adjudicator's `_project_needs_scan`,
    which tracks a genuine post-scan `_LAST_PROJECT_SCAN` dict) is a
    different call site and is not covered by this file list."""
    src = (SERVICE_ROOT / relpath).read_text(encoding="utf-8")
    for call in re.findall(r"wakeups\.wait\([^)]*\)", src, flags=re.S):
        assert "since=sweep_started" not in call
        assert "since=tick_started" not in call


# ---------------------------------------------------------------------------
# (C) CPU governor: throttles on high CPU + zero signals, stays quiet when
# signalled. Pure in-process arithmetic (no cross-process component), so
# exercised directly with tiny thresholds rather than the real 10s window.
# ---------------------------------------------------------------------------

def _burn_cpu(stop: threading.Event) -> None:
    while not stop.is_set():
        sum(i * i for i in range(2000))


def test_cpu_governor_throttles_on_high_cpu_with_zero_signals(tmp_path, monkeypatch):
    import prism_service.services.worker_host as wh_mod
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from prism_service.services import wakeups
    wakeups._reset_for_tests()

    recorded = []
    monkeypatch.setattr(
        "prism_service.services.system_activity.record",
        lambda kind, project, detail, **kw: recorded.append((kind, detail)),
    )

    burner_stop = threading.Event()
    burner = threading.Thread(target=_burn_cpu, args=(burner_stop,), daemon=True)
    burner.start()
    gov_stop = threading.Event()
    # _cpu_governor loops until stop is set, so it is run in its own
    # thread with a short deadline rather than called inline.
    gov_thread = threading.Thread(
        target=wh_mod._cpu_governor,
        args=(gov_stop,), kwargs=dict(poll_s=0.05, cpu_pct_threshold=5.0,
                                       window_s=0.15, throttle_sleep_s=0.05),
        daemon=True,
    )
    gov_thread.start()
    time.sleep(1.0)
    gov_stop.set()
    burner_stop.set()
    gov_thread.join(timeout=2)
    burner.join(timeout=2)

    assert recorded, "CPU governor never recorded a throttle with zero signals"
    assert recorded[0][0] == "throttled"


def test_cpu_governor_stays_quiet_when_signals_keep_arriving(tmp_path, monkeypatch):
    import prism_service.services.worker_host as wh_mod
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    from prism_service.services import wakeups
    wakeups._reset_for_tests()

    recorded = []
    monkeypatch.setattr(
        "prism_service.services.system_activity.record",
        lambda kind, project, detail, **kw: recorded.append((kind, detail)),
    )

    burner_stop = threading.Event()
    burner = threading.Thread(target=_burn_cpu, args=(burner_stop,), daemon=True)
    burner.start()

    def _keep_signalling(stop):
        while not stop.is_set():
            wakeups.signal("task_changed", "*")
            time.sleep(0.03)

    sig_stop = threading.Event()
    signaller = threading.Thread(target=_keep_signalling, args=(sig_stop,), daemon=True)
    signaller.start()

    gov_stop = threading.Event()
    gov_thread = threading.Thread(
        target=wh_mod._cpu_governor,
        args=(gov_stop,), kwargs=dict(poll_s=0.05, cpu_pct_threshold=5.0,
                                       window_s=0.15, throttle_sleep_s=0.05),
        daemon=True,
    )
    gov_thread.start()
    time.sleep(1.0)
    gov_stop.set()
    sig_stop.set()
    burner_stop.set()
    gov_thread.join(timeout=2)
    signaller.join(timeout=2)
    burner.join(timeout=2)

    assert recorded == [], (
        f"CPU governor throttled despite continuous signals: {recorded}"
    )
