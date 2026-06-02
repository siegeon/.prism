"""Red scaffold (integration) — the event pool must be WIRED into the
daemon lifecycle, not defined as dead code.

The exact false-green this guards against: a service module that imports
and a method that runs in a unit test, but no daemon ever spins it, so
the substrate never actually drains in production. Phase 1's acceptance
explicitly requires the pool be "startable as a daemon-thread entrypoint
... and wired into main.py lifespan", mirroring start_understand_drainer
(main.py:321-322).

So we assert the real seam: main.py's lifespan startup block imports
start_event_pool from prism_service.services.event_pool and spins it the
same way the other workers are spun. We assert against the SOURCE of the
lifespan so the test fails loudly if the wiring is removed, independent
of import side-effects.
"""

from __future__ import annotations

import inspect
import re

from prism_service.services import event_pool as ep


def _lifespan_source() -> str:
    import prism_service.main as main_mod

    # The startup wiring lives in the module's lifespan/startup path.
    # Read the whole module source so we catch the import + spawn lines
    # wherever they sit in the lifespan block.
    return inspect.getsource(main_mod)


def test_start_event_pool_entrypoint_exists():
    """The daemon entrypoint exists with the worker-lifecycle shape."""
    assert hasattr(ep, "start_event_pool"), "no start_event_pool entrypoint"
    sig = inspect.signature(ep.start_event_pool)
    # interval + initial-delay knobs like start_understand_drainer.
    assert "interval_s" in sig.parameters
    assert "initial_delay_s" in sig.parameters


def test_main_lifespan_wires_event_pool():
    """main.py's startup must import start_event_pool and spawn it as a
    daemon thread, mirroring the understand_drainer wiring."""
    src = _lifespan_source()
    assert "start_event_pool" in src, (
        "main.py does not reference start_event_pool — the pool is not "
        "wired into the daemon lifecycle (dead code)"
    )
    assert re.search(r"event_pool\s+import\s+start_event_pool", src), (
        "main.py does not import start_event_pool from "
        "prism_service.services.event_pool"
    )
    # Spun as a daemon thread, like the other workers.
    assert re.search(
        r"Thread\(\s*target=start_event_pool[^)]*daemon=True", src, re.S
    ) or "start_event_pool()" in src, (
        "start_event_pool is imported but never spawned in the lifespan"
    )
