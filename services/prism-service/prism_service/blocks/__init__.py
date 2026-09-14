"""Multiplier blocks -- typed, registered, run-counted units of seat
behaviour (owner 2026-09-13, on task b490fabc's hand-built Python
branches: "im not seeing how many tasks, and how few workflow nodes...
you have not got the hang of creating the multiplier blocks that are
pydantic").

A Block is declared ONCE (id, title, kind, owner_seat, onFailure policy)
and referenced by id from as many flows as need it -- the same way a
conductor step's route is looked up by `_attach_node_trend`
(api/workflows.py) regardless of which behaviour JSON names it. The
block's `run(ctx)` body is never a reimplementation of seat logic: it
calls the EXISTING seat function directly, so the block is a typed
wrapper around code that already works, not a fork of it.

Every call through `run_block` is recorded via the same
`_record_codified_run` the conductor's own codified sub-steps already
use (task_runner.py), so a block's node on /workflows shows "N runs ·
last" for free once a catalog entry names its id as a route -- see
`_attach_node_trend`'s route-keyed lookup.
"""
from prism_service.blocks.base import Block, BlockKind, BlockScope, OnFailure
from prism_service.blocks.registry import (
    get_block,
    list_blocks,
    register_block,
    run_block,
)

__all__ = [
    "Block",
    "BlockKind",
    "BlockScope",
    "OnFailure",
    "get_block",
    "list_blocks",
    "register_block",
    "run_block",
]

# Registers red.targets_from_acs / red.context_pack / red.prompt_compose /
# red.materialize (owner 2026-09-13/14, second landing). Imported HERE,
# after Block/register_block are already bound above, so `import
# prism_service.blocks` anywhere -- api/workflows.py, task_runner.py, a
# test -- is enough to register these four without a separate app-startup
# wire. red_blocks.py itself only imports prism_service.api.workflows
# LAZILY inside its functions, so this stays import-cycle-safe.
from prism_service.blocks import red_blocks as _red_blocks  # noqa: E402,F401
