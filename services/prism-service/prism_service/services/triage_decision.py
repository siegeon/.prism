"""triage_decision -- the machine seat for the triage workflow's `decide` gate.

WHY THIS EXISTS. models/workflow.py's TRIAGE_STEPS declares `decide` as a
gate with validation=None, so no rubric scores it. services/gate_agent.py's
`_BEHAVIOUR_FOR_GATE` mapped only story/plan/red/green, and
services/gate_adjudicator.py swept only those same four steps. A triage task
therefore reached `decide` and stopped: no machine seat could decide it, and
api/conductor_flow.py's distinct-actor tooth refuses every session already
linked to the row. Observed live on task edeab040 (2026-09-09), where the
task page rendered no approve control at all.

WHAT THIS IS NOT. It is not a rubber stamp. A gate with nothing to check
must not auto-pass; that is worse than a visible stall, because a stall is
visible and a stamp is not. So this module scores the CLASSIFICATION the
`classify` step produced, and REFUSES when that classification is missing,
bucket-less, or unreasoned.

THIS MODULE IS DELIBERATELY OUTSIDE services/control_plane.py POLICY_FILES.
It reads no rubric YAML and edits no judge. It calls ConductorService only
through the public gate_decide entry point.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# The four buckets the triage workflow's classify step may assign. Named in
# models/workflow.py's TRIAGE_STEPS comment: "bucket Open/Monitoring/
# Resolved/Dropped with a one-line reason".
BUCKETS: tuple[str, ...] = ("open", "monitoring", "resolved", "dropped")

# A classification has to say something beyond naming a bucket. This is a
# floor on substance, not a quality score -- the seat refuses an empty or
# one-word rationale and leaves everything else to the reader.
MIN_REASON_CHARS = 40


def classification_text(task) -> str:
    """The text the classify step left behind, from the first field that
    carries one. completion_proof is where conductor_flow writes a step's
    reported proof; the other two are read as fallbacks so a driver that
    recorded its bucket elsewhere still scores."""
    for field in ("completion_proof", "premise_notes", "plan_doc"):
        value = str(getattr(task, field, "") or "").strip()
        if value:
            return value
    return ""


def named_buckets(text: str) -> list[str]:
    """Every bucket named in the text, matched on a word boundary so
    'dropped' inside a sentence counts and 'openly' does not."""
    found = []
    for bucket in BUCKETS:
        if re.search(rf"\b{bucket}\b", text, flags=re.IGNORECASE):
            found.append(bucket)
    return found


def score(task) -> tuple[bool, str]:
    """Score one triage classification. Returns (ok, reason).

    The reason is ALWAYS populated, on a pass and on a refusal both, so the
    seat can record why it decided as it did. A tooth that computes a
    refusal and then discards it leaves a driver with an empty gate_reason
    and nothing to act on (task e0149f1f)."""
    text = classification_text(task)
    if not text:
        return False, ("triage decide: the classify step recorded no "
                       "classification. Report the bucket and a reason at "
                       "classify, then this gate can decide.")
    found = named_buckets(text)
    if not found:
        return False, ("triage decide: the classification names no bucket. "
                       f"Name exactly one of {', '.join(BUCKETS)}.")
    if len(found) > 1:
        return False, ("triage decide: the classification is ambiguous. It "
                       f"names {len(found)} buckets ({', '.join(found)}). "
                       "Name exactly one.")
    if len(text) < MIN_REASON_CHARS:
        return False, ("triage decide: the classification names a bucket "
                       f"({found[0]}) but gives no reason. Say why in at "
                       f"least {MIN_REASON_CHARS} characters.")
    return True, (f"triage decide: classified '{found[0]}' with a stated "
                  f"reason ({len(text)} chars). Machine review passed.")


def adjudicate(svc, task_svc, task_id: str) -> Optional[dict]:
    """Decide ONE triage `decide` gate. Returns the decision, or None when
    this seat has no remit (a foreign workflow or step) or when it refuses.

    None NEVER means approve. On a refusal the gate stays PENDING and the
    reason is stamped on the row, so the next driver reads why instead of
    guessing -- a failed gate would need a human to reopen it, and the
    classification is fixable in place."""
    task = task_svc.get(task_id)
    if task is None:
        return None
    from prism_service.models.task import normalize_workflow
    if normalize_workflow(getattr(task, "workflow", "") or "") != "triage":
        return None
    if getattr(task, "workflow_step", "") != "decide":
        return None
    # Accept both pending and failed gates. A FAILED gate from a machine/config
    # refusal is resweppable; the sweep already checks _failed_gate_is_refused_approve
    # to distinguish human rejects (never re-sweep) from machine refusals (resweppable).
    # We filter to those two states; a passed gate stays decided and is never touched.
    if getattr(task, "gate_state", "") not in ("pending", "failed"):
        return None

    ok, reason = score(task)
    if not ok:
        if str(getattr(task, "gate_reason", "") or "") != reason:
            try:
                task_svc.update(task_id, gate_reason=reason)
            except Exception as exc:
                logger.debug("triage decide: gate_reason not stamped: %s", exc)
        return None

    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
    return svc.gate_decide(task_id, "approve", reason=reason,
                           session_id=ADJUDICATOR_SEAT,
                           actor=ADJUDICATOR_SEAT)
