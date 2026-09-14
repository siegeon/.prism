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

# REGISTERS EVERY BLOCK THIS REPO DECLARES. Live defect (owner
# 2026-09-13/14, task b490fabc): GET /api/workflows reported block_count
# 7, not 12 -- 5 blocks (certainty.derive_oracle, resume.clear_stale_
# park [it DID show, from a worker-host run row, but was still absent
# from the count before this fix in the API process itself],
# adjudicator.drain, adjudicator.unconditional_first_sweep,
# adjudicator.fair_cursor, adjudicator.inconclusive_rewind_backoff,
# red.rewind_on_exhausted_budget) live in seat modules (design_packet,
# resume_actuator, gate_adjudicator) that each call register_block at
# THEIR OWN import time -- so registration only happens if something
# else in that process already imports them for an unrelated reason.
# The worker-host process does (task_runner drives them); the API
# process serving /api/workflows does not necessarily.
#
# FIX: the DEPENDENCY RUNS THIS DIRECTION ONLY -- blocks/__init__.py
# imports every seat module that declares a block, never the reverse
# for registration purposes. `import prism_service.blocks` anywhere (the
# API process's own workflows.py included) is now enough on its own to
# register everything, in ANY process, with no separate app-startup wire
# and no dependency on what else that process happens to import. Each
# import below is safe against the circular Block/register_block import
# those seat modules make back INTO this package, because Block/
# register_block/run_block are already bound above by the time these
# lines run (the same pattern red_blocks already used, extended to
# every block-declaring module). Every seat module's own references to
# prism_service.api.workflows are LAZY (inside functions), confirmed
# before adding these -- so none of this drags api/workflows.py's own
# (much heavier) import chain in at blocks-import time.
from prism_service.services import deploy_worker as _deploy_worker  # noqa: E402,F401
from prism_service.services import design_packet as _design_packet  # noqa: E402,F401
from prism_service.services import gate_adjudicator as _gate_adjudicator  # noqa: E402,F401
from prism_service.services import resume_actuator as _resume_actuator  # noqa: E402,F401
from prism_service.services import task_workspace as _task_workspace  # noqa: E402,F401
from prism_service.blocks import red_blocks as _red_blocks  # noqa: E402,F401
