"""Candidate hook that never returns (task 39244a32).

Loaded by ab_retrieval.py load_candidate("benchmarks.tests.ab_stall_candidate:block_forever")
and assigned to Brain._graph_search, so the block sits on the harness's OWN
call path: Harness.arm -> Harness.call -> handle_tool("brain_search") ->
Brain._graph_search. It stands in for the Jellyfin wedge. No sleep, no
timeout: an Event that nobody sets is a wait that never ends.
"""

from __future__ import annotations

import threading

_NEVER = threading.Event()


def block_forever(self, *args, **kwargs):
    """Replacement for Brain._graph_search that never returns."""
    _NEVER.wait()
