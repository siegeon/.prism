"""Gate banner quotes the live gate's real reason (task a646cbd1).

THE BUG: GET /api/conductor/gate/readiness (api/conductor.py:gate_readiness)
has special-cased branches for plan_gate, red_gate, epic roll-up and
human-judgment demo/review proof_type — but NO story_gate branch. A task
parked at story_gate with a rubric refusal (e.g. "story_complete: no
acceptance criteria with AC-<n> ids found") falls through to the generic
tail, which evaluates the GREEN-GATE-shaped EvidenceReceipt oracle tooth —
the wrong evaluation entirely for a rubric gate.

On the SPA side, TaskDetailPage.tsx's banner headline has exactly 4
hardcoded strings (AWAITING YOUR APPROVAL / AWAITING YOUR REVIEW / READY /
BLOCKED-generic). A story_gate rubric refusal falls to the generic
"BLOCKED · evidence not on file" even though the real rubric reason string
is sitting in gateReadiness.receipt_refusal — never surfaced. The epic
green_gate roll-up refusal ("N child task(s) not done") never names WHICH
child. And the collapsed "audit detail" block renders the STALE stored
task.gate_reason unconditionally, even when it says something inviting
("the evidence is ready; review it and Approve") while live readiness
disagrees.

CONVENTION (mx-a31a3d, task 0c49c385): key new UI branches on the live
receipt/adapter shape (gateReadiness.receipt.adapter), never on
task.workflow_step string equality, so the fix generalizes to future
rubric-shaped gates. Test file follows test_gate_banner_honest_state_ui.py's
source-reading convention (balanced-brace JSX extraction) — the SPA has no
JS test runner.

Pins, RED-first (ALL FAIL against current source):
  AC-1  gate_readiness for a story_gate task with a rubric refusal returns
        adapter="story-rubric" and a reason EQUAL to the live
        ConductorService._verify_rubric_gate(task, "story_complete") verdict
        (not the green_gate EvidenceReceipt oracle refusal).
  AC-2  gate_readiness for a story_gate task with a COMPLIANT story returns
        receipt_ok=True, adapter="story-rubric" (the affirmative case is
        preserved, not just the refusal).
  AC-3  gate_readiness for an epic green_gate blocked on incomplete children
        returns blocking_children: [{id, title}, ...] naming the specific
        unfinished child(ren), not just prose.
  AC-4  TaskDetailPage.tsx's headline expression gets a story-rubric branch
        keyed on receipt.adapter === "story-rubric" (never workflow_step
        equality), quoting the live reason instead of falling to the
        generic BLOCKED strings.
  AC-5  that story-rubric pending state renders an amber (not rose) tone —
        a machine-owned, healthily-progressing rubric gate is not the same
        as a genuine stuck-blocked state.
  AC-6  the epic-rollup-blocked headline/panel links the blocking child via
        an EntityChip to=`/tasks/${id}`, using gateReadiness.blocking_children.
  AC-7  the collapsed "audit detail" block consults live gateReadiness
        (not just the stale stored task.gate_reason) so an inviting stored
        string can't leak past a live refusal unqualified.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the JSX `{...}` expression starting at the first `{` after
    `marker`, matched by BRACE DEPTH — never a fixed slice — so a comment
    or unrelated literal near the marker can't satisfy the assertion in
    place of the real guard (repeat failure mode, see
    test_gate_banner_honest_state_ui.py)."""
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


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


_NO_AC_STORY = """# Summary
Fix the gate banner.

# Requirements
Quote the live reason.

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


def _story_gate_task(task_svc, plan_doc: str):
    t = task_svc.create(title="story gate rubric task", tags=["conductor"],
                        oracle="pytest tests/unit/test_x.py -q",
                        proof_type="test",
                        likely_misfire="looks fine but is not")
    task_svc.update(t.id, workflow_step="story_gate", gate_state="pending",
                    plan_doc=plan_doc)
    return task_svc.get(t.id)


# ---------------------------------------------------------------------------
# AC-1 / AC-2 — GET /api/conductor/gate/readiness at story_gate
# ---------------------------------------------------------------------------


def test_story_gate_readiness_reflects_live_rubric_refusal(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    t = _story_gate_task(task_svc, _NO_AC_STORY)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    live = cond._verify_rubric_gate(t, "story_complete")
    assert live["verified"] is False, live  # sanity: fixture is non-compliant

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is False, out
    assert out["receipt"]["adapter"] == "story-rubric", out
    # the reason must be the LIVE rubric verdict, not the green_gate
    # EvidenceReceipt oracle refusal (which never mentions "acceptance
    # criteria" — it talks about pytest specs / trees).
    assert out["receipt"]["reason"] == live["reason"], out
    assert out["receipt_refusal"] == live["reason"], out
    assert "AC-" in out["receipt_refusal"], out


def test_story_gate_readiness_ready_when_rubric_passes(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    t = _story_gate_task(task_svc, _COMPLIANT_STORY)
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    live = cond._verify_rubric_gate(t, "story_complete")
    assert live["verified"] is True, live  # sanity: fixture is compliant

    out = capi.gate_readiness(task_id=t.id, project="default")
    assert out["receipt_ok"] is True, out
    assert out["receipt"]["adapter"] == "story-rubric", out
    assert out["receipt_refusal"] == "", out


# ---------------------------------------------------------------------------
# AC-3 — epic green_gate roll-up names the specific blocking child
# ---------------------------------------------------------------------------


def test_epic_rollup_readiness_names_blocking_child(tmp_path, monkeypatch):
    from prism_service.api import conductor as capi
    task_svc, cond = _services(tmp_path)
    parent = task_svc.create(title="epic parent", tags=["conductor"],
                             oracle="two children finish", proof_type="test",
                             likely_misfire="rolls up without real evidence")
    task_svc.update(parent.id, workflow_step="green_gate", gate_state="pending")
    done_child = task_svc.create(title="finished slice", parent_id=parent.id,
                                 oracle="x", proof_type="test",
                                 likely_misfire="y")
    task_svc.update(done_child.id, status="done",
                    completion_proof="pytest tests/unit/test_a.py -q -> 3 passed")
    blocking_child = task_svc.create(title="still-in-flight slice",
                                     parent_id=parent.id, oracle="x",
                                     proof_type="test", likely_misfire="y")
    task_svc.update(blocking_child.id, status="in_progress")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=parent.id, project="default")
    assert out["receipt_ok"] is False, out
    assert out["receipt"]["adapter"] == "epic-rollup", out
    blockers = out.get("blocking_children") or []
    ids = {b["id"] for b in blockers}
    assert blocking_child.id in ids, out
    assert done_child.id not in ids, out
    named = next(b for b in blockers if b["id"] == blocking_child.id)
    assert named["title"] == "still-in-flight slice", named


# ---------------------------------------------------------------------------
# AC-4 / AC-5 — TaskDetailPage.tsx headline: story-rubric branch, amber tone
# ---------------------------------------------------------------------------


def test_headline_has_story_rubric_branch_keyed_on_adapter():
    src = _read(_TASK_DETAIL)
    expr = _extract_braced_expr(src, "● {")
    assert "story-rubric" in expr, (
        "the headline expression must branch on "
        "receipt.adapter === 'story-rubric' so a story_gate rubric refusal "
        "quotes its live reason instead of falling to the generic "
        "'BLOCKED · evidence not on file'"
    )
    # CONVENTION (mx-a31a3d): keyed on the receipt adapter, never on
    # task.workflow_step string equality — that is what stops this fix from
    # generalizing to the next rubric-shaped gate.
    assert 'workflow_step === "story_gate"' not in expr, (
        "the story-rubric headline branch must key on "
        "gateReadiness.receipt.adapter, not task.workflow_step equality "
        "(mx-a31a3d convention)"
    )


def test_is_story_rubric_pending_keyed_on_receipt_adapter():
    src = _read(_TASK_DETAIL)
    m = re.search(r"const\s+isStoryRubricPending\s*=([\s\S]*?);\n", src)
    assert m, (
        "TaskDetailPage.tsx must define an isStoryRubricPending const the "
        "headline and bannerTone key on"
    )
    definition = m.group(1)
    assert "story-rubric" in definition, definition
    assert 'workflow_step === "story_gate"' not in definition, (
        "must key on receipt.adapter, not workflow_step (mx-a31a3d)"
    )


def test_headline_never_says_blocked_for_story_rubric_pending():
    src = _read(_TASK_DETAIL)
    expr = _extract_braced_expr(src, "● {")
    idx = expr.index("story-rubric")
    # Slice the branch guarded by the story-rubric condition (up to the next
    # ':' at the same nesting depth is overkill for this source; bound to a
    # generous window and require the alarm word BLOCKED is not the label
    # chosen for a machine-owned, healthily-progressing rubric gate).
    window = expr[max(0, idx - 40):min(len(expr), idx + 220)]
    assert "BLOCKED" not in window.upper() or "PENDING" in window.upper(), (
        "a story_gate rubric-pending state is machine-owned and healthily "
        "progressing (rubric re-sweep) — it must not read as the alarm "
        "word BLOCKED the way a genuinely stuck gate does (mx-a31a3d "
        "plan_gate precedent)"
    )


def test_banner_tone_amber_for_story_rubric_pending():
    src = _read(_TASK_DETAIL)
    m = re.search(r"const\s+bannerTone[^=]*=([\s\S]*?);\n", src)
    assert m, "TaskDetailPage.tsx must define bannerTone"
    definition = m.group(1)
    assert "isStoryRubricPending" in definition, (
        "bannerTone must key on isStoryRubricPending so the story-rubric "
        "pending state gets the calm amber tone, distinct from a genuine "
        "rose-tone stuck-blocked gate"
    )


# ---------------------------------------------------------------------------
# AC-6 — epic-rollup blocked headline names + links the blocking child
# ---------------------------------------------------------------------------


def test_gate_readiness_type_declares_blocking_children():
    src = _read(_TASK_DETAIL)
    m = re.search(r"type\s+GateReadiness\s*=\s*\{([\s\S]*?)\n\};", src)
    assert m, "TaskDetailPage.tsx must declare the GateReadiness type"
    body = m.group(1)
    assert "blocking_children" in body, (
        "GateReadiness must declare blocking_children (the epic-rollup "
        "adapter's named unfinished child list from gate/readiness) so the "
        "page can render a link, not just prose"
    )


def test_blocking_child_rendered_as_linked_entity_chip():
    src = _read(_TASK_DETAIL)
    assert "blocking_children" in src.split("type GateReadiness", 1)[-1], (
        "blocking_children must be consumed somewhere in the render tree"
    )
    # The blocking-child chip must reuse the SAME EntityChip -> /tasks/${id}
    # pattern already used for ordinary child-task links on this page
    # (line ~2530), not a bespoke unlinked prose render.
    m = re.search(
        r"blocking_children[\s\S]{0,400}?<EntityChip[^>]*to=\{`/tasks/\$\{[^}]*\}`\}",
        src,
    )
    assert m, (
        "the epic-rollup-blocked banner/panel must render an "
        "<EntityChip .../> linking to /tasks/${childId} for each named "
        "blocking child, within ~400 chars of consuming "
        "gateReadiness.blocking_children"
    )


# ---------------------------------------------------------------------------
# AC-7 — collapsed audit-detail never leaks a stale inviting gate_reason
# past a live refusal, unqualified
# ---------------------------------------------------------------------------


def test_audit_detail_block_consults_live_gate_readiness():
    src = _read(_TASK_DETAIL)
    expr = _extract_braced_expr(src, "audit detail (machine text)")
    assert "gateReadiness" in expr, (
        "the collapsed 'audit detail' block renders task.gate_reason — a "
        "STORED, potentially STALE field — unconditionally today. It must "
        "also consult the LIVE gateReadiness state (e.g. gate it behind "
        "gateReadiness.receipt_ok / gateVerdict agreement, or render the "
        "live receipt reason alongside it) so a stale inviting string "
        "('the evidence is ready; review it and Approve') can never be "
        "shown unqualified while the live gate/readiness evaluation "
        "disagrees and is in a refusal state"
    )
