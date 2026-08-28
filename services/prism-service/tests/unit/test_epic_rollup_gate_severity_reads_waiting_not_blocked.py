"""An epic-rollup gate waiting on unfinished children must never read
BLOCKED -- that word is the same alarm-vocabulary mistake this codebase has
fixed before for activity_for's "idle"/"stalled" states (owner 2026-07-21:
"idle and stalled are ALARM words -- the owner reads them as 'I must
intervene somewhere'. Render them ONLY when the owner actually has an
action.") An epic-rollup block is never something the owner can act on --
they cannot force a child task to finish -- so gateSeverity's four-label
vocabulary (READY/PENDING/AWAITING YOU/BLOCKED) was overloading BLOCKED onto
two very different situations: "the gate genuinely cannot proceed and needs
a human" (verifier_refused, no-evidence) and "normal epic-rollup waiting,
which may well be actively driven underneath" (task 95474ec7, live,
owner: "no it reads blocked as best i can tell", pointing at StepRail's
green_gate pill reading BLOCKED while a great-grandchild was mid-implement).

This adds a FIFTH label, "WAITING ON CHILDREN" (key "waiting_on_children"),
scoped ONLY to readiness.receipt.adapter === "epic-rollup" with a non-empty
blocking_children list -- every other blocked path (verifier_refused, the
generic no-evidence fallback) keeps the real BLOCKED word, unchanged.
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
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_STEP_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"
_GATE_SEVERITY = _SRC / "lib" / "gateSeverity.ts"

_SEVERITY_OPEN = "// --- GATE-SEVERITY-BLOCK-START ---"
_SEVERITY_CLOSE = "// --- GATE-SEVERITY-BLOCK-END ---"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed; cannot execute the marker block")
    return exe


def _marker_block(src: str, open_marker: str, close_marker: str, where: str) -> str:
    if open_marker not in src or close_marker not in src:
        pytest.fail(f"{where} must delimit its logic with {open_marker} ... {close_marker}")
    body = src.split(open_marker, 1)[1].split(close_marker, 1)[0]
    assert body.strip(), f"the marker block in {where} is empty"
    return body


def _run_node(block: str, script_tail: str) -> str:
    script = f"{block}\n{script_tail}\n"
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mjs"
        probe.write_text(script, encoding="utf-8")
        proc = subprocess.run([_node(), str(probe)], capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        pytest.fail(f"marker block failed under node (rc={proc.returncode}):\n{proc.stderr}\n{proc.stdout}")
    return proc.stdout.strip()


def _gate_severity_block() -> str:
    src = _read(_GATE_SEVERITY)
    return _marker_block(src, _SEVERITY_OPEN, _SEVERITY_CLOSE, "gateSeverity.ts")


_MATRIX = {
    "epic_rollup_blocked": {"gate_state": "pending", "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "epic-rollup"},
        "blocking_children": [{"id": "c1", "title": "child"}]}},
    "verifier_refused": {"gate_state": "pending", "verifier_refused": True, "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "green"}}},
    "generic_no_evidence": {"gate_state": "pending", "readiness": {
        "receipt_ok": False, "receipt": {"adapter": "green"}}},
}


def test_epic_rollup_blocked_reads_waiting_on_children_not_blocked():
    block = _gate_severity_block()
    script_tail = f"""
const matrix = {json.dumps(_MATRIX)};
const out = {{}};
for (const [name, snap] of Object.entries(matrix)) out[name] = gateSeverity(snap);
console.log(JSON.stringify(out));
"""
    results = json.loads(_run_node(block, script_tail))
    rollup = results["epic_rollup_blocked"]
    assert rollup["label"] != "BLOCKED", (
        f"an epic-rollup gate waiting on unfinished children must not read "
        f"the alarm word BLOCKED -- the owner cannot act on it directly, "
        f"got {rollup}")
    assert rollup["key"] not in ("blocked",), f"got {rollup}"

    # Genuine blocks (something IS wrong, the owner's action is the fix)
    # must be completely unaffected by this change.
    assert results["verifier_refused"]["label"] == "BLOCKED", results
    assert results["generic_no_evidence"]["label"] == "BLOCKED", results


def test_step_rail_pending_gate_pill_reads_the_new_label():
    """StepRail's gate row pill (task 95474ec7's live symptom, exactly) must
    render whatever label gateSeverity computes -- so fixing gateSeverity.ts
    alone must be sufficient; nothing in StepRail may re-hardcode BLOCKED."""
    src = _read(_STEP_RAIL)
    assert "gateSeverity(" in src, "StepRail must call the shared gateSeverity()"
    assert re.search(r'"BLOCKED"', src) is None, (
        "StepRail must never hardcode the literal \"BLOCKED\" -- it renders "
        "gateSeverity(...).label verbatim, or it can re-diverge from the "
        "shared vocabulary")


def test_task_detail_epic_rollup_headline_drops_the_alarm_word():
    """TaskDetailPage.tsx special-cases the epic-rollup headline with its
    own literal (naming the blocking-child count) -- that literal must not
    lead with BLOCKED either, matching the same reasoning as gateSeverity's
    own fix."""
    src = _read(_TASK_DETAIL)
    m = re.search(
        r'`([^`]*\$\{gateReadiness!\.blocking_children!\.length\}[^`]*)`',
        src,
    )
    assert m, "expected the epic-rollup blocking_children headline literal to still exist"
    literal = m.group(1)
    assert not literal.upper().startswith("BLOCKED"), (
        f"the epic-rollup headline must not lead with the alarm word "
        f"BLOCKED -- got {literal!r}")
