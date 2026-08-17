"""Override actually releases a stuck gate (task 377b00a8) - UI half.

Owner incident 2026-08-06 (task 7feed0c8, mirrored github #311 / jira
PRIS-43): a stuck green_gate with a refused/stale oracle receipt turned a
single Approve/Override click into a permanently stranded task. Pins the
two UI/client defects that ship in THIS slice - never the readiness-vs-
decider divergence (belongs to sibling 5c61e0e6, already done).

  1. COPY (TaskDetailPage.tsx:2101): the Override checkbox said "bypass
     the verifier and release on manual judgment" - unconditional, but
     override does NOT skip the oracle receipt check (conductor_service.py
     "NO OVERRIDE-SKIPS-THE-ORACLE", ~3550-3579): a stale/refused receipt
     still refuses. Truthful copy must say what override actually bypasses
     (the shell verifier, never the oracle) and name the real remedy
     (re-run the oracle for a fresh receipt, then Approve with override
     UNTICKED).
  2. CLIENT UX (TaskDetailPage.gateDecide): the gate-decide POST had no
     AbortController/timeout, so a wedged request left "checking..."
     indistinguishable from a dead page forever.

PATH B SPLIT (plan_doc section 2, this task's own plan, no owner policy
authorisation on record as of this step): the third defect - a refused
oracle-receipt Approve strands gate_state="failed" instead of parking
"pending" - lives inside services/conductor_service.py, a
control_plane.POLICY_FILES entry outside this task's allowed_files. That
half is filed as child task 97d92854 ("A refused approve parks the gate
pending"), tagged needs-policy-authorisation, not silently dropped: see
this task's completion_proof for the pointer. The two backend tests that
originally lived in this file at the red step moved verbatim into
97d92854's own pinned suite (tests/unit/test_gate_refusal_parks_pending.py).
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
        / "TaskDetailPage.tsx")


# --------------------------------------------------------------------------
# Defect 1 - the Override checkbox copy is truthful and names the remedy.
# --------------------------------------------------------------------------

def _override_label_block() -> str:
    """The RENDERED <label> block wrapping the Override checkbox
    (TaskDetailPage.tsx ~2098-2103) - parses the enclosing JSX branch by
    brace/tag structure, never a fixed character window a comment above
    the element could satisfy (a lesson paid for three times already)."""
    src = _TSX.read_text(encoding="utf-8")
    m = re.search(
        r"<label[^>]*>.*?checked=\{gateOverride\}.*?</label>", src, re.S)
    assert m, (
        "could not locate the rendered Override <label>...<input "
        "checked={gateOverride}.../>...</label> block in "
        "TaskDetailPage.tsx - the affordance moved; update this locator")
    span = re.search(r"<span>(.*?)</span>", m.group(0), re.S)
    assert span, "the Override <label> has no <span> copy to read"
    return span.group(1)


def test_override_copy_names_the_verifier_not_the_oracle():
    low = _override_label_block().lower()
    assert "verifier" in low, (
        "Override copy must name what it actually bypasses - the shell "
        "verifier")
    assert "bypass the oracle" not in low, (
        "Override copy must NEVER claim it bypasses the oracle - override "
        "still requires a fresh oracle EvidenceReceipt "
        "(conductor_service.py NO-OVERRIDE-SKIPS-THE-ORACLE)")
    assert "oracle" in low, (
        "truthful copy must say the oracle evidence check STILL applies "
        "under override, or an owner reads 'release on manual judgment' "
        "as unconditional - the exact misreading behind the 7feed0c8 "
        "incident")


def test_override_copy_names_the_working_remedy():
    low = _override_label_block().lower()
    assert re.search(r"re-?run", low), (
        "copy must name the working remedy: re-run the oracle to mint a "
        "fresh receipt")
    assert re.search(r"(uncheck|un-?tick|without override)", low), (
        "copy must say the recovery path is Approve with override "
        "UNTICKED once evidence is fresh - not 'stay ticked forever'")
    assert "release on manual judgment" not in low, (
        "the unconditional 'release on manual judgment' claim is the "
        "lie: a refused oracle receipt still refuses under override "
        "today")


# --------------------------------------------------------------------------
# Defect 2 (renumbered from the ticket's #3) - the gate-decide client
# submit has a real timeout/abort UX.
# --------------------------------------------------------------------------

def _gate_decide_fn_source() -> str:
    """The gateDecide() function body, brace-matched - never a fixed line
    window: an UNRELATED setTimeout/clearTimeout pair already exists
    elsewhere in this file (a notice-dismiss timer at ~1297-1298), so a
    whole-file grep for those tokens would false-pass today."""
    src = _TSX.read_text(encoding="utf-8")
    start = src.index("const gateDecide = async")
    i = src.index("{", start)
    depth, j = 0, i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[start:j + 1]


def test_gate_decide_has_abort_controller_wired_to_the_fetch():
    fn = _gate_decide_fn_source()
    assert "AbortController" in fn, (
        "gateDecide() has no AbortController - a wedged POST leaves "
        "'checking...' indistinguishable from a dead page forever")
    assert re.search(r"signal\s*:", fn), (
        "the AbortController's signal must be wired into the fetch() "
        "call options, or constructing it does nothing")
    assert "setTimeout(" in fn and ".abort()" in fn, (
        "a setTimeout must call the controller's .abort() - an "
        "AbortController with no timer never actually times out")
    assert "clearTimeout" in fn, (
        "the timer must be cleared on a normal (non-timeout) completion, "
        "or a finished request still fires a stray abort() later")


def test_gate_decide_surfaces_elapsed_time_while_checking():
    fn = _gate_decide_fn_source()
    assert re.search(r"elapsed", fn, re.I), (
        "gateDecide() must surface elapsed time while CHECKING - the "
        "owner cannot tell a slow machine check from a hung one "
        "otherwise (mx-d6c1df names exactly this drive-liveness failure "
        "mode)")
    assert re.search(r"Date\.now\(\)", fn), (
        "elapsed time must be computed from a real clock read "
        "(Date.now()) captured at submit time, not a hardcoded label")


def test_gate_decide_timeout_returns_control_with_a_next_action():
    fn = _gate_decide_fn_source()
    assert re.search(r"AbortError", fn), (
        "the catch block must distinguish a client-side timeout/abort "
        "(err.name === 'AbortError') from a real server refusal, or the "
        "owner gets the same opaque message either way")
    assert re.search(r"(timed out|timeout)", fn, re.I), (
        "the timeout branch must say plainly that it timed out")
    assert re.search(r"(try again|retry|re-run|check readiness)", fn,
                     re.I), (
        "the timeout message must name a next action - clearing busy "
        "silently with no guidance reproduces the same stuck-owner "
        "defect this ticket exists to fix")
    assert "setBusy(false)" in fn, (
        "on timeout, busy must still clear (control returns to the "
        "owner) - a hang that never resets busy is the CHECKING-looks-"
        "like-DEAD failure mode verbatim")
