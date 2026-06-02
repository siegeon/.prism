"""In-process event bus + concurrency-capped, budget-governed consumer pool.

Phase 1 of epic 4fd1e6b4 ("Consolidate memory workers into one
event-driven learning pipeline"). This module is the SUBSTRATE only:
it establishes the fire-and-forget seam that the write / recall /
import paths (Phase 2) and the real handlers (Phase 3) will migrate
onto. It changes NO behavior yet — handlers are registered wrap/no-op,
no emitter is wired live, and nothing shells `claude -p`.

Why this shape:
  PRISM is zero-LLM by default; the consumer pool is meant to become
  the SINGLE `claude -p` inference chokepoint, replacing the ~9
  independent timer workers whose slow cadence is what implicitly
  rate-limits token cost today (see reflection_worker.py:1-20 and
  memory_summary_worker.py:59-69 — the 60s->300s bump existed precisely
  because the old cadence ran `claude -p` near-continuously). So the
  pool carries a per-interval call/token BUDGET governor and
  CIRCUIT-BREAKS on breach: an over-budget item is RE-QUEUED for the
  maintenance clock to batch later, never dropped and never shelled in
  real time.

Lifecycle mirrors the existing worker pattern (reflection_worker.py:
62-209, understand_drainer.py:115-183): an `is_enabled()` env tri-state,
a claim-one-per-tick `_tick` loop modeled on the Understand Drainer's
`_tick_project`, a `_loop()` that try/except-wraps work + sleeps, and a
`start_event_pool()` daemon-thread entrypoint disable-able via
PRISM_EVENT_POOL_INTERVAL=0. Wired into main.py lifespan.

Disable by setting PRISM_EVENT_POOL_INTERVAL=0.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional


# --- Event types -----------------------------------------------------------
# The three events the learning pipeline migrates onto in Phases 2-3.
# Pinned as MODULE-LEVEL constants — the oracle imports ep.SESSION_IMPORTED
# etc. directly; EventType is kept as a thin namespaced alias.
SESSION_IMPORTED = "session.imported"
MEMORY_WRITTEN = "memory.written"
MEMORY_RECALLED_OUTCOME = "memory.recalled+outcome"


class EventType:
    SESSION_IMPORTED = SESSION_IMPORTED
    MEMORY_WRITTEN = MEMORY_WRITTEN
    MEMORY_RECALLED_OUTCOME = MEMORY_RECALLED_OUTCOME


ALL_EVENT_TYPES = (
    SESSION_IMPORTED,
    MEMORY_WRITTEN,
    MEMORY_RECALLED_OUTCOME,
)


@dataclass
class Event:
    """A unit of work on the bus. `attempts` tracks re-queues so the
    maintenance clock (Phase 2+) can prefer older deferrals."""

    type: str
    payload: dict = field(default_factory=dict)
    attempts: int = 0


# --- env tri-state (mirrors memory_summary_worker._env_truthy) -------------
def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def _interval_s() -> int:
    """Tick cadence; PRISM_EVENT_POOL_INTERVAL=0 disables the daemon."""
    try:
        return int(os.environ.get("PRISM_EVENT_POOL_INTERVAL", "5"))
    except ValueError:
        return 5


def _max_concurrency() -> int:
    """Hard cap on simultaneous `claude -p` subprocesses the pool runs."""
    try:
        return max(1, int(os.environ.get("PRISM_EVENT_POOL_CONCURRENCY", "2")))
    except ValueError:
        return 2


def _budget_per_interval() -> int:
    """Per-interval `claude -p` call/token budget. When exceeded the pool
    circuit-breaks and re-queues rather than inferring in real time."""
    try:
        return max(0, int(os.environ.get("PRISM_EVENT_POOL_BUDGET", "8")))
    except ValueError:
        return 8


def is_enabled() -> bool:
    """Honor PRISM_EVENT_POOL_INTERVAL=0 as the off switch; default ON.

    Surfaced so a future /api/consolidation/workers row can reflect
    reality (UI registration is Phase 5, out of scope here)."""
    return _interval_s() > 0


# --- budget governor -------------------------------------------------------
class BudgetGovernor:
    """Tracks `claude -p` spend within one interval and trips a breaker.

    Phase 1 counts a SYNTHETIC cost of 1 per dispatch (handlers are
    no-op so there is no real token read yet; the real per-call token
    read wires in Phase 2-3). `try_charge(cost)` returns True when the
    charge fits the remaining budget and False when it would breach —
    the caller then re-queues the item (circuit-break), never shelling
    inference in real time. `reset()` is called at the top of each
    interval by the pool loop."""

    def __init__(self, budget: int) -> None:
        self._budget = budget
        self._spent = 0
        self._lock = threading.Lock()

    def try_charge(self, cost: int = 1) -> bool:
        with self._lock:
            if self._spent + cost > self._budget:
                return False
            self._spent += cost
            return True

    def reset(self) -> None:
        with self._lock:
            self._spent = 0

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent


Handler = Callable[[Event], None]


# --- event bus -------------------------------------------------------------
class EventBus:
    """Fire-and-forget, in-process event bus.

    `emit(event)` ENQUEUES and returns immediately — the caller (a
    write / recall / import path) NEVER awaits a handler. Handlers are
    registered per event type and invoked later by the ConsumerPool, so
    a deliberately-slow handler cannot back-pressure the caller."""

    def __init__(self) -> None:
        self._q: "queue.Queue[Event]" = queue.Queue()
        self._handlers: dict[str, list[Handler]] = {}
        # Event types carrying at least one inference (claude -p) handler.
        # The budget governor only charges these; non-inference dispatch is
        # free and never circuit-broken.
        self._inference_types: set[str] = set()
        self._lock = threading.Lock()

    def register_handler(
        self, event_type: str, handler: Handler, inference: bool = False
    ) -> None:
        """Register a handler. `inference=True` marks it as one that shells
        `claude -p`, so the pool's budget governor accounts for it (and
        circuit-breaks when over budget)."""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)
            if inference:
                self._inference_types.add(event_type)

    def handlers_for(self, event_type: str) -> list[Handler]:
        with self._lock:
            return list(self._handlers.get(event_type, ()))

    def is_inference(self, event_type: str) -> bool:
        """Whether dispatching this event type would shell `claude -p`."""
        with self._lock:
            return event_type in self._inference_types

    def emit(self, event: Event) -> None:
        """Enqueue and return at once. Backpressure-free by contract."""
        self._q.put(event)

    def requeue(self, event: Event) -> None:
        """Defer an over-budget item for the maintenance clock to batch
        later. Increments attempts so deferrals stay observable."""
        event.attempts += 1
        self._q.put(event)

    def claim_one(self) -> Optional[Event]:
        """Claim a single pending event or None — one-per-tick, modeled
        on understand_drainer._tick_project (max_jobs=1)."""
        try:
            return self._q.get_nowait()
        except queue.Empty:
            return None

    def queue_depth(self) -> int:
        """Number of events waiting on the bus (pinned oracle name)."""
        return self._q.qsize()


# Process-wide singleton — the one bus emitters (Phase 2) attach to.
_BUS: Optional[EventBus] = None


def get_bus() -> EventBus:
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
        _register_default_handlers(_BUS)
    return _BUS


# --- wrap/no-op handlers ---------------------------------------------------
# Phase 1 registers a handler per event type but each is a NO-OP: zero
# behavior change vs. the current timer workers. Phase 3 swaps these for
# the real migrated behavior. They are wrap/no-op, not absent, so the
# dispatch path + concurrency cap + governor are all exercised today.
def _noop_handler(event: Event) -> None:  # pragma: no cover - trivial
    return None


# Event types whose handler shells `claude -p` — registered inference=True so
# the BudgetGovernor charges + circuit-breaks them. recalled+outcome is pure
# arithmetic (zero LLM) and is deliberately absent.
_INFERENCE_EVENT_TYPES = (SESSION_IMPORTED, MEMORY_WRITTEN)


def default_registry() -> dict[str, list[Handler]]:
    """The Phase-3 default handler map: the REAL migrated learning handler
    per event type (no more wrap/no-op). This is the single swap point the
    process-singleton bus registers through, so every emitter hits the
    migrated behavior."""
    from prism_service.services import event_handlers as eh

    return {
        SESSION_IMPORTED: [eh.handle_session_imported],
        MEMORY_WRITTEN: [eh.handle_memory_written],
        MEMORY_RECALLED_OUTCOME: [eh.handle_recalled_outcome],
    }


def _register_default_handlers(bus: EventBus) -> None:
    for et, handlers in default_registry().items():
        for h in handlers:
            bus.register_handler(et, h, inference=et in _INFERENCE_EVENT_TYPES)


_LOG_PREFIX = "[event_pool]"


def _log(msg: str) -> None:
    print(f"{_LOG_PREFIX} {msg}", file=sys.stderr, flush=True)


# --- consumer pool ---------------------------------------------------------
class ConsumerPool:
    """Drains the bus one-per-tick and dispatches to handlers under a HARD
    concurrency cap, gated by the per-interval budget governor.

    Modeled on understand_drainer: claim-one-per-tick (`_tick`), a
    bounded number of claims per sweep, and a daemon `_loop`. The cap is
    a Semaphore = max simultaneous `claude -p` subprocesses; the governor
    circuit-breaks an over-budget item back onto the queue (re-queue,
    never drop, never infer in real time)."""

    def __init__(
        self,
        bus: EventBus,
        *,
        max_concurrency: Optional[int] = None,
        per_interval_call_budget: Optional[int] = None,
    ) -> None:
        self._bus = bus
        cap = max_concurrency if max_concurrency is not None else _max_concurrency()
        self._sem = threading.Semaphore(cap)
        self._cap = cap
        self.governor = BudgetGovernor(
            per_interval_call_budget
            if per_interval_call_budget is not None
            else _budget_per_interval()
        )
        self._stop = threading.Event()
        self._drain_thread: Optional[threading.Thread] = None

    def _run_handlers(self, event: Event) -> None:
        """Run every handler for the event; a handler exception is logged
        but never crashes the pool."""
        for handler in self._bus.handlers_for(event.type):
            try:
                handler(event)
            except Exception as exc:  # noqa: BLE001
                _log(f"handler {event.type} raised: {exc}")

    # -- continuous (live daemon) mode -------------------------------------
    def start_draining(self) -> None:
        """Spin a background thread that claims + dispatches continuously,
        each dispatch bounded by the concurrency cap. emit() stays
        backpressure-free because handlers run here, off the caller."""
        if self._drain_thread is not None:
            return
        self._stop.clear()
        self._drain_thread = threading.Thread(
            target=self._drain_loop, name="prism-event-pool-drain", daemon=True
        )
        self._drain_thread.start()

    def stop_draining(self) -> None:
        self._stop.set()
        t = self._drain_thread
        if t is not None:
            t.join(timeout=2.0)
        self._drain_thread = None

    def _drain_loop(self) -> None:
        while not self._stop.is_set():
            if not self._sem.acquire(timeout=0.05):
                continue
            event = self._bus.claim_one()
            if event is None:
                # Queue drained — refresh the budget so the next burst gets a
                # full allowance, then idle briefly.
                self.governor.reset()
                self._sem.release()
                time.sleep(0.01)
                continue
            if self._bus.is_inference(event.type) and not self.governor.try_charge():
                # CIRCUIT-BREAK: defer for the maintenance clock to batch.
                self._bus.requeue(event)
                self._sem.release()
                time.sleep(0.01)
                continue

            def _work(ev: Event = event) -> None:
                try:
                    self._run_handlers(ev)
                finally:
                    self._sem.release()

            threading.Thread(
                target=_work, name="prism-event-dispatch", daemon=True
            ).start()

    # -- one-shot (maintenance clock) mode ---------------------------------
    def drain_once(self) -> list[Event]:
        """Synchronously drain the events queued right now. Returns the list
        of items CIRCUIT-BROKEN (deferred) because the per-interval budget
        was exhausted — deferred items are re-queued, never dropped."""
        deferred: list[Event] = []
        for _ in range(self._bus.queue_depth()):
            event = self._bus.claim_one()
            if event is None:
                break
            if self._bus.is_inference(event.type) and not self.governor.try_charge():
                self._bus.requeue(event)
                deferred.append(event)
                continue
            self._run_handlers(event)
        return deferred


# --- daemon lifecycle ------------------------------------------------------
def _drain_tick(pool: ConsumerPool) -> list[Event]:
    """One maintenance-clock tick: reset the per-interval budget, then
    synchronously drain the queue. Budget resets at the TOP of each tick so
    deferred items get a fresh allowance — that's the batching. Module-level
    so the lifecycle test can monkeypatch it."""
    pool.governor.reset()
    return pool.drain_once()


def _loop(pool: ConsumerPool, interval: int, initial_delay: int = 0) -> None:
    if initial_delay > 0:
        time.sleep(initial_delay)
    _log(f"started; interval={interval}s cap={pool._cap}")
    while True:
        try:
            _drain_tick(pool)
        except Exception as exc:  # noqa: BLE001
            _log(f"sweep failed: {exc}")
        time.sleep(interval)


def start_event_pool(
    interval_s: Optional[int] = None, initial_delay_s: int = 0
) -> Optional[threading.Thread]:
    """Daemon-thread entrypoint, mirroring start_understand_drainer.

    `interval_s` defaults to PRISM_EVENT_POOL_INTERVAL (env); pass 0 to
    disable (short-circuits before any drain). `initial_delay_s` staggers
    startup like the other workers. Registered in main.py lifespan."""
    interval = _interval_s() if interval_s is None else interval_s
    if interval <= 0:
        _log("disabled (PRISM_EVENT_POOL_INTERVAL=0)")
        return None
    pool = ConsumerPool(get_bus())
    t = threading.Thread(
        target=_loop, args=(pool, interval, initial_delay_s),
        name="prism-event-pool", daemon=True,
    )
    t.start()
    return t
