"""RED scaffold - plan approval is a plain click (task 791602a9).

Pins the DEFECT: at plan_gate with a complete-but-unapproved design
packet (isAwaitingDesignApproval true, TaskDetailPage.tsx:917-920), the
Approve button's disabled expression (line ~1863) never consults that
flag, so gateVerdict stays "blocked" and the owner is forced to tick the
override checkbox and type a reason just to approve a plan that has
nothing wrong with it. Separately, gateDecide() (line ~1285) never calls
the design-packet approval route at all, so even an override-recovered
approve leaves the packet ledger's own approved flag false forever.

Source-reading tests (this repo has no JS test runner - see
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6): comments are
stripped before any assertion and enclosing blocks are found by brace
balancing, never a fixed character window - a comment or an inserted
line must not be able to satisfy these (CLAUDE.md Lessons).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
# .../services/prism-service/tests/unit/<file> -> service root is 2 up.
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"
_API_TS = _SERVICE_ROOT / "prism_service" / "web" / "src" / "lib" / "api.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"{p} missing"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments (guarding an escaped-slash
    regex literal, per test_stale_gate_banner_inspect_vs_override.py's
    AC-1..AC-4) so a comment can never satisfy a source assertion."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _tsx() -> str:
    return _strip_comments(_read(_TSX))


def _api_ts() -> str:
    return _strip_comments(_read(_API_TS))


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


def _approve_button_disabled_expr(src: str) -> str:
    """The Approve button's `disabled={...}` expression text, found by
    scanning BACK from its unique onClick to the enclosing `disabled={`,
    then forward by brace balance - never a fixed character window."""
    marker = 'onClick={() => gateDecide("approve")}'
    call_idx = src.index(marker)
    disabled_at = src.rfind("disabled={", 0, call_idx)
    assert disabled_at != -1, "no enclosing disabled={ found before Approve's onClick"
    brace_open = disabled_at + len("disabled=")
    assert src[brace_open] == "{"
    close = _balanced(src, brace_open, "{", "}")
    return src[brace_open + 1:close]


def _gate_decide_body(src: str) -> str:
    """The gateDecide() function body, found by brace balance from its
    unique `const gateDecide = ...` definition."""
    marker = 'const gateDecide = async (action: "approve" | "reject") => {'
    start = src.index(marker)
    brace_open = start + len(marker) - 1
    assert src[brace_open] == "{"
    close = _balanced(src, brace_open, "{", "}")
    return src[brace_open:close + 1]


def _eval_disabled(expr: str, *, busy: bool, gate_verdict: str, gate_override: bool,
                    gate_reason_nonempty: bool, is_awaiting_design_approval: bool) -> bool:
    """Evaluate the button's disabled={...} JS expression under a scenario
    by substituting each recognized atom with a Python bool literal, then
    translating &&/||/! to and/or/not (same technique as
    test_livebar_refresh_contract.py's `_guard_permits_healthy_visible`).
    An atom this doesn't recognize is left alone and will raise on eval,
    which is the honest failure - never silently "satisfiable"."""
    e = re.sub(r"\s+", " ", expr)
    e = e.replace('gateVerdict !== "ready"', "True" if gate_verdict != "ready" else "False")
    e = e.replace('gateVerdict === "ready"', "True" if gate_verdict == "ready" else "False")
    e = e.replace("gateReason.trim()", "True" if gate_reason_nonempty else "False")
    e = re.sub(r"\bisAwaitingDesignApproval\b",
                "True" if is_awaiting_design_approval else "False", e)
    e = re.sub(r"\bgateOverride\b", "True" if gate_override else "False", e)
    e = re.sub(r"\bbusy\b", "True" if busy else "False", e)
    e = re.sub(r"!\s*(True|False)", r"not \1", e)
    e = e.replace("&&", " and ").replace("||", " or ")
    return bool(eval(e, {"__builtins__": {}}, {}))


# ---------------------------------------------------------------------
# Clause A - at plan_gate with isAwaitingDesignApproval true, Approve is
# enabled with the override checkbox unchecked and no reason typed.
# ---------------------------------------------------------------------


def test_approve_button_disabled_expr_names_awaiting_design_approval():
    expr = _approve_button_disabled_expr(_tsx())
    assert "isAwaitingDesignApproval" in expr, (
        "the Approve button's disabled={...} expression never references "
        "isAwaitingDesignApproval, so a complete-but-unapproved design "
        "packet still forces the override checkbox + a typed reason "
        "(clause A)"
    )


def test_approve_button_plain_click_enabled_when_awaiting_design_approval():
    expr = _approve_button_disabled_expr(_tsx())
    disabled = _eval_disabled(
        expr, busy=False, gate_verdict="blocked", gate_override=False,
        gate_reason_nonempty=False, is_awaiting_design_approval=True,
    )
    assert disabled is False, (
        "at plan_gate with isAwaitingDesignApproval true, Approve must be "
        "enabled with override unchecked and no reason typed (clause A) - "
        f"disabled expression evaluated to {disabled!r} for that scenario"
    )


# ---------------------------------------------------------------------
# Clause C - a genuinely BLOCKED, non-design-approval verdict must still
# require the override checkbox + a typed reason. The fix must not make
# every blocked gate plain-approvable.
# ---------------------------------------------------------------------


def test_genuinely_blocked_verdict_still_requires_override_and_reason():
    expr = _approve_button_disabled_expr(_tsx())
    scenario = dict(busy=False, gate_verdict="blocked",
                     is_awaiting_design_approval=False)
    assert _eval_disabled(expr, gate_override=False, gate_reason_nonempty=False,
                           **scenario) is True, (
        "a genuinely blocked, non-design-approval verdict must stay "
        "disabled with no override armed (clause C)"
    )
    assert _eval_disabled(expr, gate_override=True, gate_reason_nonempty=False,
                           **scenario) is True, (
        "override armed without a typed reason must stay disabled "
        "(clause C - the reason coupling must survive the fix)"
    )
    assert _eval_disabled(expr, gate_override=True, gate_reason_nonempty=True,
                           **scenario) is False, (
        "override armed WITH a typed reason must remain the recovery path "
        "for a genuinely blocked gate (clause C)"
    )


def test_ready_and_busy_states_unaffected_by_the_new_clause():
    """Regression guard: the plain ready-approve path and the busy lockout
    must be exactly as before - only the awaiting-design-approval case
    should change behaviour."""
    expr = _approve_button_disabled_expr(_tsx())
    assert _eval_disabled(expr, busy=False, gate_verdict="ready", gate_override=False,
                           gate_reason_nonempty=False,
                           is_awaiting_design_approval=False) is False
    assert _eval_disabled(expr, busy=True, gate_verdict="ready", gate_override=False,
                           gate_reason_nonempty=False,
                           is_awaiting_design_approval=True) is True, (
        "busy must always disable Approve, even while awaiting design "
        "approval"
    )


# ---------------------------------------------------------------------
# Clause B - gateDecide(), when isAwaitingDesignApproval is true, calls
# approveDesignPacket (POST /api/conductor/design-packet/approve) BEFORE
# posting the gate approve, inside the same try{} so a failed packet
# approve aborts before the gate POST ever fires.
# ---------------------------------------------------------------------


def test_gate_decide_calls_approve_design_packet_before_the_gate_post():
    body = _gate_decide_body(_tsx())
    assert "approveDesignPacket(" in body, (
        "gateDecide() never calls approveDesignPacket - a plain Approve at "
        "plan_gate never records the design-packet ledger's own approval "
        "(clause B)"
    )
    call_idx = body.index("approveDesignPacket(")
    gate_fetch_marker = "fetch(`/api/conductor/gate?project="
    assert gate_fetch_marker in body, "sanity: the existing gate POST must still be present"
    gate_idx = body.index(gate_fetch_marker)
    assert call_idx < gate_idx, (
        "approveDesignPacket must be called BEFORE the gate approve POST "
        "(clause B) - it currently runs after, or not at all"
    )
    guard_at = body.rfind("isAwaitingDesignApproval", 0, call_idx)
    assert guard_at != -1, (
        "the approveDesignPacket call is not guarded by "
        "isAwaitingDesignApproval - it must only fire on that path, never "
        "unconditionally on every approve or on a reject (clause B)"
    )
    try_at = body.rfind("try {", 0, call_idx)
    assert try_at != -1 and try_at < call_idx < gate_idx, (
        "approveDesignPacket must run inside the SAME try{} that wraps the "
        "gate approve POST, so a rejected/failed design-packet approve "
        "throws and the gate POST below it never runs (stop_if: never post "
        "the gate approve when the design-packet approve failed)"
    )


# ---------------------------------------------------------------------
# Clause D - api.ts exports a new approveDesignPacket function hitting
# POST /api/conductor/design-packet/approve with the task id and the
# resolved approver identity, and TaskDetailPage imports it.
# ---------------------------------------------------------------------


def test_api_ts_exports_approve_design_packet_helper():
    src = _api_ts()
    m = re.search(
        r"export\s+(?:async\s+function\s+approveDesignPacket|"
        r"const\s+approveDesignPacket\s*=)",
        src,
    )
    assert m, (
        "web/src/lib/api.ts does not export an approveDesignPacket helper "
        "(clause D)"
    )
    tail = src[m.start():m.start() + 800]
    assert "/api/conductor/design-packet/approve" in tail, (
        "approveDesignPacket must POST to "
        "/api/conductor/design-packet/approve (clause D)"
    )
    assert re.search(r"\bapi\.post\(", tail), (
        "approveDesignPacket must go through the shared api.post helper, "
        "not a bare fetch (clause D)"
    )
    assert "task_id" in tail, (
        "approveDesignPacket's POST body must carry the task id (clause D)"
    )
    assert "approver" in tail, (
        "approveDesignPacket's POST body must carry the resolved approver "
        "identity (clause D)"
    )


def test_task_detail_page_imports_approve_design_packet_from_api():
    src = _tsx()
    m = re.search(r'import\s*\{[^}]*\}\s*from\s*"@/lib/api"', src)
    assert m, "sanity: TaskDetailPage must import from @/lib/api"
    assert "approveDesignPacket" in m.group(0), (
        "TaskDetailPage.tsx does not import approveDesignPacket from "
        "@/lib/api - gateDecide cannot call it (clauses B/D wiring)"
    )


# ---------------------------------------------------------------------
# Clause E - the helper caption BESIDE the Approve/Reject buttons must not
# call an enabled, plain-click Approve "Blocked". Caught by live
# verification (Vite HMR against the real dev daemon, task 19c03bbc): the
# button correctly enables (clause A), but the adjacent caption still fell
# through to "Blocked: tick override to recover..." for the exact same
# scenario - tests-pass is not feature-works.
# ---------------------------------------------------------------------


def _caption_ternary_expr(src: str) -> str:
    """The helper `<span>{...}</span>` caption beside Approve/Reject,
    found by brace balance from its unique 'Ready: Approve records your
    decision' string (never a fixed character window)."""
    marker = "Ready: Approve records your decision"
    at = src.index(marker)
    brace_open = src.rfind("{", 0, at)
    assert brace_open != -1, "no enclosing { found before the caption text"
    close = _balanced(src, brace_open, "{", "}")
    return src[brace_open + 1:close]


def test_caption_references_awaiting_design_approval_before_the_blocked_fallback():
    expr = _caption_ternary_expr(_tsx())
    assert "isAwaitingDesignApproval" in expr, (
        "the Approve/Reject helper caption never branches on "
        "isAwaitingDesignApproval, so an enabled plain-click Approve at "
        "plan_gate still gets captioned by the generic blocked fallback "
        "(clause E)"
    )
    aware_at = expr.index("isAwaitingDesignApproval")
    blocked_at = expr.index("Blocked: tick override to recover")
    assert aware_at < blocked_at, (
        "isAwaitingDesignApproval must be checked BEFORE the generic "
        "'Blocked: tick override...' fallback in the caption ternary, or "
        "the honest branch is unreachable (clause E)"
    )


def test_caption_awaiting_branch_does_not_say_blocked():
    expr = _caption_ternary_expr(_tsx())
    aware_at = expr.index("isAwaitingDesignApproval")
    blocked_at = expr.index("Blocked: tick override to recover")
    # The string literal immediately following the isAwaitingDesignApproval
    # condition (up to the blocked fallback) must not itself be the
    # "Blocked" copy - i.e. the branch must say something else.
    between = expr[aware_at:blocked_at]
    assert "Blocked: tick override to recover" not in between, (
        "the isAwaitingDesignApproval branch of the caption still resolves "
        "to the 'Blocked' copy - an enabled Approve must not be captioned "
        "as blocked (clause E)"
    )


# ---------------------------------------------------------------------
# stop_if guard - the fix must never reintroduce the forbidden banner copy
# test_stale_gate_banner_inspect_vs_override.py already pins as absent.
# ---------------------------------------------------------------------


def test_forbidden_override_banner_copy_stays_absent():
    assert "recover with override, or fix & re-run" not in _tsx()
