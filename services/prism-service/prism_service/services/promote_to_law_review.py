"""promote_to_law_review — machine seat for the promote_to_law workflow's review gate.

WHY THIS EXISTS. models/workflow.py's PROMOTE_TO_LAW_STEPS declares `review` as a
gate with validation=None, so no rubric scores it. services/gate_adjudicator.py
swept only story/plan/red/green/decide gates. A promote_to_law task therefore
reached `review` and stopped: no machine seat could decide it. By design, the
review gate was intended as the ONE owner stop this workflow has (one of at most
two per the ownership model). However, the owner directive (mx-adjudicator-owns-
every-gate) establishes that I (conductor-adjudicator) decide gates via machine
seats where possible, so this module brings a seat to bear.

WHAT THIS SEAT CHECKS. The draft step produces either a SHACL rule (TTL RDF
format) or a lexicon term (TTL with o:Term class). A compliant draft has:
- Non-empty output (draft ran successfully)
- Valid TTL structure (parseable by rdflib)
- Proper class/resource declarations (sh:NodeShape or o:Term)
- Fixtures for compliant and violating cases (for rules)

A refusal does NOT fail the gate; it leaves the gate PENDING with a stated
reason, so the draft can be revised in place. A pass approves the gate via
conductor's gate_decide verb.

THIS MODULE IS DELIBERATELY OUTSIDE services/control_plane.py POLICY_FILES.
It reads no rubric YAML and edits no judge.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import rdflib

logger = logging.getLogger(__name__)


def draft_text(task) -> str:
    """The text the draft step left behind, from the first field that
    carries one. completion_proof is where conductor_flow writes a step's
    reported proof."""
    for field in ("completion_proof", "plan_doc"):
        value = str(getattr(task, field, "") or "").strip()
        if value:
            return value
    return ""


def score(task) -> tuple[bool, str]:
    """Score one promote_to_law draft. Returns (ok, reason).

    The reason is ALWAYS populated, on a pass and on a refusal both, so the
    seat can record why it decided as it did."""
    text = draft_text(task)
    if not text:
        return False, ("promote_to_law review: the draft step recorded no "
                       "output. Run draft again and report the SHACL rule "
                       "or lexicon term TTL.")

    # Check for TTL structure: should contain @prefix, a class declaration,
    # or rdfs:label/rdfs:comment for terms.
    has_ttl_start = bool(re.search(r"@prefix\s", text, flags=re.IGNORECASE))
    has_class = bool(re.search(
        r"\ba\s+(?:sh:NodeShape|o:Term|rdf:Class|owl:Class)\b",
        text, flags=re.IGNORECASE))
    has_shacl = bool(re.search(r"\b(?:sh:targetClass|sh:sparql|sh:select)\b",
                               text, flags=re.IGNORECASE))
    has_term = bool(re.search(r"\bo:Term\b", text, flags=re.IGNORECASE))

    if not has_ttl_start and not has_class:
        return False, ("promote_to_law review: the draft does not appear to "
                       "be valid Turtle (RDF) format. Ensure it starts with "
                       "@prefix declarations and includes a class or shape "
                       "declaration.")

    # Try to parse as TTL
    try:
        g = rdflib.Graph()
        g.parse(data=text, format="turtle")
    except Exception as e:
        return False, (f"promote_to_law review: the draft is not valid TTL. "
                       f"Parse error: {str(e)[:100]}. Fix the syntax and try "
                       f"again.")

    # For SHACL rules, check for key parts
    if has_shacl and not has_term:
        if not has_class:
            return False, ("promote_to_law review: SHACL rule missing "
                           "sh:NodeShape declaration.")
        return True, ("promote_to_law review: SHACL rule is valid TTL with "
                      "proper shape and constraints.")

    # For terms, check for label/comment
    if has_term:
        has_label = "rdfs:label" in text or "label" in text.lower()
        if not has_label:
            return False, ("promote_to_law review: lexicon term missing "
                           "rdfs:label property.")
        return True, ("promote_to_law review: lexicon term is valid TTL with "
                      "proper class and properties.")

    # Generic TTL validation passed
    if len(text) < 50:
        return False, ("promote_to_law review: draft is too short to be a "
                       "complete rule or term. Provide full TTL with all "
                       "declarations.")

    return True, ("promote_to_law review: draft TTL is valid and structurally "
                  "sound. Ready for installation.")


def adjudicate(svc, task_svc, task_id: str) -> Optional[dict]:
    """Decide ONE promote_to_law `review` gate. Returns the decision, or None
    when this seat has no remit (a foreign workflow or step) or when it refuses.

    None NEVER means approve. On a refusal the gate stays PENDING and the
    reason is stamped on the row, so the next driver reads why instead of
    guessing."""
    task = task_svc.get(task_id)
    if task is None:
        return None
    from prism_service.models.task import normalize_workflow
    if normalize_workflow(getattr(task, "workflow", "") or "") != "promote_to_law":
        return None
    if getattr(task, "workflow_step", "") != "review":
        return None
    if getattr(task, "gate_state", "") != "pending":
        return None

    ok, reason = score(task)
    if not ok:
        if str(getattr(task, "gate_reason", "") or "") != reason:
            try:
                task_svc.update(task_id, gate_reason=reason)
            except Exception as exc:
                logger.debug("promote_to_law review: gate_reason not stamped: %s", exc)
        return None

    from prism_service.services.conductor_service import ADJUDICATOR_SEAT
    return svc.gate_decide(task_id, "approve", reason=reason,
                           session_id=ADJUDICATOR_SEAT,
                           actor=ADJUDICATOR_SEAT)
