"""Pinned regression tests for task 73f13267 - "Approving a design never
needs an override".

The PRISM SPA has NO JS test runner, so UI-FIRST acceptance criteria are
pinned by asserting the ACTUAL web source (TSX) - same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6. These assert the
RENDERED tag / ENCLOSING branch, never a fixed character window or a
presence-only check: comments are stripped first (a comment explaining a
guard must not satisfy the guard's own assertion), and JSX/JS blocks are
sliced by walking brace/tag depth to their real boundary.

AC-1 (DesignPacket.tsx): the approval control ("Approve design") must be
reachable without scrolling past the prototype iframe + Mermaid diagram -
either earlier in JSX source order, or pinned via a position-sticky
ancestor - while the prototype keeps rendering unconditionally (never
collapsed into a <details> to shorten the card). FAILS today: the footer
renders LAST (after the iframe, the diagram and the plan_doc markdown) with
no sticky wrapper.

AC-2/AC-3 (TaskDetailPage.tsx gate panel): the plan_gate Approve button must
offer a legitimate, override-free path when the parked evidence is an
unapproved design packet - already true via isAwaitingDesignApproval gating
the `disabled` expression (line ~2089). Pinned so it can't regress.

AC-4 (TaskDetailPage.tsx banner): the pill text for a design-approval-pending
gate must avoid alarm words (BLOCKED/FAILED/ERROR) while a genuinely blocked
gate still uses them - already true (lines ~1742-1758). Pinned so it can't
regress.

AC-5 (TaskDetailPage.tsx gateDecide): a plan_gate cleared via an ARMED
override must record NO design-packet approval receipt - approveDesignPacket
must only fire when override is NOT armed. FAILS today: the guard around the
call (`isAwaitingDesignApproval && action === "approve"`) never checks
gateOverride, so ticking Override and clicking Approve still records a
design-packet approval receipt as if it were an explicit owner_explicit sign
-off.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_DESIGN_PACKET = _SRC / "components" / "plan" / "DesignPacket.tsx"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    # A comment saying a guard exists must never satisfy the assertion that
    # checks for the guard - strip // and /* */ comments before scanning.
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _slice_balanced(src: str, open_idx: int, open_ch: str = "{", close_ch: str = "}") -> str:
    """Return src[open_idx : matching close] by walking depth - the real
    boundary of a block/expression, never a fixed character window."""
    assert src[open_idx] == open_ch, f"expected {open_ch!r} at {open_idx}, got {src[open_idx]!r}"
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == open_ch:
            depth += 1
        elif src[i] == close_ch:
            depth -= 1
            if depth == 0:
                return src[open_idx: i + 1]
    raise AssertionError(f"unbalanced {open_ch!r}/{close_ch!r} scanning from {open_idx}")


def _component_body(src: str, export_signature: str) -> str:
    """Slice the { ... } body of a named function component, scoping
    subsequent scans to that one JSX tree. export_signature must end at the
    function's opening '(' - the (possibly multi-line, destructured)
    parameter list is walked by PAREN depth first (never the naive first
    '{' after the signature, which lands inside the destructured params),
    then the real body is sliced by brace depth from the '{' after that."""
    start = src.index(export_signature)
    paren_open = start + len(export_signature) - 1
    assert src[paren_open] == "(", f"export_signature must end at '(': {export_signature!r}"
    params = _slice_balanced(src, paren_open, "(", ")")
    close_paren = paren_open + len(params) - 1
    brace = src.index("{", close_paren)
    return _slice_balanced(src, brace)


_WRAP_TAGS = ("div", "details")
_WRAP_TAG_RE = re.compile(r"<(?:%s)\b[^>]*>|</(?:%s)>" % ("|".join(_WRAP_TAGS), "|".join(_WRAP_TAGS)))


def _ancestor_elements(src: str, marker_idx: int) -> list[str]:
    """Return every <div>/<details> opening tag that structurally wraps
    src[marker_idx], innermost first - a JSX-aware equivalent of walking up
    the DOM, built from tag-depth (not a fixed-radius substring search)."""
    stack: list[tuple[int, str]] = []
    wrapping: list[tuple[int, str, int]] = []
    for m in _WRAP_TAG_RE.finditer(src):
        tok = m.group(0)
        if tok.startswith("</"):
            if not stack:
                continue
            open_s, open_tag = stack.pop()
            if open_s <= marker_idx <= m.end():
                wrapping.append((open_s, open_tag, m.end()))
        else:
            stack.append((m.start(), tok))
    wrapping.sort(key=lambda t: t[2] - t[0])
    return [t[1] for t in wrapping]


# ---------------------------------------------------------------------------
# AC-1: approval reachable without scrolling past the prototype + diagram.
# ---------------------------------------------------------------------------

def test_approval_control_precedes_or_is_pinned_above_the_prototype():
    src = _strip_comments(_read(_DESIGN_PACKET))
    body = _component_body(src, "export default function DesignPacket(")

    assert 'title="prototype"' in body, "expected the prototype iframe marker in DesignPacket"
    assert "Approve design" in body, "expected the approval button text in DesignPacket"

    approve_idx = body.index("Approve design")
    iframe_idx = body.index('title="prototype"')

    reachable_by_order = approve_idx < iframe_idx
    ancestor_tags = _ancestor_elements(body, approve_idx)
    reachable_by_pin = any("sticky" in t.lower() for t in ancestor_tags)

    assert reachable_by_order or reachable_by_pin, (
        "the approval control ('Approve design') must render BEFORE the "
        "prototype iframe in JSX source order, or sit inside a "
        "position-sticky ancestor - today it renders after the iframe, the "
        "diagram and the plan_doc markdown with no sticky wrapper, so a "
        "reviewer must scroll past all three screens to reach it"
    )


def test_prototype_stays_unconditionally_visible_not_collapsed():
    # stop_if guard: the fix must never collapse/hide the prototype to
    # shorten the card. Regression-pin the CURRENT, correct behaviour.
    src = _strip_comments(_read(_DESIGN_PACKET))
    body = _component_body(src, "export default function DesignPacket(")

    assert "data.prototype.exists && prototypeSrc" in body, (
        "the prototype must still render whenever the packet actually has "
        "one - unconditional on existence, not hidden behind a toggle"
    )
    iframe_idx = body.index('title="prototype"')
    ancestor_tags = _ancestor_elements(body, iframe_idx)
    assert not any(t.lower().startswith("<details") for t in ancestor_tags), (
        "the prototype iframe must not be wrapped in a <details>/<summary> "
        "collapse to shorten the card - it must stay visible"
    )


# ---------------------------------------------------------------------------
# AC-2/AC-3: the gate panel offers a legitimate design-approval affordance,
# and override is not the only enabled exit for it.
# ---------------------------------------------------------------------------

def _approve_button_disabled_expr(src: str) -> str:
    onclick = 'onClick={() => gateDecide("approve")}'
    assert onclick in src, "expected the gate panel's plain Approve control"
    onclick_idx = src.index(onclick)
    button_open = src.rindex("<button", 0, onclick_idx)
    disabled_at = src.index("disabled={", button_open)
    assert disabled_at < onclick_idx, "expected disabled= before onClick on the same <button>"
    brace_open = src.index("{", disabled_at)
    return _slice_balanced(src, brace_open)


def test_approve_button_offers_a_legitimate_design_approval_affordance():
    src = _strip_comments(_read(_TASK_DETAIL))
    disabled_expr = _approve_button_disabled_expr(src)
    assert "isAwaitingDesignApproval" in disabled_expr, (
        "the Approve button's disabled expression must reference "
        "isAwaitingDesignApproval, so a design-approval-pending gate has a "
        "real Approve affordance, not just Override"
    )
    # The helper copy under the controls must name what the click DOES for
    # this state (signs the packet), not just leave Override as the story.
    assert "records the design-packet sign-off" in src, (
        "the decision panel must explain that Approve, in this state, "
        "signs the design packet - the legitimate approval affordance"
    )


def test_override_is_not_the_only_enabled_exit_for_design_approval():
    src = _strip_comments(_read(_TASK_DETAIL))
    disabled_expr = _approve_button_disabled_expr(src)
    # The override-required clause must be short-circuited OFF by
    # !isAwaitingDesignApproval, so approving a design packet needs no
    # override at all - override stays available for OTHER refusals only.
    assert (
        re.search(r"!\s*isAwaitingDesignApproval\s*&&\s*!\s*gateOverride", disabled_expr)
        or re.search(r"!\s*gateOverride\s*&&\s*!\s*isAwaitingDesignApproval", disabled_expr)
    ), (
        "the disabled expression's override-required clause must be gated "
        "off by !isAwaitingDesignApproval - today's expression: "
        f"{disabled_expr!r} - otherwise override is the only enabled exit "
        "for a design-approval-pending gate"
    )


# ---------------------------------------------------------------------------
# AC-4: banner text avoids alarm words for an awaiting-approval state, but a
# genuinely blocked gate still uses them (comparative, not presence-only).
# ---------------------------------------------------------------------------

def test_banner_avoids_alarm_words_when_awaiting_but_not_when_blocked():
    src = _strip_comments(_read(_TASK_DETAIL))
    start = src.index("return isAwaitingDesignApproval")
    end = src.index(";", start)
    ternary = src[start:end]

    awaiting_end = ternary.index(":", ternary.index("AWAITING YOUR APPROVAL"))
    awaiting_branch = ternary[:awaiting_end]
    for alarm_word in ("BLOCKED", "FAILED", "ERROR"):
        assert alarm_word not in awaiting_branch.upper(), (
            f"the awaiting-design-approval branch must avoid the alarm "
            f"word {alarm_word!r} - a parked, available human decision must "
            f"not read as broken"
        )
    assert "BLOCKED" in ternary, (
        "a genuinely blocked branch of this SAME ternary must still use "
        "BLOCKED - otherwise this isn't distinguishing states, it's just "
        "alarm words removed everywhere"
    )


# ---------------------------------------------------------------------------
# AC-5: a plan_gate cleared via an ARMED override must record NO
# design-packet approval receipt.
# ---------------------------------------------------------------------------

def test_override_approve_never_records_a_design_packet_approval():
    src = _strip_comments(_read(_TASK_DETAIL))
    anchor = src.index("const gateDecide")
    arrow_idx = src.index("=>", anchor)
    fn_brace = src.index("{", arrow_idx)
    fn_body = _slice_balanced(src, fn_brace)

    assert "approveDesignPacket(" in fn_body, "expected the design-packet approve call in gateDecide"
    call_idx = fn_body.index("approveDesignPacket(")
    if_idx = fn_body.rindex("if (", 0, call_idx)
    cond_open = fn_body.index("(", if_idx)
    cond = _slice_balanced(fn_body, cond_open, "(", ")")

    assert re.search(r"!\s*gateOverride", cond), (
        "the guard around approveDesignPacket(...) must require "
        "!gateOverride before it fires - today's guard is "
        f"{cond!r}, so ticking Override and clicking Approve while "
        "isAwaitingDesignApproval is true STILL records a design-packet "
        "approval receipt, forging an explicit owner_explicit sign-off "
        "from what was actually an override release"
    )
