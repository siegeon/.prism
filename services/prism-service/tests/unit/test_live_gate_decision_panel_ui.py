"""UI contract tests for the /live gate decision panel (task d56f3b25, S3 of
the conductor-into-live migration, mx-e5c400): a card parked at plan_gate or
green_gate gets a THIRD action-strip button that opens a DOM decision panel
over the canvas (URL stays /live) with a working Approve/Reject that POSTs
the SAME /api/conductor/gate endpoint TaskDetailPage.gateDecide already
calls -- never a forked endpoint.

The PRISM SPA has NO JS test runner, so these pin the ACTUAL web source
(TSX/TS) -- same convention as test_live_graph_visual_grammar_ui.py: assert
on real exported function names / structure / rendered strings, never on
comments. Source-reading assertions match the RENDERED TAG / literal call
site, not the bare name, and parse the enclosing function body rather than
a fixed character window.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_LIVE = _SRC / "live"
_LIVE_PAGE = _SRC / "pages" / "LivePage.tsx"
_LIVE_GATE_PANEL = _SRC / "components" / "live" / "LiveGatePanel.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# FR-1: cards.ts -- the strip widens to a 3rd "gate" button, resolved by a
# 3-slot hit test, guarded by ONE shared owner-gate predicate so draw and
# hit-test can never disagree about whether the button exists.
# ---------------------------------------------------------------------------

def test_action_strip_hit_widens_to_include_a_gate_target():
    src = _read(_LIVE / "cards.ts")
    assert 'export type ActionStripHit = "open" | "explore" | "gate" | null;' in src, (
        "FR-1: ActionStripHit must widen to a 3rd gate target")


def test_shared_owner_gate_predicate_guards_draw_and_hit_test():
    src = _read(_LIVE / "cards.ts")
    assert "export function isGateStripEligible(" in src, (
        "one shared predicate must decide whether a card's strip carries "
        "a 3rd gate button -- so drawActionStrip and actionStripHitTest "
        "consult the SAME function, never two hand-written copies that "
        "can drift apart")
    pred_body = src.split("export function isGateStripEligible(")[1][:400]
    assert 'gate_state === "pending"' in pred_body, (
        "the predicate must key off the card's live gate_state")
    assert '"plan_gate"' in pred_body and '"green_gate"' in pred_body, (
        "the predicate must admit exactly plan_gate and green_gate")

    draw_fn = src.split("export function drawActionStrip(")[1]
    if "export function actionStripHitTest(" in draw_fn:
        draw_fn = draw_fn[: draw_fn.index("export function actionStripHitTest(")]
    assert "isGateStripEligible(n)" in draw_fn, (
        "drawActionStrip must consult the shared predicate, not a "
        "redundant inline gate_state/workflow_step check")

    hit_fn = src[src.index("export function actionStripHitTest("):]
    assert "isGateStripEligible(n)" in hit_fn, (
        "actionStripHitTest must consult the SAME shared predicate as "
        "drawActionStrip, or draw and hit-test can disagree about "
        "whether the 3rd button exists")


def test_strip_width_derives_from_button_count_not_grown_unconditionally():
    src = _read(_LIVE / "cards.ts")
    assert "const STRIP_W = STRIP_BTN_W * 2;" not in src, (
        "the old fixed-2-button STRIP_W constant must be gone -- a "
        "non-gate card's strip must keep its original width, not grow "
        "unconditionally for every card (that would break the existing "
        "2-button hit-test contract in test_live_graph_visual_grammar_ui.py)")
    assert "STRIP_BTN_W = 28" in src, (
        "the per-button width itself stays a fixed constant -- only the "
        "strip's TOTAL width should vary with the actual button count")


def test_action_strip_hit_test_resolves_three_slots():
    src = _read(_LIVE / "cards.ts")
    hit_fn = src[src.index("export function actionStripHitTest("):]
    # A 2-way ternary can only ever return one of two values; FR-1 requires
    # a real 3rd outcome, so the hit test must contain a "gate" resolution
    # gated by the shared eligibility predicate.
    assert '"gate"' in hit_fn, (
        "actionStripHitTest must be able to resolve a 'gate' hit")
    gate_return_idx = hit_fn.index('"gate"')
    preceding = hit_fn[:gate_return_idx]
    assert "isGateStripEligible(n)" in preceding, (
        "the 'gate' slot must only ever resolve when the shared "
        "predicate admits it -- never an unconditional 3rd slot on every "
        "card")


# ---------------------------------------------------------------------------
# FR-2: LivePage.tsx -- onPointerUp gains a third `gate` arm that sets panel
# state (no navigate()); onPointerDown's strip-first guard stays intact;
# the panel renders inside the existing relative container.
# ---------------------------------------------------------------------------

def test_live_page_imports_the_gate_panel_component():
    src = _read(_LIVE_PAGE)
    assert 'import LiveGatePanel from "@/components/live/LiveGatePanel";' in src, (
        "LivePage must mount the new panel component, never a hand-rolled "
        "second implementation inline")


def test_live_page_gate_hit_sets_panel_state_without_navigating():
    src = _read(_LIVE_PAGE)
    up_start = src.index("const onPointerUp")
    up_end = src.index("const onWheel")
    up_fn = src[up_start:up_end]

    assert 'if (hit === "gate")' in up_fn, (
        "onPointerUp must gain a third arm resolving a gate strip hit")
    gate_idx = up_fn.index('if (hit === "gate")')
    # Isolate just this arm's block body (up to its own closing brace).
    brace_idx = up_fn.index("{", gate_idx)
    depth = 0
    end_idx = brace_idx
    for i in range(brace_idx, len(up_fn)):
        if up_fn[i] == "{":
            depth += 1
        elif up_fn[i] == "}":
            depth -= 1
            if depth == 0:
                end_idx = i + 1
                break
    gate_arm = up_fn[gate_idx:end_idx]
    assert "navigate(" not in gate_arm, (
        "FR-2: a gate hit must set panel state, never navigate() -- the "
        "URL must stay /live")
    assert "setGatePanelTaskId(selectedNode.id)" in gate_arm, (
        "the gate arm must record which task the panel is for")


def test_live_page_pointer_down_strip_guard_precedes_node_hit_test():
    # Unmodified by this slice, but the oracle names it explicitly (FR-2):
    # the new button must never be reachable by a card-drag arm.
    src = _read(_LIVE_PAGE)
    down_start = src.index("const onPointerDown")
    down_end = src.index("const onPointerMove")
    down_fn = src[down_start:down_end]
    strip_idx = down_fn.index("actionStripHitTest(selectedNode")
    node_idx = down_fn.index("state.nodeAtWorld(world.x, world.y)")
    assert strip_idx < node_idx, (
        "onPointerDown must still check the action strip before "
        "nodeAtWorld -- a press on the new gate button must never arm a "
        "card drag")


def test_live_page_mounts_gate_panel_inside_the_relative_container():
    src = _read(_LIVE_PAGE)
    container_idx = src.index('<div className="relative')
    canvas_close_idx = src.index("/>", src.index("<canvas"))
    container_close_idx = src.index("</div>", canvas_close_idx)
    panel_idx = src.index("<LiveGatePanel")
    assert container_idx < panel_idx < container_close_idx, (
        "FR-2: the panel must mount inside the EXISTING relative "
        "container (the one already holding the canvas + reset-layout "
        "button), never a new page-level overlay or a route change")
    assert "navigate(`/tasks" not in src and "navigate('/tasks" not in src, (
        "opening the panel must never navigate to the task detail page"
    )


# ---------------------------------------------------------------------------
# FR-3/FR-4: LiveGatePanel.tsx -- mounts the REAL DesignPacket (hideApproval,
# diagram+prototype nested inside per mx-d0ed12) and the REAL DecisionPacket
# as a sibling Evidence section, never a hand-built second packet. No
# top-level sibling Diagram/Prototype exists next to Plan.
# ---------------------------------------------------------------------------

def test_live_gate_panel_file_exists():
    assert _LIVE_GATE_PANEL.is_file(), (
        "components/live/LiveGatePanel.tsx must exist -- the DOM overlay "
        "a gate strip button opens")


def test_live_gate_panel_mounts_the_real_design_and_decision_packets():
    src = _read(_LIVE_GATE_PANEL)
    assert 'import DesignPacket from "@/components/plan/DesignPacket";' in src, (
        "FR-3: the panel must mount the REAL DesignPacket, never a "
        "hand-built second Plan section")
    assert 'import DecisionPacket from "@/components/plan/DecisionPacket";' in src, (
        "FR-3: the panel must mount the REAL DecisionPacket, never a "
        "hand-built second Evidence section")
    assert "<DesignPacket" in src, "DesignPacket must actually be rendered"
    design_tag = src[src.index("<DesignPacket"):]
    design_tag = design_tag[: design_tag.index(">") + 1]
    assert "hideApproval" in design_tag, (
        "DesignPacket must render with hideApproval -- the panel's own "
        "decision footer is the single approval affordance (never a "
        "forked second Approve button)")
    assert "<DecisionPacket" in src, "DecisionPacket must actually be rendered"


def test_live_gate_panel_has_no_second_top_level_diagram_or_prototype():
    src = _read(_LIVE_GATE_PANEL)
    # FR-4: Diagram and Prototype are reachable ONLY inside the Plan
    # section (DesignPacket's own nested body) -- never a sibling element.
    assert "Mermaid" not in src, (
        "the panel must never import/render a diagram directly -- it is "
        "reachable only inside DesignPacket's nested Plan body")
    assert "<iframe" not in src, (
        "the panel must never render a prototype iframe directly -- it "
        "lives only inside DesignPacket's nested Plan body")


# ---------------------------------------------------------------------------
# FR-5/FR-6/FR-7: the decision footer replicates TaskDetailPage.gateDecide's
# exact shape -- needsReason refusal, approveDesignPacket awaited first at
# plan_gate, ONE gate POST, never a redirect to /tasks/<id>.
# ---------------------------------------------------------------------------

def test_live_gate_panel_gate_decide_posts_exactly_one_gate_request():
    src = _read(_LIVE_GATE_PANEL)
    assert 'const gateDecide = async (action: "approve" | "reject")' in src, (
        "FR-5: the panel's decision function must mirror "
        "TaskDetailPage.gateDecide's exact shape")
    body = src[src.index('const gateDecide = async (action: "approve" | "reject")'):]
    assert body.count("/api/conductor/gate?project=") == 1, (
        "gateDecide must POST the gate exactly once -- never a forked "
        "endpoint, never a duplicate call")
    fetch_idx = body.index("/api/conductor/gate?project=")
    call_block = body[fetch_idx: fetch_idx + 400]
    for key in ("task_id", "action", "reason", "override", "actor"):
        assert key in call_block, (
            f"the gate POST body must carry {key!r}, matching "
            "TaskDetailPage.gateDecide's exact request shape")


def test_live_gate_panel_reject_with_empty_reason_refuses_before_any_fetch():
    src = _read(_LIVE_GATE_PANEL)
    body = src[src.index('const gateDecide = async (action: "approve" | "reject")'):]
    fetch_idx = body.index("/api/conductor/gate?project=")
    guard_idx = body.index("!reason.trim()")
    assert guard_idx < fetch_idx, (
        "FR-6: the empty-reason refusal must short-circuit BEFORE the "
        "fetch call ever fires -- a reject with an empty reason must "
        "make zero network requests")
    guard_line = body[max(0, guard_idx - 80): guard_idx + 20]
    assert "return" in body[guard_idx: guard_idx + 300], (
        "the empty-reason branch must return early, not merely warn")


def test_live_gate_panel_awaits_design_packet_approval_before_the_gate_post():
    src = _read(_LIVE_GATE_PANEL)
    assert 'import { approveDesignPacket } from "@/lib/api";' in src or (
        "approveDesignPacket" in src and 'from "@/lib/api"' in src
    ), "the panel must import the SAME approveDesignPacket TaskDetailPage uses"
    body = src[src.index('const gateDecide = async (action: "approve" | "reject")'):]
    fetch_idx = body.index("/api/conductor/gate?project=")
    approve_idx = body.index("await approveDesignPacket(")
    try_idx = body.index("try")
    assert try_idx < approve_idx < fetch_idx, (
        "FR-5: at plan_gate awaiting design approval, approveDesignPacket "
        "must be awaited FIRST, inside the SAME try{} as the gate POST -- "
        "a failed packet approve must block the gate POST")


def test_live_gate_panel_never_redirects_to_task_detail_page():
    src = _read(_LIVE_GATE_PANEL)
    assert "/tasks/" not in src, (
        "FR-7: plan_gate must never redirect to /tasks/<id> -- the panel "
        "IS the decision surface, on /live")
    assert "useNavigate" not in src and "navigate(" not in src, (
        "the panel must never navigate away from /live")


# ---------------------------------------------------------------------------
# FR-8/NFR-1: verdict + enabled state derive from GET /api/conductor/
# gate/readiness plus the task's live gate_state via ONE refreshReadiness
# choke point, read on open and re-read after a decision -- never from the
# /api/work/graph node snapshot.
# ---------------------------------------------------------------------------

def test_live_gate_panel_reads_readiness_through_one_choke_point():
    src = _read(_LIVE_GATE_PANEL)
    assert "/api/conductor/gate/readiness" in src, (
        "the panel must read the SAME live readiness endpoint "
        "TaskDetailPage's card reads")
    assert "/api/work/graph" not in src, (
        "the panel's verdict must never read the /api/work/graph node "
        "snapshot -- that endpoint only decides whether the strip "
        "button exists, not what Approve will do")
    assert src.count("const refreshReadiness") == 1, (
        "NFR-1: readiness must be read through exactly ONE named choke "
        "point function")
    assert src.count("refreshReadiness()") >= 2, (
        "refreshReadiness must be called on open AND re-read after a "
        "decision, not just once")


def test_live_gate_panel_approve_button_disabled_state_derives_from_readiness():
    src = _read(_LIVE_GATE_PANEL)
    assert 'onClick={() => gateDecide("approve")}' in src
    assert 'onClick={() => gateDecide("reject")}' in src
    onclick_lit = 'onClick={() => gateDecide("approve")}'
    approve_tag = src[src.index(onclick_lit):]
    # SUPERSEDED 2026-08-17 (task d56f3b25): the arrow syntax's own "=>"
    # already carries a ">" 13 chars into onclick_lit, so a bare
    # approve_tag.index(">") always cut the window off INSIDE the matched
    # literal itself, before ever reaching the tag's real closing ">" --
    # unsatisfiable by any implementation carrying the exact onclick_lit
    # text above. Skip past the matched literal's own length first, then
    # hunt for the tag's actual close.
    rest = approve_tag[len(onclick_lit):]
    close_idx = rest.index(">") if ">" in rest[:400] else 400
    approve_tag = approve_tag[: len(onclick_lit) + close_idx + 1]
    assert "disabled=" in approve_tag, (
        "FR-8: Approve's enabled state must derive from live readiness, "
        "never render unconditionally clickable")


def test_live_gate_panel_renders_distinct_verdict_states():
    src = _read(_LIVE_GATE_PANEL)
    assert "receipt_ok" in src, "the verdict must read the live receipt_ok tooth"
    assert '"settled"' in src, (
        "NFR-1: an already-decided gate (readiness adapter=settled) must "
        "render distinctly from a genuine refusal")
    assert "stale" in src, (
        "NFR-1: a stale read must render distinctly (neutral amber), "
        "never red")


# ---------------------------------------------------------------------------
# NFR-5: user-visible /live change -> patch-bumped PRISM_VERSION.
# ---------------------------------------------------------------------------

def test_version_bumped_for_the_live_gate_decision_panel():
    ver_src = _read(_HERE.parent.parent.parent / "prism_service" / "__version__.py")
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', ver_src)
    assert m, (
        "PRISM_VERSION must be plain release semver; got "
        f"{ver_src.splitlines()[:1]!r}")
    current = tuple(int(x) for x in m.groups())
    assert current > (7, 11, 16), (
        "NFR-5: PRISM_VERSION must be patch-bumped past 7.11.16 in the "
        "implementation commit for this user-visible /live change")
