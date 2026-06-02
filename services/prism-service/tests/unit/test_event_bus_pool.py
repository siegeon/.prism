"""Phase 1 substrate: in-process event bus + consumer pool.

Pins the two oracle properties against the REAL bus + pool:
  (a) BACKPRESSURE — emit() returns to the caller BEFORE a deliberately
      slow registered handler completes (caller never awaits a handler).
  (b) CIRCUIT-BREAK — when the per-interval budget is exceeded the
      over-budget item is RE-QUEUED for later, NOT dropped and NOT
      inferred in real time.

Plus the supporting invariants: hard concurrency cap, the three event
types are defined, and wrap/no-op handlers are registered by default.
No real `claude -p` is shelled — handlers are no-op / test doubles.
"""

from __future__ import annotations

import threading
import time

from prism_service.services import event_pool as ep
from prism_service.services.event_pool import (
    ALL_EVENT_TYPES,
    BudgetGovernor,
    ConsumerPool,
    Event,
    EventBus,
    EventType,
)


def test_event_types_defined():
    assert EventType.SESSION_IMPORTED == "session.imported"
    assert EventType.MEMORY_WRITTEN == "memory.written"
    assert EventType.MEMORY_RECALLED_OUTCOME == "memory.recalled+outcome"
    assert set(ALL_EVENT_TYPES) == {
        "session.imported", "memory.written", "memory.recalled+outcome",
    }


def test_default_handlers_registered_and_noop():
    """A fresh process bus registers a wrap/no-op handler per event type
    (zero behavior change), not an empty registry."""
    ep._BUS = None  # force a fresh singleton
    bus = ep.get_bus()
    for et in ALL_EVENT_TYPES:
        handlers = bus.handlers_for(et)
        assert len(handlers) == 1
        # no-op returns None and changes nothing
        assert handlers[0](Event(type=et)) is None
    ep._BUS = None


def test_emit_returns_before_slow_handler_completes():
    """BACKPRESSURE: emit() enqueues and returns immediately; the caller
    never awaits the (deliberately slow) handler."""
    bus = EventBus()
    started = threading.Event()
    finished = threading.Event()

    def slow_handler(event: Event) -> None:
        started.set()
        time.sleep(0.5)
        finished.set()

    bus.register(EventType.MEMORY_WRITTEN, slow_handler)
    pool = ConsumerPool(bus, max_concurrency=1, budget=10)

    t0 = time.perf_counter()
    bus.emit(Event(type=EventType.MEMORY_WRITTEN))
    emit_elapsed = time.perf_counter() - t0

    # emit returned essentially instantly, long before the 0.5s handler.
    assert emit_elapsed < 0.1
    assert not finished.is_set()

    # Drive the dispatch on a worker thread so emit() truly never awaited.
    worker = threading.Thread(target=pool.drain_tick, daemon=True)
    worker.start()
    assert started.wait(timeout=2.0)
    # While the handler runs, emit stayed non-blocking (already returned).
    assert finished.wait(timeout=2.0)
    worker.join(timeout=2.0)


def test_over_budget_item_is_deferred_not_dropped():
    """CIRCUIT-BREAK: once the per-interval budget is spent, the next
    over-budget event is RE-QUEUED (still on the bus, attempts bumped),
    NOT dropped and NOT dispatched to a handler in real time."""
    bus = EventBus()
    dispatched: list[Event] = []
    bus.register(
        EventType.MEMORY_RECALLED_OUTCOME, lambda e: dispatched.append(e)
    )
    # budget=1 -> exactly one inference dispatch fits per interval.
    pool = ConsumerPool(bus, max_concurrency=2, budget=1)

    first = Event(type=EventType.MEMORY_RECALLED_OUTCOME)
    second = Event(type=EventType.MEMORY_RECALLED_OUTCOME)
    bus.emit(first)
    bus.emit(second)

    # Tick 1: first fits the budget and dispatches.
    assert pool.drain_tick() == 1
    # Tick 2: second is over budget -> circuit-break, no dispatch.
    assert pool.drain_tick() == 0

    # The second item was NOT dropped: still queued, attempts incremented.
    assert bus.qsize() == 1
    assert second.attempts == 1
    # And it was NOT inferred in real time — only the first reached a handler.
    assert dispatched == [first]

    # Maintenance-clock batching: a fresh interval resets the budget and
    # the deferred item then drains (deferred, not lost).
    pool.governor.reset()
    assert pool.drain_tick() == 1
    assert dispatched == [first, second]
    assert bus.qsize() == 0


def test_hard_concurrency_cap_bounds_simultaneous_dispatch():
    """The pool never runs more than `max_concurrency` handlers at once —
    the cap on simultaneous `claude -p` subprocesses."""
    bus = EventBus()
    cap = 2
    live = {"now": 0, "peak": 0}
    lock = threading.Lock()
    release = threading.Event()

    def blocking_handler(event: Event) -> None:
        with lock:
            live["now"] += 1
            live["peak"] = max(live["peak"], live["now"])
        release.wait(timeout=2.0)
        with lock:
            live["now"] -= 1

    bus.register(EventType.SESSION_IMPORTED, blocking_handler)
    pool = ConsumerPool(bus, max_concurrency=cap, budget=100)

    # Fire more events than the cap, each dispatched on its own thread.
    threads = []
    for _ in range(cap + 3):
        bus.emit(Event(type=EventType.SESSION_IMPORTED))
    for _ in range(cap + 3):
        th = threading.Thread(target=pool.drain_tick, daemon=True)
        th.start()
        threads.append(th)

    time.sleep(0.3)
    with lock:
        peak = live["peak"]
    release.set()
    for th in threads:
        th.join(timeout=2.0)

    assert peak <= cap


def test_budget_governor_charges_and_resets():
    gov = BudgetGovernor(budget=2)
    assert gov.try_charge() is True
    assert gov.try_charge() is True
    assert gov.try_charge() is False  # over budget -> breaker trips
    assert gov.spent == 2
    gov.reset()
    assert gov.spent == 0
    assert gov.try_charge() is True


def test_is_enabled_env_tristate(monkeypatch):
    monkeypatch.delenv("PRISM_EVENT_POOL_INTERVAL", raising=False)
    assert ep.is_enabled() is True  # default ON
    monkeypatch.setenv("PRISM_EVENT_POOL_INTERVAL", "0")
    assert ep.is_enabled() is False  # explicit off switch
    assert ep.start_event_pool() is None


def test_start_event_pool_returns_daemon_thread(monkeypatch):
    monkeypatch.setenv("PRISM_EVENT_POOL_INTERVAL", "60")
    ep._BUS = None
    t = ep.start_event_pool()
    assert t is not None
    assert t.daemon is True
    assert t.name == "prism-event-pool"
    ep._BUS = None
