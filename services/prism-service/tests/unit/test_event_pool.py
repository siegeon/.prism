"""Red scaffold for Phase 1 — in-process event bus + async consumer pool.

Pins the acceptance criteria for the event-driven learning pipeline
substrate (epic 4fd1e6b4, Phase 1 / task 779d8c5e). The module under
test (prism_service.services.event_pool) does NOT exist yet, so every
test here fails at import time today (red). Phases 2-3 wire real
emitters and migrate handlers; Phase 1 only builds the seam and
registers handlers as wrap/no-op.

Acceptance pinned here:
  (a) BACKPRESSURE — emit() ENQUEUES and returns immediately; the
      caller never awaits a handler. Proven by a deliberately-slow
      handler: emit() returns long before the handler completes.
  (b) CONCURRENCY CAP — the pool dispatches with a HARD cap on
      simultaneous claude -p subprocesses (claim-one-per-tick loop
      modeled on the Understand Drainer).
  (c) CIRCUIT-BREAK — a per-interval token/call budget governor
      DEFERS over-budget inference (re-queues it for the maintenance
      clock to batch later) rather than shelling claude -p in real
      time; the item is NOT dropped.
  (d) EVENT TYPES — session.imported, memory.written,
      memory.recalled+outcome are defined.
  (e) LIFECYCLE — is_enabled() env tri-state + start entrypoint that
      honors the disable env var (=0), mirroring the worker pattern.
"""

from __future__ import annotations

import threading
import time

import pytest

# Import-time failure is the FIRST red signal: this module is greenfield.
from prism_service.services import event_pool as ep


# --------------------------------------------------------------------------
# (d) Event types are defined.
# --------------------------------------------------------------------------
def test_event_types_defined():
    assert ep.SESSION_IMPORTED == "session.imported"
    assert ep.MEMORY_WRITTEN == "memory.written"
    assert ep.MEMORY_RECALLED_OUTCOME == "memory.recalled+outcome"


# --------------------------------------------------------------------------
# (a) Backpressure — emit() enqueues and returns before a slow handler runs.
# --------------------------------------------------------------------------
def test_emit_returns_before_slow_handler_completes():
    """emit() must ENQUEUE and return immediately — the caller (a
    write/recall/import path) never awaits a handler. A deliberately-slow
    handler proves emit() returns well before the handler finishes."""
    bus = ep.EventBus()

    handler_done = threading.Event()
    HANDLER_SLEEP_S = 1.0

    def slow_handler(event):
        time.sleep(HANDLER_SLEEP_S)
        handler_done.set()

    bus.register_handler(ep.MEMORY_WRITTEN, slow_handler)

    # Start a consumer so the queued event is actually dispatched.
    pool = ep.ConsumerPool(bus, max_concurrency=2)
    pool.start_draining()
    try:
        t0 = time.monotonic()
        bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"id": "m1"}))
        emit_elapsed = time.monotonic() - t0

        # emit() returned essentially instantly — it did NOT block on the
        # 1s handler. Generous bound (200ms) to absorb CI jitter.
        assert emit_elapsed < HANDLER_SLEEP_S / 2, (
            f"emit() blocked {emit_elapsed:.3f}s — it awaited the handler "
            "instead of enqueueing (no backpressure)"
        )
        # The handler runs out-of-band and eventually completes.
        assert handler_done.wait(timeout=HANDLER_SLEEP_S * 3), (
            "slow handler never ran — the queued event was not dispatched"
        )
    finally:
        pool.stop_draining()


# --------------------------------------------------------------------------
# (b) Hard concurrency cap on simultaneous handler dispatch.
# --------------------------------------------------------------------------
def test_concurrency_cap_is_hard():
    """No more than max_concurrency handlers run simultaneously, modeling
    the cap on simultaneous claude -p subprocesses."""
    bus = ep.EventBus()
    CAP = 2

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def tracking_handler(event):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.2)
        with lock:
            state["current"] -= 1

    bus.register_handler(ep.SESSION_IMPORTED, tracking_handler)
    pool = ep.ConsumerPool(bus, max_concurrency=CAP)
    pool.start_draining()
    try:
        for i in range(6):
            bus.emit(ep.Event(ep.SESSION_IMPORTED, {"id": f"s{i}"}))
        # Let the pool churn through the backlog.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if bus.queue_depth() == 0 and state["current"] == 0:
                break
            time.sleep(0.05)
        assert state["peak"] <= CAP, (
            f"peak in-flight handlers {state['peak']} exceeded cap {CAP}"
        )
        assert state["peak"] >= 1, "no handler ever ran"
    finally:
        pool.stop_draining()


# --------------------------------------------------------------------------
# (c) Circuit-break: over-budget inference is DEFERRED, not dropped.
# --------------------------------------------------------------------------
def test_over_budget_inference_is_deferred_not_dropped():
    """When the per-interval claude -p budget is exceeded, the pool must
    CIRCUIT-BREAK: re-queue the over-budget item for the maintenance clock
    to batch later, NOT shell claude -p in real time and NOT drop it."""
    bus = ep.EventBus()
    # Budget allows exactly ONE claude -p call this interval.
    pool = ep.ConsumerPool(bus, max_concurrency=2, per_interval_call_budget=1)

    inferred: list[str] = []

    def inferring_handler(event):
        # A handler that WOULD shell claude -p. The pool must only let it
        # run while budget remains; once spent, further inference defers.
        inferred.append(event.payload["id"])

    # Mark the handler as a claude -p (inference) handler so the budget
    # governor accounts for it.
    bus.register_handler(ep.MEMORY_WRITTEN, inferring_handler, inference=True)

    # Two inference events, budget of one.
    bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"id": "first"}))
    bus.emit(ep.Event(ep.MEMORY_WRITTEN, {"id": "second"}))

    deferred = pool.drain_once()

    # Exactly one inference ran in real time; the second was deferred.
    assert inferred == ["first"], (
        f"expected only the in-budget item to infer, got {inferred}"
    )
    # The over-budget item was NOT dropped — it is deferred for later
    # batching (re-queued / handed to the maintenance clock).
    assert any(getattr(d, "payload", {}).get("id") == "second" for d in deferred), (
        "over-budget item was dropped instead of deferred"
    )


# --------------------------------------------------------------------------
# (e) Lifecycle: env tri-state + disable via env var, mirroring workers.
# --------------------------------------------------------------------------
def test_is_enabled_env_tristate(monkeypatch):
    monkeypatch.delenv("PRISM_EVENT_POOL_INTERVAL", raising=False)
    # Unset -> default on (the substrate must self-start like the drainer).
    assert ep.is_enabled() is True
    monkeypatch.setenv("PRISM_EVENT_POOL_INTERVAL", "0")
    assert ep.is_enabled() is False


def test_start_event_pool_disabled_with_zero_interval(monkeypatch):
    """interval_s=0 short-circuits — no drain loop entered (mirrors
    start_understand_drainer's PRISM_UNDERSTAND_DRAIN_INTERVAL=0)."""
    calls: list[int] = []
    monkeypatch.setattr(ep, "_drain_tick", lambda *a, **k: calls.append(1))
    ep.start_event_pool(interval_s=0, initial_delay_s=0)
    assert calls == []


def test_handlers_are_registered_but_noop_this_phase():
    """Phase 1 registers handlers for every event type as wrap/no-op —
    NO behavior change vs. the current timer workers. The default
    registry must carry a handler for each defined event type, and those
    handlers must not raise when invoked with a representative event."""
    registry = ep.default_registry()
    for etype in (ep.SESSION_IMPORTED, ep.MEMORY_WRITTEN, ep.MEMORY_RECALLED_OUTCOME):
        handlers = registry.get(etype)
        assert handlers, f"no handler registered for {etype}"
        for h in handlers:
            # no-op: must accept an Event and return without side effects/raises
            h(ep.Event(etype, {"id": "x"}))
