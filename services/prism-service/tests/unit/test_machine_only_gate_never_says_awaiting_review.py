"""UI + API contract: a gate a human owner is NEVER routed to must never
render "awaiting review" (task be158613 follow-on, found live 2026-08-26 —
the owner watched red_gate read "awaiting review" on their own screen via
remote assist and said "dont show the user things that are not real").

red_gate is the one gate api/workflows.py GATE_AUTHORITY never gives a
human path (unlike story_gate/plan_gate/green_gate, whose authority text
each carry "...or a human owner's own Approve..."/"...human-only..."). The
SPA's shared honest-state map (SdlcProgress.ACTIVITY_META), the tile pill
(ConductorPage.TaskTile), and the burn graph (TokenTurns) all rendered the
SAME amber "awaiting review" claim for red_gate as for a genuinely
human-owed plan_gate/green_gate — falsely implying the owner has a click
waiting on a gate that is unconditionally machine-adjudicated.

The PRISM SPA has NO JS test runner, so these are pinned by asserting the
ACTUAL source (TSX/Python), the same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent.parent  # services/prism-service
_SRC = _ROOT / "prism_service" / "web" / "src"
_WORKFLOWS_API = _ROOT / "prism_service" / "api" / "workflows.py"
_USE_WORKFLOW_DEF = _SRC / "lib" / "useWorkflowDef.ts"
_SDLC = _SRC / "components" / "conductor" / "SdlcProgress.tsx"
_CONDUCTOR_PAGE = _SRC / "pages" / "ConductorPage.tsx"
_TOKEN_TURNS = _SRC / "components" / "conductor" / "TokenTurns.tsx"


def test_red_gate_is_the_one_machine_only_gate_server_side():
    src = _WORKFLOWS_API.read_text(encoding="utf-8")
    assert "MACHINE_ONLY_GATES" in src
    assert '"red_gate"' in src.split("MACHINE_ONLY_GATES = {", 1)[1].split("}", 1)[0]
    # The response for /api/workflows carries the flag per step.
    assert '"machine_only_gate": step["id"] in MACHINE_ONLY_GATES' in src


def test_rail_step_carries_machine_only_gate_through_the_cache():
    src = _USE_WORKFLOW_DEF.read_text(encoding="utf-8")
    assert "machine_only_gate" in src.split("export type RailStep", 1)[1].split("\n", 1)[0]
    railfrom = src.split("function railFrom", 1)[1].split("\n\n", 1)[0]
    assert "machine_only_gate" in railfrom, (
        "railFrom must pass machine_only_gate through, or every consumer "
        "of useWorkflowSteps() reads it back as undefined"
    )


def test_sdlc_progress_never_labels_a_machine_only_gate_awaiting_review():
    src = _SDLC.read_text(encoding="utf-8")
    meta_block = src.split("export const ACTIVITY_META", 1)[1].split("};", 1)[0]
    assert "awaiting_gate_machine" in meta_block
    assert "review" not in meta_block.split("awaiting_gate_machine:", 1)[1].split(",", 1)[0], (
        "the machine-only-gate label must not say 'review' — no human "
        "reviewer is owed a decision"
    )
    effective_state = src.split("const effectiveState", 1)[1].split(";", 1)[0]
    assert "curStepMachineOnly" in effective_state
    assert "awaiting_gate_machine" in effective_state


def test_conductor_tile_applies_the_same_override():
    src = _CONDUCTOR_PAGE.read_text(encoding="utf-8")
    assert "useWorkflowSteps" in src
    act_state_block = src.split("const actState =", 1)[1].split(";", 1)[0]
    assert "curStepMachineOnly" in act_state_block
    assert "awaiting_gate_machine" in act_state_block
    act_label_block = src.split("const actLabel =", 1)[1].split("const actToneFinal", 1)[0]
    assert "awaiting_gate_machine" in act_label_block


def test_token_turns_burn_graph_applies_the_same_override():
    src = _TOKEN_TURNS.read_text(encoding="utf-8")
    heading_block = src.split("const heading =", 1)[1].split(";", 1)[0]
    assert "awaiting_gate_machine" in heading_block
    assert '"review"' not in heading_block.split('"awaiting_gate_machine"', 1)[1].split("\n", 1)[0]
