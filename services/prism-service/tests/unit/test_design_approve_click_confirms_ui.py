"""RED scaffold - the Approve-design click confirms on the SAME card that
was clicked, without a full page reload (task fa7735bd).

Bug: DesignPacket.tsx (:54-61) fetches /api/conductor/design-packet via a
`load` useCallback keyed only on [taskId, project], and
`useEffect(() => { load(); }, [load])`. Nothing external can retrigger it.
TaskDetailPage.tsx's gateDecide (:1418-1480) approves the design packet AND
posts the gate, then calls the PAGE's own `load()` (refetches the task) -
never DesignPacket's. So after a successful Approve click, the Design tab's
DesignPacket card still renders the stale not-approved branch (and its
Approve button) until a full page reload.

Fix shape pinned here: TaskDetailPage bumps a refresh-signal state
(`designPacketRefreshToken`) INSIDE gateDecide's success branch (after BOTH
approveDesignPacket() and the /api/conductor/gate POST resolve, never
optimistically before) -> forwards it into <PlanView designPacketRefreshToken=.../>
-> PlanView forwards it into <DesignPacket refreshToken=.../> -> DesignPacket's
own useEffect deps include it, so approval.approved flipping true retriggers
`load()` and the not-approved branch (DesignPacket.tsx:135-141) stops
rendering, with no reload.

Second half: the success toast (TaskDetailPage.tsx:1467-1470) unconditionally
claims "This task is released." on every successful approve, including
non-terminal story_gate/plan_gate advances. Fixed by gating that clause on
`body.gate_step === "green_gate"` (the ONE terminal gate - models/workflow.py
WORKFLOW_STEPS - `gate_step` is `gate_step_id = task.workflow_step` captured
PRE-advance and returned by ConductorService.gate_decide, conductor_service.py
:3852).

Source-reading tests (this repo has no JS test runner - see
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6): comments are
stripped before any assertion and enclosing blocks are found by brace
balancing or template-literal matching, never a fixed character window - a
comment or an inserted line must not be able to satisfy these
(CLAUDE.md Lessons).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
# .../services/prism-service/tests/unit/<file> -> service root is 2 up.
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"
_PLAN_VIEW = _SERVICE_ROOT / "prism_service" / "web" / "src" / "components" / "plan" / "PlanView.tsx"
_DESIGN_PACKET = _SERVICE_ROOT / "prism_service" / "web" / "src" / "components" / "plan" / "DesignPacket.tsx"

# The signal name this fix is pinned to (chosen to match the ticket's own
# suggested shape: "TaskDetailPage bumps ... [a] refresh key").
_SIGNAL = "designPacketRefreshToken"


def _read(p: Path) -> str:
    assert p.exists(), f"{p} missing"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments (guarding an escaped-slash regex
    literal) so a comment can never satisfy a source assertion."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _tsx() -> str:
    return _strip_comments(_read(_TSX))


def _plan_view() -> str:
    return _strip_comments(_read(_PLAN_VIEW))


def _design_packet() -> str:
    return _strip_comments(_read(_DESIGN_PACKET))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _balanced(src: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the close char that matches the open char at open_idx."""
    depth = 0
    for k in range(open_idx, len(src)):
        if src[k] == open_ch:
            depth += 1
        elif src[k] == close_ch:
            depth -= 1
            if depth == 0:
                return k
    raise AssertionError(f"unbalanced {open_ch}{close_ch} scanning from {open_idx}")


def _gate_decide_body(src: str) -> str:
    """The gateDecide() function body, found by brace balance from its
    unique `const gateDecide = ...` definition (same technique as
    test_plan_approval_is_plain_click.py's _gate_decide_body)."""
    marker = 'const gateDecide = async (action: "approve" | "reject") => {'
    start = src.index(marker)
    brace_open = start + len(marker) - 1
    assert src[brace_open] == "{"
    close = _balanced(src, brace_open, "{", "}")
    return src[brace_open:close + 1]


def _gate_decide_success_branch(body: str) -> str:
    """The `else { ... }` block that runs when body.ok !== false - the
    ONLY place a genuinely-successful approve is handled - found by brace
    balance from the `if (body.ok === false) {` check, never a fixed
    character window."""
    marker = "if (body.ok === false) {"
    if_idx = body.index(marker)
    if_open = if_idx + len(marker) - 1
    assert body[if_open] == "{"
    if_close = _balanced(body, if_open, "{", "}")
    rest = body[if_close + 1:]
    m = re.match(r"\s*else\s*\{", rest)
    assert m, "no else{} block immediately follows the body.ok===false check"
    else_open = if_close + 1 + m.end() - 1
    else_close = _balanced(body, else_open, "{", "}")
    return body[else_open + 1:else_close]


def _gate_result_ok_text(success_branch: str) -> str:
    """The ok-branch `text:` template literal inside setGateResult({...}) -
    captured as raw text (nested `${ ... }` template expressions defeat a
    brace-balance parse, so this matches from the unique `kind: "ok",`
    marker to the closing backtick immediately before the setGateResult
    call's own closing `});`)."""
    marker = 'kind: "ok",'
    at = success_branch.index(marker)
    tail = success_branch[at:at + 500]
    m = re.search(r"text:\s*`(.*?)`,\s*\}\);", tail, re.S)
    assert m, "could not locate the ok-branch text template literal"
    return m.group(1)


def _planview_jsx_call(src: str) -> str:
    marker = "<PlanView"
    start = src.index(marker)
    close = src.index("/>", start)
    return src[start:close]


def _designpacket_jsx_call(src: str) -> str:
    marker = "<DesignPacket"
    start = src.index(marker)
    close = src.index("/>", start)
    return src[start:close]


# ---------------------------------------------------------------------
# Clause A - gateDecide's SUCCESS branch (after both approveDesignPacket()
# and the gate POST resolve) bumps a refresh signal. It must be inside the
# else{} success block, never before the gate POST resolves (that would be
# the optimistic-before-resolve failure mode the ticket forbids).
# ---------------------------------------------------------------------


def test_gate_decide_success_branch_bumps_a_design_packet_refresh_signal():
    body = _gate_decide_body(_tsx())
    success = _gate_decide_success_branch(body)
    assert f"set{_SIGNAL[0].upper()}{_SIGNAL[1:]}(" in success, (
        "gateDecide's success branch (the else{} after body.ok===false) "
        f"never calls set{_SIGNAL[0].upper()}{_SIGNAL[1:]}(...) - nothing "
        "signals DesignPacket to refetch after a successful Approve click, "
        "so the same card keeps rendering the stale not-approved state "
        "until a full page reload (clause A)"
    )


def test_refresh_signal_is_never_bumped_before_the_gate_post_resolves():
    body = _gate_decide_body(_tsx())
    setter = f"set{_SIGNAL[0].upper()}{_SIGNAL[1:]}("
    gate_fetch_marker = "fetch(`/api/conductor/gate?project="
    fetch_idx = body.index(gate_fetch_marker)
    before = body[:fetch_idx]
    assert setter not in before, (
        "the design-packet refresh signal is bumped BEFORE the gate POST "
        "fires - that is an optimistic update; a refused/failed approve "
        "would then show a false approved state (clause A stop_if)"
    )


# ---------------------------------------------------------------------
# Clause B - TaskDetailPage forwards the refresh signal into <PlanView .../>
# alongside isAwaitingDesignApproval, the same wiring style task 791602a9
# already used for that flag.
# ---------------------------------------------------------------------


def test_taskdetailpage_forwards_refresh_signal_into_planview():
    call = _norm(_planview_jsx_call(_tsx()))
    assert f"{_SIGNAL}={{{_SIGNAL}}}" in call, (
        "TaskDetailPage's <PlanView .../> call never forwards "
        f"{_SIGNAL} - PlanView has no way to pass a refresh signal down "
        "into DesignPacket (clause B)"
    )


# ---------------------------------------------------------------------
# Clause C - PlanView accepts that prop and forwards it into
# <DesignPacket .../> as `refreshToken` (DesignPacket's own new prop name).
# ---------------------------------------------------------------------


def test_planview_signature_accepts_the_refresh_signal_prop():
    assert f"{_SIGNAL}" in _plan_view(), (
        f"PlanView.tsx never declares a {_SIGNAL} prop - it has no signal "
        "to forward into DesignPacket (clause C)"
    )


def test_planview_forwards_refresh_signal_into_designpacket_as_refreshtoken():
    call = _norm(_designpacket_jsx_call(_plan_view()))
    assert re.search(r"refreshToken=\{[^}]*" + re.escape(_SIGNAL) + r"[^}]*\}", call), (
        "PlanView's <DesignPacket .../> call never forwards the refresh "
        f"signal as refreshToken - the card rendered inside the Design tab "
        "has nothing wired to retrigger its own fetch (clause C)"
    )


# ---------------------------------------------------------------------
# Clause D - DesignPacket accepts an optional refreshToken prop and
# includes it in the load useEffect's dependency array alongside `load`,
# so a bumped signal actually retriggers the fetch (DesignPacket.tsx:54-61).
# ---------------------------------------------------------------------


def test_designpacket_declares_refreshtoken_prop():
    assert "refreshToken" in _design_packet(), (
        "DesignPacket.tsx has no refreshToken prop declared - nothing can "
        "tell it to refetch after an external approve (clause D)"
    )


def test_designpacket_load_effect_depends_on_refreshtoken():
    src = _design_packet()
    found = False
    for eff in re.finditer(r"useEffect\(\s*\(\)\s*=>\s*\{", src):
        brace_open = eff.end() - 1
        close = _balanced(src, brace_open, "{", "}")
        dep_m = re.match(r"\s*,\s*\[([^\]]*)\]", src[close + 1:close + 200])
        if not dep_m:
            continue
        body = src[brace_open + 1:close]
        if "load()" in body and "refreshToken" in dep_m.group(1) and "load" in dep_m.group(1):
            found = True
            break
    assert found, (
        "no useEffect calling load() depends on BOTH load and refreshToken "
        "- DesignPacket's [taskId, project]-only fetch never refires when "
        "TaskDetailPage bumps the refresh signal after a successful "
        "Approve click, so the not-approved branch survives its own "
        "approval until a full reload (clause D)"
    )


# ---------------------------------------------------------------------
# Clause E - toast honesty: the ok-branch text must stop unconditionally
# claiming "This task is released." on every approve. It may say that only
# when body.gate_step is the terminal green_gate.
# ---------------------------------------------------------------------


def test_ok_toast_gates_the_released_claim_on_green_gate():
    body = _gate_decide_body(_tsx())
    success = _gate_decide_success_branch(body)
    text = _gate_result_ok_text(success)
    assert 'gate_step === "green_gate"' in text, (
        "the ok-branch toast text never checks body.gate_step === "
        "\"green_gate\" - it has no way to distinguish the terminal gate "
        "from a mid-flow story_gate/plan_gate/red_gate advance (clause E)"
    )


def test_ok_toast_no_longer_claims_released_on_bare_approve():
    body = _gate_decide_body(_tsx())
    success = _gate_decide_success_branch(body)
    text = _gate_result_ok_text(success)
    assert 'action === "approve" ? "This task is released."' not in text, (
        "the ok-branch toast still claims release off bare "
        "action === \"approve\" - a plan_gate/story_gate approve (not the "
        "terminal green_gate) still says \"This task is released.\", which "
        "is false (clause E)"
    )


def test_ok_toast_still_names_the_next_step_for_non_terminal_advances():
    """Regression guard: the existing `-> ${body.to_step}` clause, which
    already names the next step, must survive the fix."""
    body = _gate_decide_body(_tsx())
    success = _gate_decide_success_branch(body)
    text = _gate_result_ok_text(success)
    assert "body.to_step" in text, (
        "the ok-branch toast text lost its body.to_step reference - a "
        "non-terminal approve must still name the next step it advanced to"
    )


def test_approve_design_button_has_a_stable_selector():
    """A live near-miss (2026-08-26): remote-driving this button by its ONLY
    prior handle (a generic positional CSS selector, e.g.
    `div:nth-of-type(2) > button`) mis-fired onto the page's OWN unrelated
    "-> done / -> blocked / -> pending" quick-status buttons instead -- those
    PATCH /api/tasks/{id} directly (setStatus, TaskDetailPage.tsx), and only
    a 422 from the server (an invalid status transition) prevented an actual
    wrong mutation on a shared task. A gate-deciding click that matters must
    have an id/attribute selector, never a positional guess (see the
    qa-agent doctrine on this exact class of mistake)."""
    src = _design_packet()
    button_at = src.index("Approve design")
    button_open = src.rindex("<button", 0, button_at)
    button_tag = src[button_open : button_at]
    assert 'id="approve-design-btn"' in button_tag, (
        "the Approve design button must carry a stable id so a driver can "
        "target it directly instead of a fragile positional selector"
    )
