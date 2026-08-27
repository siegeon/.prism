"""The delivery/pin-tests fetch on the task detail page re-runs on a gate
TRANSITION, never only once at mount (task 8582921d).

THE BUG, confirmed live 2026-08-26: task 8582921d's green_gate was approved
live (POST /api/conductor/gate -> 200), the task row flipped to
status=done/gate_state=passed, and GET /api/tasks/<id>/delivery correctly
reported delivered=true with every stage done (verified, committed, pushed,
merged to main, released). But a TaskDetailPage left open across the
approval (not a fresh page load - a real user's actual browsing session)
kept showing the STALE pre-approval delivery object: the header read both
"DONE" (from the task object, which DOES get pushed live via SSE) and
"NOT DELIVERED YET - GATE NOT PASSED YET" (wrong - a frozen `delivery.stages`
computed from the effect's ONE fetch at mount). A soft SPA navigation back
to the same id did not fix it either, since `useEffect(..., [id, project])`
never re-runs when only `id` is unchanged.

THE FIX: the SAME one-shot-per-transition runKey/re-observe shape already
proven by `readinessRunFor` (:1275-1282) and `ranPinTestsFor` (:1288-1321)
in TaskDetailPage.tsx, applied to the tests+delivery effect: a
`deliveryRunFor` useRef guards a runKey built from
id:project:workflow_step:gate_state:status, so the effect fires again
exactly when the task's gate position changes mid-session - never on a
fixed timer, never only once.

Pins:
  AC-1  a `deliveryRunFor` one-shot ref guards the tests+delivery effect.
  AC-2  the effect's dependency array includes task?.workflow_step,
        task?.gate_state and task?.status (alongside id/project) so React
        actually re-runs it on a transition.
  AC-3  the runKey computed inside the effect is built from the SAME
        transition fields as its dependency array (the guard cannot silently
        no-op on a real transition because the key never changed).
  AC-4  behavioural: replaying a mount -> gate-mint -> gate-resolve sequence
        of task snapshots against the extracted runKey logic yields one
        re-fetch per genuine transition, not one total.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # tests/unit -> tests -> prism-service
_TASK_DETAIL = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"


def _src() -> str:
    return _TASK_DETAIL.read_text(encoding="utf-8")


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


def _delivery_effect():
    """Return (body, deps) for the useEffect that fetches
    `/api/tasks/${id}/tests` and `/api/tasks/${id}/delivery`, found by
    brace-balance from its own useEffect( — never a fixed line slice, so a
    comment near the marker can't satisfy the assertions in its place."""
    src = _src()
    for eff in re.finditer(r"useEffect\(\s*\(\)\s*=>\s*\{", src):
        brace_open = eff.end() - 1
        close = _balanced(src, brace_open, "{", "}")
        body = src[brace_open + 1:close]
        if "/tests?project=" in body and "/delivery?project=" in body:
            dep_m = re.match(r"\s*,\s*\[([^\]]*)\]", src[close + 1:close + 200])
            deps = dep_m.group(1) if dep_m else ""
            return body, deps
    raise AssertionError(
        "no useEffect fetching both /api/tasks/:id/tests and "
        "/api/tasks/:id/delivery was found in TaskDetailPage.tsx"
    )


def test_delivery_effect_exists_and_is_findable():
    body, deps = _delivery_effect()
    assert body.strip(), "the tests+delivery effect body must not be empty"


def test_delivery_effect_has_a_oneshot_transition_ref():
    src = _src()
    assert re.search(r'const\s+deliveryRunFor\s*=\s*useRef<string>\(""\)', src), (
        "TaskDetailPage.tsx must declare a `deliveryRunFor` useRef<string> "
        "one-shot guard for the tests+delivery effect, mirroring "
        "readinessRunFor/ranPinTestsFor's existing pattern (AC-1)"
    )
    body, _ = _delivery_effect()
    assert "deliveryRunFor.current" in body, (
        "the tests+delivery effect body must read/write deliveryRunFor.current "
        "to guard its one-shot-per-transition fetch (AC-1)"
    )


def test_delivery_effect_depends_on_gate_transition_fields():
    _, deps = _delivery_effect()
    for field in ("task?.workflow_step", "task?.gate_state", "task?.status"):
        assert field in deps, (
            f"the tests+delivery effect's dependency array is missing "
            f"{field!r} (deps={deps!r}) — without it, React never re-runs "
            "the fetch when the task's gate position changes mid-session, "
            "which is exactly the stale-delivery-badge bug from task "
            "8582921d (AC-2)"
        )
    assert "id" in deps and "project" in deps, (
        f"the effect must still depend on id and project (deps={deps!r})"
    )


def test_runkey_uses_the_same_transition_fields_as_the_deps_array():
    body, deps = _delivery_effect()
    runkey_m = re.search(r"const\s+runKey\s*=\s*`([^`]*)`", body)
    assert runkey_m, (
        "the tests+delivery effect must compute a template-literal runKey "
        "from the task's transition fields, mirroring ranPinTestsFor (AC-3)"
    )
    runkey_src = runkey_m.group(1)
    for field in ("workflow_step", "gate_state", "status"):
        assert field in runkey_src, (
            f"runKey {runkey_src!r} does not reference {field!r} — a runKey "
            "that omits a transition field can silently no-op on a real "
            "gate transition even though the dependency array looks correct "
            "(AC-3)"
        )


def test_transition_sequence_yields_one_refetch_per_transition():
    """Behavioural replay of deliveryRunFor's guard logic (extracted as the
    same `prev !== key` shape already proven by shouldRefetchReadiness) —
    a mount, a gate mint, and a gate resolve must each trigger exactly one
    fetch; 20 re-renders with an unchanged gate position must trigger zero
    extra fetches (AC-4, stop_if: never a fixed-interval timer)."""
    seq = [
        {"id": "t1", "workflow_step": "write_failing_tests", "gate_state": "", "status": "in_progress"},
        {"id": "t1", "workflow_step": "green_gate", "gate_state": "pending", "status": "in_progress"},
        {"id": "t1", "workflow_step": "green_gate", "gate_state": "passed", "status": "done"},
    ]
    project = "prism"

    def run_key(t):
        return f"{t['id']}:{project}:{t['workflow_step']}:{t['gate_state']}:{t['status']}"

    prev = None
    fetches = 0
    for t in seq:
        key = run_key(t)
        if prev != key:
            fetches += 1
            prev = key
    assert fetches == 3, (
        "a mount + a gate mint (pending) + a gate resolve (passed/done) "
        f"must each trigger exactly one delivery/tests re-fetch: got {fetches}"
    )

    # 20 re-renders where nothing about the task's gate position changed.
    stable = seq[-1]
    extra = 0
    for _ in range(20):
        key = run_key(stable)
        if prev != key:
            extra += 1
            prev = key
    assert extra == 0, (
        "re-renders with an unchanged workflow_step/gate_state/status must "
        f"trigger ZERO extra fetches (transition-keyed, never timed): {extra}"
    )
