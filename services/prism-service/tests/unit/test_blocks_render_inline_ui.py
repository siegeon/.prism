"""UI contract test for "multiplier steps before the red that are pydantic"
must be visible IN the flow (owner 2026-09-13/14, task b490fabc's lineage,
this landing: fix/blocksinline).

The PRISM SPA has NO JS test runner, so this pins the ACTUAL TSX/TS source
of the /workflows canvas -- the same convention as
test_conductor_page_animated_cleanup_ui.py. Before this landing a behaviour
step whose route dispatched straight to a registered block's own route
(write-failing-tests-loop.json's targets/pack/compose) rendered as a plain
generic step, indistinguishable from route/gather/recall/scaffold/loop --
the three typed, zero-model-call blocks ahead of the agentic reason-loop
step were invisible in the flow itself, findable only in the separate
worker_seat_blocks group.
"""
from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_GRAPH = _SRC / "live" / "workflowGraph.ts"
_DEF = _SRC / "lib" / "useWorkflowDef.ts"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_workflow_step_def_carries_block_fields():
    src = _read(_DEF)
    assert "block_id?: string | null;" in src
    assert "block_kind?: string | null;" in src
    assert "block_title?: string | null;" in src


def test_wf_node_carries_block_fields():
    src = _read(_GRAPH)
    assert "blockId?: string | null;" in src
    assert "blockKind?: string | null;" in src


def test_setdef_stamps_block_id_and_kind_from_the_step():
    src = _read(_GRAPH)
    assert "blockId: s.block_id ?? null," in src
    assert "blockKind: s.block_kind ?? null," in src


def test_a_block_step_carries_a_distinct_badge_in_its_sub_line():
    """The rendered `sub` text a block-styled step actually draws appends
    " · block" -- a distinct, literal badge a viewer reads even before
    learning the glyph vocabulary."""
    src = _read(_GRAPH)
    assert '+ (s.block_id ? " · block" : ""),' in src


def test_a_block_step_gets_a_distinct_glyph_by_kind():
    """A deterministic block draws a gear; an agentic/http block draws a
    sparkle -- distinct from the ordinary persona dot every other step
    draws (glyphFor("session", s.persona))."""
    src = _read(_GRAPH)
    assert 's.block_id ? (s.block_kind === "agentic" || s.block_kind === "http" ? "✦" : "⚙")' in src


def test_the_idle_glyph_paint_colours_a_block_by_kind():
    """drawNode's own idle-glyph fillStyle branch (the ELSE of the
    occupiedLit check) colours a block's glyph teal (deterministic) or
    orange (agentic/http) -- not the plain textLabel every ordinary idle
    step glyph gets."""
    src = _read(_GRAPH)
    assert (
        'ctx.fillStyle = n.blockId\n'
        '      ? (n.blockKind === "agentic" || n.blockKind === "http" ? PALETTE.orange : PALETTE.teal)\n'
        '      : PALETTE.textLabel;'
    ) in src


def test_workflows_page_never_hardcodes_a_second_block_route_table():
    """The client renders whatever block_id/block_kind the service sent --
    it must never carry its OWN hardcoded route->block-id table, which
    would silently drift from prism_service/blocks/*.py the next time a
    block is added or renamed."""
    from prism_service.api import workflows as wf

    assert hasattr(wf, "_block_index_by_route")
    assert hasattr(wf, "_block_slug")
    assert wf._block_slug("red.targets_from_acs") == "red-targets-from-acs"
    assert wf._block_slug("red.context_pack") == "red-context-pack"
    assert wf._block_slug("certainty.derive_oracle") == "certainty-derive-oracle"
