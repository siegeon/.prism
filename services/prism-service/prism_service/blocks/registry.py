"""The block registry: id -> (Block metadata, wrapped callable).

`register_block` is called once at import time by each seat module that
owns a block (design_packet.py, resume_actuator.py, gate_adjudicator.py,
...). `run_block` is what a seat's call site uses INSTEAD OF calling its
own function directly -- the function itself is unchanged, still lives
in its seat module, and is still callable directly by tests; going
through `run_block` only adds the typed lookup and the recorded run.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from prism_service.blocks.base import Block

_REGISTRY: dict[str, tuple[Block, Callable[..., Any]]] = {}


def register_block(meta: Block, fn: Callable[..., Any]) -> None:
    """Register `fn` under `meta.id`. Re-registering the SAME id with the
    SAME callable is a no-op (module re-import, e.g. under pytest) --
    only a genuine id COLLISION between two different callables raises,
    so a flow can never silently reference the wrong block's body."""
    existing = _REGISTRY.get(meta.id)
    if existing is not None and existing[1] is not fn:
        raise ValueError(
            f"block id {meta.id!r} already registered to a different "
            f"callable ({existing[1]!r} vs {fn!r})")
    _REGISTRY[meta.id] = (meta, fn)


def get_block(block_id: str) -> Block:
    return _REGISTRY[block_id][0]


def list_blocks() -> list[Block]:
    return [meta for meta, _fn in _REGISTRY.values()]


def run_block(block_id: str, *, project: str, task_id: str = "",
              args: tuple = (), kwargs: dict | None = None) -> Any:
    """Call the block's wrapped function and record the run.

    Recording goes through task_runner._record_codified_run -- the SAME
    zero-token agent_runs row a conductor codified sub-step already
    writes, keyed by `route=block_id` -- so a catalog entry that later
    names this block's id as a step's route picks up real "N runs ·
    last" data via the existing `_attach_node_trend` machinery with no
    further plumbing. Recording is best-effort (swallows its own
    failures, same posture as _record_codified_run itself): a broken
    recorder must never take down the seat behaviour it is only
    observing.
    """
    kwargs = kwargs or {}
    meta, fn = _REGISTRY[block_id]  # KeyError -- fail loud on an unknown id
    run_id = str(uuid.uuid4())
    started = time.monotonic()
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 -- policy decides re-raise
        _record(project, task_id, meta, run_id, ok=False,
                summary=f"{block_id} failed: {exc}",
                duration_ms=(time.monotonic() - started) * 1000)
        if meta.on_failure == "stop":
            raise
        return None
    _record(project, task_id, meta, run_id, ok=True,
            summary=f"{block_id} ok",
            duration_ms=(time.monotonic() - started) * 1000)
    return result


def _record(project: str, task_id: str, meta: Block, run_id: str, ok: bool,
            summary: str, duration_ms: float) -> None:
    try:
        from prism_service.services.task_runner import _record_codified_run
        _record_codified_run(
            project, task_id or f"project:{project}", meta.id, run_id, ok,
            summary)
    except Exception:
        pass


def _reset_registry_for_tests() -> None:
    """Test-only: clear the registry so a test can register a throwaway
    block without leaking into other tests' assertions about the real
    ids. Never called from production code."""
    _REGISTRY.clear()
