"""Gate banner re-fetches readiness on gate arrival; one severity vocabulary
shared by the tile, the banner and the step row (task 8e5aa63b).

THE BUG (round-2 gauntlet critic, 2026-08-15): a646cbd1 (shipped) taught the
banner to QUOTE the live per-gate-shape rubric reason, but the readiness
fetch that feeds it is still a mount-only one-shot — TaskDetailPage.tsx's
readiness effect (`useEffect(..., [id, project])`, the GET at
`/api/conductor/gate/readiness`) never re-fires when `task.gate_state` /
`task.workflow_step` transition via the live `/sse/tasks` push. A page
opened BEFORE a gate mints therefore renders the legacy generic
"BLOCKED · evidence not on file" literal forever, even though the server is
sitting on a fresh, gate-shape-correct payload. The SAME moment also shows
three different severities for one gate: the ConductorPage tile, this
banner, and StepRail's gate row.

THE FIX UNDER TEST (per plan_doc D-1..D-4):
  D-1  a single `refreshReadiness` choke point, re-observed on a runKey
       transition (`${id}:${workflow_step}:${gate_state}:${status}`), the
       same pattern already proven by `ranPinTestsFor` (:1153-1186).
  D-2  `web/src/lib/gateSeverity.ts` — ONE total function `gateSeverity()`
       that the banner, StepRail's gate row and the tile's handoff line all
       call, so the three surfaces cannot re-diverge.
  D-4  both the readiness-refresh logic and gateSeverity's derivation are
       delimited by marker comments so this suite EXECUTES the shipped code
       under node (precedent: test_implement_step_budget.py:70-115) instead
       of pattern-matching source text — a behavioural discriminator, not a
       spelling check.

Pins, RED-first (fail against current source — no gateSeverity.ts, no
runKey-keyed readiness re-fetch yet):
  AC-1  readiness re-fetches automatically on a gate_state/workflow_step
        transition, via the runKey pattern — no click, no timer.
  AC-2  after a mid-session transition the banner quotes the live per-shape
        reason (never the frozen/legacy fallback); proven both server-side
        (readiness genuinely changes across a transition) and client-side
        (the headline expression derives its verdict from gateSeverity()).
  AC-3  the tile, banner and step row share ONE severity vocabulary — same
        function, same four keys, and a null readiness (the tile's input)
        never escalates to blocked/awaiting_you.
  AC-4  no fixed-interval poll: the refetch is transition-keyed, and the
        readiness URL literal appears exactly once (one choke point).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # tests/unit -> tests -> prism-service
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_STEP_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"
_PLAN_VIEW = _SRC / "components" / "plan" / "PlanView.tsx"
_CONDUCTOR_PAGE = _SRC / "pages" / "ConductorPage.tsx"
_GATE_SEVERITY = _SRC / "lib" / "gateSeverity.ts"

_READINESS_OPEN = "// --- READINESS-REFRESH-BLOCK-START ---"
_READINESS_CLOSE = "// --- READINESS-REFRESH-BLOCK-END ---"
_SEVERITY_OPEN = "// --- GATE-SEVERITY-BLOCK-START ---"
_SEVERITY_CLOSE = "// --- GATE-SEVERITY-BLOCK-END ---"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the JSX `{...}` expression starting at the first `{` after
    `marker`, matched by BRACE DEPTH — never a fixed slice — so a comment or
    unrelated literal near the marker can't satisfy an assertion in place of
    the real guard (repeat failure mode, see test_gate_banner_honest_state_ui.py
    and test_gate_banner_reason_matches_gate.py)."""
    idx = src.index(marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {marker!r}")


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed; cannot execute the marker block")
    return exe


def _marker_block(src: str, open_marker: str, close_marker: str, where: str) -> str:
    """The pure, executable slice of `where` delimited by the given markers.
    Types/JSX/exports must stay OUTSIDE the markers (D-4) so the block is
    plain JS a bare `node` can run — the same discipline
    test_implement_step_budget.py:70-80 already applies to implement.js."""
    if open_marker not in src or close_marker not in src:
        pytest.fail(
            f"{where} must delimit its logic with {open_marker} ... "
            f"{close_marker} so this suite can EXECUTE it under node instead "
            "of pattern-matching the source text. Neither marker was found."
        )
    body = src.split(open_marker, 1)[1].split(close_marker, 1)[0]
    assert body.strip(), f"the marker block in {where} is empty"
    return body


def _run_node(block: str, script_tail: str) -> str:
    """EXECUTE `block` (a plain-JS marker block) plus `script_tail` under
    node. Returns raw stdout. Behavioural, not textual: the numbers/labels
    asserted below come from the real shipped code running, not from a
    regex over its spelling."""
    script = f"{block}\n{script_tail}\n"
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mjs"
        probe.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [_node(), str(probe)], capture_output=True, text=True, timeout=60,
        )
    if proc.returncode != 0:
        pytest.fail(
            f"the marker block did not execute under node (rc={proc.returncode}). "
            "It must be pure, side-effect-free plain JS with no TypeScript-only "
            f"syntax inside the markers.\nstderr:\n{proc.stderr.strip()}\n"
            f"stdout:\n{proc.stdout.strip()}"
        )
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# AC-1 / AC-4 — readiness re-fetches on a gate transition, never on a timer
# ---------------------------------------------------------------------------


def _readiness_refresh_block() -> str:
    src = _read(_TASK_DETAIL)
    return _marker_block(src, _READINESS_OPEN, _READINESS_CLOSE, "TaskDetailPage.tsx")


def test_readiness_refetches_on_gate_arrival():
    block = _readiness_refresh_block()
    script_tail = """
const seq = [
  { id: "t1", workflow_step: "write_failing_tests", gate_state: "", status: "in_progress" },
  { id: "t1", workflow_step: "story_gate", gate_state: "pending", status: "in_progress" },
  { id: "t1", workflow_step: "story_gate", gate_state: "passed", status: "in_progress" },
];
let prevKey = null;
let fetches = 0;
for (const task of seq) {
  const key = readinessRunKey(task.id, task);
  if (shouldRefetchReadiness(prevKey, key)) { fetches += 1; prevKey = key; }
}
console.log(JSON.stringify({ fetches }));
"""
    out = json.loads(_run_node(block, script_tail))
    assert out["fetches"] == 3, (
        "a mount + a gate mint (pending) + a gate resolve (passed) must each "
        f"trigger exactly one readiness re-fetch (3 total transitions): {out}"
    )


def test_refetch_is_transition_keyed_not_timed():
    block = _readiness_refresh_block()
    script_tail = """
const task = { id: "t1", workflow_step: "story_gate", gate_state: "pending", status: "in_progress" };
const startKey = readinessRunKey(task.id, task);
let prevKey = startKey;
let extra = 0;
for (let i = 0; i < 20; i++) {
  // 20 re-renders where unrelated state moved but the gate fields did not.
  const sameKey = readinessRunKey(task.id, task);
  if (shouldRefetchReadiness(prevKey, sameKey)) extra += 1;
  prevKey = sameKey;
}
console.log(JSON.stringify({ extra }));
"""
    out = json.loads(_run_node(block, script_tail))
    assert out["extra"] == 0, (
        "20 re-renders with an unchanged gate_state/workflow_step/status "
        f"must trigger ZERO extra readiness fetches (transition-keyed, "
        f"never timed — stop_if): {out}"
    )


def test_no_fixed_interval_readiness_poll_and_one_choke_point():
    src = _read(_TASK_DETAIL)
    assert "setInterval" not in src, (
        "no fixed-interval timer against /api/conductor/gate/readiness may "
        "be introduced (stop_if) — TaskDetailPage.tsx must contain no "
        "setInterval at all"
    )
    url_literal = "/api/conductor/gate/readiness?task_id="
    count = src.count(url_literal)
    assert count == 1, (
        "the readiness URL literal must occur exactly ONCE in "
        "TaskDetailPage.tsx (one refreshReadiness choke point used by both "
        f"the mount/transition effect and the manual re-run oracle button, "
        f"D-1) — found {count} occurrence(s)"
    )


# ---------------------------------------------------------------------------
# AC-2 — the banner quotes the live per-shape reason, never a frozen one
# ---------------------------------------------------------------------------


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


_NO_AC_STORY = """# Summary
Fix the gate banner.

# Acceptance Criteria
Do the thing well.
"""

_COMPLIANT_STORY = """# Summary
Fix the gate banner.

# Requirements
Quote the live reason.

# Acceptance Criteria
- AC-1: banner quotes the live reason. oracle: read the rendered headline.
"""


def test_readiness_payload_changes_across_the_transition(tmp_path, monkeypatch):
    """Proves the SERVER truth is genuinely dynamic across a gate mid-session
    transition — the motivating fact for AC-1's client-side re-fetch: a
    mount-only read would freeze on `before` forever."""
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="story gate rubric task", tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py -q",
                        proof_type="test", likely_misfire="looks fine but is not")
    task_svc.update(t.id, workflow_step="story_gate", gate_state="pending",
                    plan_doc=_NO_AC_STORY)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    before = capi.gate_readiness(task_id=t.id, project="default")
    assert before["receipt_ok"] is False, before
    assert before["receipt"]["adapter"] == "story-rubric", before

    # The gate resolves mid-session — a drive fixes the story doc.
    task_svc.update(t.id, plan_doc=_COMPLIANT_STORY)
    after = capi.gate_readiness(task_id=t.id, project="default")

    assert after["receipt_ok"] is True, after
    assert after["receipt"]["reason"] != before["receipt"]["reason"], (
        "readiness must produce a materially different verdict/reason "
        f"across the transition — before={before}, after={after}"
    )


def test_headline_prefix_comes_from_gate_severity():
    src = _read(_TASK_DETAIL)
    assert re.search(r"""from\s+["'][./]*lib/gateSeverity["']""", src), (
        "TaskDetailPage.tsx must import the shared gateSeverity derivation "
        "instead of hand-rolling its own headline literals"
    )
    expr = _extract_braced_expr(src, "● {")
    assert "gateSeverity(" in expr, (
        "the headline expression must render via gateSeverity(...) so the "
        "banner, StepRail's gate row and the board tile cannot re-diverge"
    )
    # The legacy generic literal survives (AC-7 precedent, mx-a31a3d) but
    # must be reachable ONLY via the shared function's blocked verdict —
    # never a private, re-derived ternary duplicated in this file.
    assert "BLOCKED · evidence not on file" in expr, expr
    idx_blocked = expr.index("BLOCKED · evidence not on file")
    idx_guard = expr.find('.key === "blocked"')
    assert idx_guard != -1 and idx_guard < idx_blocked, (
        "the 'BLOCKED · evidence not on file' literal must be gated behind "
        "gateSeverity(...).key === \"blocked\", checked BEFORE the literal "
        f"in source order — guard_idx={idx_guard}, literal_idx={idx_blocked}"
    )


# ---------------------------------------------------------------------------
# AC-3 — tile, banner and step row share ONE severity vocabulary
# ---------------------------------------------------------------------------


def _gate_severity_block() -> str:
    if not _GATE_SEVERITY.exists():
        pytest.fail(
            f"missing {_GATE_SEVERITY} — the shared gate-severity "
            "derivation module (D-2) does not exist yet"
        )
    src = _read(_GATE_SEVERITY)
    return _marker_block(src, _SEVERITY_OPEN, _SEVERITY_CLOSE, "gateSeverity.ts")


_MATRIX = {
    "rubric_pending": {"gate_state": "pending", "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "story-rubric", "reason": "no AC-n ids"}}},
    "design_packet": {"gate_state": "pending", "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "design-packet", "passed": False}}},
    "human_review": {"gate_state": "pending", "readiness": {
        "receipt_ok": True, "manual_review": True, "receipt": {"adapter": "human"}}},
    "verifier_refused": {"gate_state": "pending", "verifier_refused": True, "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "green"}}},
    # Renamed from "epic_rollup_blocked" (superseded: an epic-rollup
    # refusal is no longer the alarm-word BLOCKED — see
    # test_epic_rollup_gate_severity_reads_waiting_not_blocked.py).
    "epic_rollup_waiting": {"gate_state": "pending", "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "epic-rollup"},
        "blocking_children": [{"id": "c1", "title": "child"}]}},
    "receipt_ok": {"gate_state": "pending", "readiness": {
        "receipt_ok": True, "receipt": {"adapter": "green"}}},
    "readiness_null": {"gate_state": "pending", "readiness": None},
}

# Five labels (was four — see test_epic_rollup_gate_severity_reads_waiting_not_blocked.py).
_ALLOWED_LABELS = {"READY", "PENDING", "AWAITING YOU", "WAITING ON CHILDREN", "BLOCKED"}


def test_gate_severity_matrix_stays_within_four_labels():
    block = _gate_severity_block()
    script_tail = f"""
const matrix = {json.dumps(_MATRIX)};
const out = {{}};
for (const [name, snap] of Object.entries(matrix)) {{
  out[name] = gateSeverity(snap);
}}
console.log(JSON.stringify(out));
"""
    results = json.loads(_run_node(block, script_tail))
    labels = {v["label"] for v in results.values()}
    assert labels, "the matrix must exercise at least one severity"
    assert labels <= _ALLOWED_LABELS, (
        f"gateSeverity must only ever emit one of {_ALLOWED_LABELS}: {results}"
    )

    null_case = results["readiness_null"]
    assert null_case["key"] not in ("blocked", "awaiting_you"), (
        "with readiness:null (the board tile's input today), gateSeverity "
        f"must never escalate to blocked/awaiting_you — got {null_case}"
    )


def test_banner_and_step_row_consume_the_same_gate_severity():
    """The banner and StepRail's gate row must be source-proven to derive
    from the SAME function fed the SAME gateReadiness data, so they cannot
    legitimately disagree — this is what makes AC-3's cross-surface
    agreement a structural property rather than a coincidence."""
    task_detail_src = _read(_TASK_DETAIL)
    step_rail_src = _read(_STEP_RAIL)
    plan_view_src = _read(_PLAN_VIEW)

    assert "gateSeverity(" in task_detail_src, (
        "TaskDetailPage.tsx must call the shared gateSeverity()"
    )
    assert "gateSeverity" in step_rail_src, (
        "StepRail.tsx must consume gateSeverity (directly, or via a "
        "gateSeverity result passed in as a prop) so its gate row's pill "
        "cannot diverge from the banner's verdict"
    )
    assert re.search(r"<StepRail[\s\S]{0,400}?gateReadiness", plan_view_src), (
        "PlanView.tsx must pass the SAME gateReadiness prop it already "
        "receives (:210,239) into <StepRail ...> (:535), so the banner and "
        "the step row derive severity from identical input"
    )


def test_conductor_tile_handoff_uses_shared_severity():
    """ConductorPage's at-gate handoff line is the tile-side consumer named
    in D-3; it must call the same gateSeverity() (with readiness:null, so it
    is structurally pinned to PENDING by test_gate_severity_matrix above),
    not a private "paused at gate" string re-derived independently. The
    activity pill (ACT_TILE) itself is deliberately UNTOUCHED — it classifies
    drive liveness, not gate severity — so this checks only the handoff
    line's own source, never ACT_TILE's labels."""
    src = _read(_CONDUCTOR_PAGE)
    assert "gateSeverity" in src, (
        "ConductorPage.tsx must consume the shared gateSeverity derivation "
        "for its at-gate handoff copy so the tile cannot re-diverge from "
        "the banner/step-row vocabulary"
    )
