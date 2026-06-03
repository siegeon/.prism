"""Phase 4 (epic 4fd1e6b4) — ONE wall-clock maintenance/heartbeat worker.

This folds the genuinely time-based memory duties that previously each ran on
their OWN daemon thread into a SINGLE heartbeat clock. One thread iterates
every project once per tick and runs, in a FIXED sequence, the five memory
passes — each behind its OWN independent cadence gate (a per-pass last-run
timestamp), so a pass executes only when its interval has elapsed:

  1. governance       — TTL + decay + duplicate-detect (Governance.run_cycle)
                        plus the bounded retention_sweep on the same cadence.
  2. verify_staleness — VerifyStalenessOperation (env-gated off by default).
  3. forget           — ForgetOperation (env-gated off by default).
  4. adaptive         — adaptive_policy.run_once() knob retune (default ON).
  5. quality          — scoring_service.score_merged_tasks vs git truth.

All existing env gates / cadence overrides remain honored
(PRISM_GOVERNANCE_INTERVAL, PRISM_QUALITY_INTERVAL,
PRISM_ADAPTIVE_POLICY_WORKER[_INTERVAL], PRISM_<OP>_WORKER for
verify_staleness/forget). The Brain-reindex drift timer and the event pool are
NOT folded — they are out of scope.
"""

from __future__ import annotations

import os
import sys as _sys
import threading
import time

WORKER_ID = "maintenance_clock"
WORKER_LABEL = "Memory maintenance clock"

# Ordered set of the five folded memory passes. Tests pin this exact order.
PASS_ORDER = ["governance", "verify_staleness", "forget", "adaptive", "quality"]


def _now() -> float:
    """Wall clock seam — monkeypatched in cadence tests to drive time."""
    return time.time()


# --- Phase 5 (epic 4fd1e6b4): heartbeat observability ----------------------
# The Background Activity clock row reports last sweep + next sweep. The loop
# stamps this each tick; the API reads it (None until the first tick).
_last_sweep_ts: float | None = None
_last_sweep_lock = threading.Lock()


def _stamp_sweep() -> None:
    global _last_sweep_ts
    with _last_sweep_lock:
        _last_sweep_ts = time.time()


def last_sweep_ts() -> float | None:
    """Epoch seconds of the most recent heartbeat tick, or None if the clock
    has not ticked yet."""
    with _last_sweep_lock:
        return _last_sweep_ts


def next_sweep_ts() -> float | None:
    """Projected epoch of the next tick: last sweep + the heartbeat interval.
    None until the clock has ticked once."""
    last = last_sweep_ts()
    if last is None:
        return None
    return last + heartbeat_interval_s()


def _log(msg: str) -> None:
    print(f"[{WORKER_ID}] {msg}", file=_sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Per-pass cadence gates — each pass has its OWN interval (independent gate).
# ---------------------------------------------------------------------------

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def pass_cadences() -> dict[str, int]:
    """The interval (seconds) gating each pass, honoring the existing env
    overrides. A pass runs only when ITS cadence has elapsed since it last
    ran — quality (~6h) is a far longer gate than adaptive (~1h)."""
    # verify_staleness / forget share the memory_ops per-op interval contract
    # (PRISM_<OP>_WORKER_INTERVAL, default 900s, floor 60s).
    vs_int = max(60, _int_env("PRISM_VERIFYSTALENESS_WORKER_INTERVAL", 900))
    fg_int = max(60, _int_env("PRISM_FORGET_WORKER_INTERVAL", 900))
    return {
        "governance": _int_env("PRISM_GOVERNANCE_INTERVAL", 300),
        "verify_staleness": vs_int,
        "forget": fg_int,
        "adaptive": max(60, _int_env("PRISM_ADAPTIVE_POLICY_WORKER_INTERVAL", 3600)),
        "quality": _int_env("PRISM_QUALITY_INTERVAL", 21600),
    }


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def pass_enabled() -> dict[str, bool]:
    """Whether each pass is enabled by its env gate. governance + quality run
    on their interval (quality disabled when interval<=0); verify_staleness /
    forget are OFF by default (per-op PRISM_<OP>_WORKER); adaptive default ON."""
    return {
        "governance": True,
        "verify_staleness": _env_truthy("PRISM_VERIFYSTALENESS_WORKER", False),
        "forget": _env_truthy("PRISM_FORGET_WORKER", False),
        "adaptive": _env_truthy("PRISM_ADAPTIVE_POLICY_WORKER", True),
        "quality": _int_env("PRISM_QUALITY_INTERVAL", 21600) > 0,
    }


# ---------------------------------------------------------------------------
# Per-pass bodies — each takes a project id. Stubbed in cadence tests so the
# sequence/gating is observable without real side effects.
# ---------------------------------------------------------------------------

def _run_governance(project: str) -> None:
    """TTL + decay + duplicate-detect via Governance.run_cycle, plus the
    bounded retention sweep on the governance cadence (folded from the old
    start_governance_timer)."""
    from prism_service.project_context import get_project
    try:
        get_project(project).governance.run_cycle()
    except Exception as exc:
        _log(f"governance cycle error ({project}): {exc}")
    try:
        from prism_service.services.consolidation_data import retention_sweep
        scores_db = str(get_project(project)._data_dir / "scores.db")
        rep = retention_sweep(scores_db)
        if rep.get("candidates_pruned") or rep.get("session_outcomes_pruned"):
            _log(f"[retention] {project}: pruned {rep}")
    except Exception as exc:
        _log(f"retention sweep error ({project}): {exc}")


def _run_memory_op(project: str, op_factory) -> None:
    """Shared body for the per-op memory passes (verify_staleness / forget):
    run the op once for one project via the memory_ops runner."""
    from prism_service.services.memory_ops import runner
    op = op_factory()
    try:
        for item in op.select(project):
            runner.run_one(op, item, project)
    except Exception as exc:
        _log(f"{getattr(op, 'op_type', '?')} error ({project}): {exc}")


def _run_verify_staleness(project: str) -> None:
    from prism_service.services.memory_ops.verify_staleness import (
        VerifyStalenessOperation,
    )
    _run_memory_op(project, VerifyStalenessOperation)


def _run_forget(project: str) -> None:
    from prism_service.services.memory_ops.forget import ForgetOperation
    _run_memory_op(project, ForgetOperation)


def _run_adaptive(project: str) -> None:
    """Adaptive-policy knob retune. adaptive_policy.run_once() already sweeps
    every project, so we drive it once on the FIRST project of the tick and
    no-op on the rest to keep it single-pass per tick."""
    from prism_service.services import adaptive_policy
    try:
        adaptive_policy.run_once()
    except Exception as exc:
        _log(f"adaptive retune error: {exc}")


def _run_quality(project: str) -> None:
    from prism_service.config import PROJECT_DIR
    from prism_service.project_context import get_project
    from prism_service.services.scoring_service import score_merged_tasks
    try:
        ctx = get_project(project)
        scored = score_merged_tasks(
            tasks_svc=ctx.task_svc,
            scores_db=str(ctx._data_dir / "scores.db"),
            repo_path=str(PROJECT_DIR),
        )
        if scored:
            _log(f"[quality] {project}: scored {len(scored)} merged task(s)")
    except Exception as exc:
        _log(f"quality cycle error ({project}): {exc}")


# Pass name -> body dispatcher (indirection so monkeypatch on the module-level
# _run_<name> functions is honored by run_tick).
def _pass_fn(name: str):
    return globals()[f"_run_{name}"]


# ---------------------------------------------------------------------------
# Clock state + tick — per-pass last-run timestamps drive the cadence gates.
# ---------------------------------------------------------------------------

def new_clock_state() -> dict:
    """Fresh clock state: each pass has never run (last_run=None), so every
    pass is due on the first tick. One dict is shared across ticks of the
    one heartbeat thread."""
    return {"last_run": {name: None for name in PASS_ORDER}}


def _due(name: str, state: dict, cadences: dict[str, int], now: float) -> bool:
    last = state["last_run"].get(name)
    if last is None:
        return True
    return (now - last) >= cadences[name]


def run_tick(project: str, state: dict, enabled: dict[str, bool] | None = None) -> list[str]:
    """Run one heartbeat tick for ONE project: walk PASS_ORDER in sequence and
    run each pass whose own cadence has elapsed (and which is enabled, if an
    enabled map is supplied). Returns the names that fired this tick.

    The cadence gate is independent per pass — a pass only fires when ITS
    interval has elapsed since its own last run, stamped on success."""
    cadences = pass_cadences()
    now = _now()
    fired: list[str] = []
    for name in PASS_ORDER:
        if enabled is not None and not enabled.get(name, False):
            continue
        if not _due(name, state, cadences, now):
            continue
        try:
            _pass_fn(name)(project)
        except Exception as exc:
            _log(f"pass {name} raised ({project}): {exc}")
        state["last_run"][name] = now
        fired.append(name)
    return fired


# ---------------------------------------------------------------------------
# Daemon-thread entrypoint — mirrors start_understand_drainer / start_event_pool.
# ---------------------------------------------------------------------------

def heartbeat_interval_s() -> int:
    """The loop sleep — the base tick. Defaults to the SMALLEST per-pass
    cadence so each pass fires close to its own interval, floored at 30s, and
    capped at 300s so a long quality gate never starves the short ones.
    Override with PRISM_MAINTENANCE_CLOCK_INTERVAL."""
    override = _int_env("PRISM_MAINTENANCE_CLOCK_INTERVAL", 0)
    if override > 0:
        return override
    cadences = pass_cadences()
    return max(30, min(300, min(cadences.values())))


def is_enabled() -> bool:
    """The clock thread itself is always on (it is the consolidated home of
    all the wall-clock memory duties); set PRISM_MAINTENANCE_CLOCK=off to
    disable the whole fold (e.g. in tests)."""
    return _env_truthy("PRISM_MAINTENANCE_CLOCK", default=True)


def _loop(interval_s: int, initial_delay_s: float) -> None:
    from prism_service.project_context import get_all_projects
    if initial_delay_s > 0:
        time.sleep(initial_delay_s)
    # One cadence state PER project so each project's passes gate
    # independently. The global adaptive pass is deduped per tick below.
    states: dict[str, dict] = {}
    _log(
        f"started; tick={interval_s}s; cadences={pass_cadences()}; "
        f"enabled={pass_enabled()}"
    )
    while True:
        try:
            _stamp_sweep()
            enabled = pass_enabled()
            adaptive_ran_this_tick = False
            for pid in get_all_projects():
                st = states.setdefault(pid, new_clock_state())
                # adaptive_policy.run_once() already sweeps ALL projects, so
                # let it fire on at most one project per tick.
                per_proj = dict(enabled)
                if adaptive_ran_this_tick:
                    per_proj["adaptive"] = False
                fired = run_tick(pid, st, enabled=per_proj)
                if "adaptive" in fired:
                    adaptive_ran_this_tick = True
                if fired:
                    _log(f"{pid}: ran passes {fired}")
        except Exception as exc:
            _log(f"tick error: {exc}")
        time.sleep(interval_s)


def start_maintenance_clock(
    interval_s: int | None = None, initial_delay_s: float = 0.0
) -> threading.Thread | None:
    """Spawn the single maintenance-clock daemon thread, unless disabled via
    PRISM_MAINTENANCE_CLOCK=off. Mirrors the other lifespan worker
    entrypoints (start_understand_drainer / start_event_pool)."""
    if not is_enabled():
        _log("disabled (PRISM_MAINTENANCE_CLOCK=off)")
        return None
    tick = interval_s if interval_s and interval_s > 0 else heartbeat_interval_s()
    t = threading.Thread(
        target=_loop, args=(tick, initial_delay_s),
        name="prism-maintenance-clock", daemon=True,
    )
    t.start()
    return t
