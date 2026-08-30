"""The INFERENCE half of a gate seat: a real `claude -p` adjudicator.

Owner, 2026-08-29: "the gate itself is its own state the conductor bot can
hand a task to, and the gate should be working a -p claude instance that is
the adjudicator", and on how the two halves relate: "it should be inferred
AND rubric. the idea of every state a bot visits is to make it the MOST
deterministic it can be by codifying things as much as it can, and leaving
room for inference to deal with unknowns."

So this module is deliberately the SECOND layer, never the first:

  1. DETERMINISTIC FIRST. Everything codifiable is decided by code --
     the EvidenceReceipt teeth, the story/plan rubrics, the plan_gate
     checks in services/plan_gate_checks.py, the abstain-only teeth in
     conductor_service.adjudicate_green_gate. If any of them REFUSES,
     this module does not run at all and never sees the task. Inference
     may not overrule a codified refusal -- that is what makes the
     codified half worth having.
  2. INFERENCE FOR THE RESIDUE. When the codified checks are satisfied
     and what remains is judgment a rubric cannot express -- is this
     plan actually answering this story, does this evidence actually
     demonstrate this oracle -- a real agent reads the packet and
     decides.

Two properties follow from that ordering and are enforced below:

  READ-ONLY. The adjudicator gets Read/Glob/Grep and NOTHING that writes.
  A seat that edits the tree it is judging is the bug fixed in 7.13.171/172,
  where minting evidence dirtied the very worktree the cleanliness tooth
  then rejected. The reviewer may look; it may not touch.

  DISTINCT ACTOR. It reports as its own seat id, never as the producing
  session, so the conductor's no-self-review rule still holds.

OFF BY DEFAULT. Set PRISM_GATE_AGENT_ENABLED=1 to opt in, matching the
existing opt-in posture of the machine seats (owner 2026-07-15: human
clicks stay the norm until an environment asks for otherwise).
"""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any, Optional

SEAT_ID = "conductor-adjudicator-agent"

# Look, never touch. See the module docstring.
REVIEW_TOOLS = ("Read", "Glob", "Grep")

_TRUE = {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    return str(os.environ.get("PRISM_GATE_AGENT_ENABLED", "")
               ).strip().lower() in _TRUE


def _max_turns() -> int:
    try:
        return max(1, int(os.environ.get("PRISM_GATE_AGENT_MAX_TURNS", "6")))
    except (TypeError, ValueError):
        return 6


def _max_budget_usd() -> float:
    try:
        return max(0.05, float(
            os.environ.get("PRISM_GATE_AGENT_MAX_BUDGET_USD", "0.75")))
    except (TypeError, ValueError):
        return 0.75


def _timeout_s() -> float:
    try:
        return max(30.0, float(
            os.environ.get("PRISM_GATE_AGENT_TIMEOUT_S", "300")))
    except (TypeError, ValueError):
        return 300.0


def _field(task: Any, name: str, default: str = "") -> str:
    value = getattr(task, name, None)
    return str(value) if value not in (None, "") else default


def build_packet(task: Any, step_id: str) -> str:
    """The evidence the adjudicator judges. Everything here is already on
    the task -- this seat introduces no new source of truth."""
    parts = [
        f"# Gate: {step_id}",
        f"## Task\n{_field(task, 'title', '(untitled)')}",
        f"## Oracle\n{_field(task, 'oracle', '(none recorded)')}",
        f"## Likely misfire\n{_field(task, 'likely_misfire', '(none recorded)')}",
    ]
    plan = _field(task, "plan_doc")
    if plan:
        parts.append(f"## Story / plan under review\n{plan}")
    proof = _field(task, "completion_proof")
    if proof:
        parts.append(f"## Completion proof on file\n{proof}")
    verify = getattr(task, "verify", None)
    if verify:
        parts.append("## Pinned suite\n" + "\n".join(
            f"- {v}" for v in verify))
    return "\n\n".join(parts)


PROMPT = """You are the ADJUDICATOR seat on a PRISM conductor gate.

Every mechanically checkable property of this gate HAS ALREADY PASSED in
code before you were called: the evidence receipt, the rubric, and the
gate's deterministic teeth. Do not re-run them and do not approve merely
because they passed -- that is already established.

Your job is the part a rubric cannot express. Read the packet below and
answer ONE question: does this evidence actually demonstrate THIS task's
oracle, or does it only look like it does? Consider the recorded
likely_misfire as the specific way this could pass while being wrong.

You have READ-ONLY tools. Do not attempt to modify anything.

Reply with ONLY a JSON object, no prose around it:
{{"verdict": "approve" | "reject", "reason": "<one or two sentences>"}}

Choose "reject" if the evidence is consistent with the misfire, if it
demonstrates something adjacent to the oracle rather than the oracle
itself, or if you cannot tell from what is here. An honest reject that
names what is missing is far more useful than a generous approve.

{packet}
"""

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_verdict(text: str) -> Optional[dict]:
    """The agent's JSON verdict, or None when it cannot be read.

    Unparseable output is NOT an approval -- it is no decision at all, and
    the caller leaves the gate pending. A seat that fails open is a seat
    that rubber-stamps whatever it could not understand.
    """
    if not str(text or "").strip():
        return None
    match = _JSON_RE.search(str(text))
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("approve", "reject"):
        return None
    return {"verdict": verdict,
            "reason": str(data.get("reason") or "").strip()}


_BEHAVIOUR_FOR_GATE = {
    "story_gate": "story-gate-check", "plan_gate": "plan-gate-check",
    "red_gate": "red-gate-status", "green_gate": "green-gate-status",
}


def _flow_version(project: str, step_id: str) -> Optional[int]:
    """The version of the behaviour definition this gate is running."""
    behaviour = _BEHAVIOUR_FOR_GATE.get(step_id)
    if not behaviour:
        return None
    try:
        import json as _json
        from pathlib import Path as _Path
        from prism_service.services.claude_transcripts import _project_source_path
        doc = _json.loads((_Path(_project_source_path(project))
                           / ".prism" / "behaviors" / "conductor"
                           / f"{behaviour}.json").read_text(encoding="utf-8"))
        return int(doc.get("version"))
    except Exception:
        return None


def adjudicate(project: str, task_id: str, step_id: str,
               *, invoke=None, decide=None) -> Optional[dict]:
    """Run the inference seat on ONE gate and record its verdict.

    THE CALLER MUST HAVE RUN THE DETERMINISTIC HALF FIRST and found no
    refusal. This function does not re-check codified properties and must
    never be reached with one outstanding -- see the module docstring.

    Returns the recorded decision, or None when no decision was made (seat
    disabled, invocation failed, or output unreadable). None always means
    the gate stays exactly as it was; it never means approve.
    """
    if not is_enabled():
        return None

    from prism_service.project_context import get_project
    ctx = get_project(project)
    task = ctx.task_svc.get(task_id)
    if task is None:
        return None

    # A demo/review proof_type green_gate is HUMAN-ONLY by owner rule
    # eaafdf75, and the machine seat abstains on it BY DESIGN. Inference
    # does not get to take that decision away from the person either --
    # this seat exists to judge what a rubric cannot express, not to
    # relieve a human of a judgement that was theirs on purpose.
    proof_type = str(getattr(task, "proof_type", "") or "").strip().lower()
    if step_id == "green_gate" and proof_type in ("demo", "review"):
        return None

    packet = build_packet(task, step_id)

    from prism_service.services import task_workspace
    work_dir = (task_workspace.workspace_for(task_id) or {}).get("path")
    if not work_dir:
        return None

    if invoke is None:
        from prism_service.inference import claude_cli
        invoke = claude_cli.invoke

    # The step does REAL work now, so its progress is real: the heartbeat is
    # what makes the flow node's clock and the board's activity honest for a
    # gate, which they could not be while a gate did nothing at all.
    try:
        from prism_service.services import drive_heartbeat
        drive_heartbeat.record_heartbeat(
            getattr(ctx, "scores_db", None) or "", {
                "task_id": task_id, "step": step_id, "elapsed_s": 0,
                "last_tool": "claude_cli.invoke", "work_units": 1})
    except Exception:
        pass

    try:
        result = invoke(
            PROMPT.format(packet=packet), work_dir=work_dir,
            plugin_dir=work_dir, max_turns=_max_turns(),
            max_budget_usd=_max_budget_usd(), timeout_s=_timeout_s(),
            allowed_tools=REVIEW_TOOLS, project=project,
            purpose=f"gate-agent@{step_id}#{task_id[:8]}",
            session_id=str(uuid.uuid4()))
    except Exception:
        return None

    verdict = parse_verdict(getattr(result, "text", None) or str(result or ""))
    if verdict is None:
        return None

    # STAMP THE FLOW VERSION THIS RAN AGAINST. An instance belongs to the
    # version of the flow that executed it -- a run of plan-gate-check v1
    # (one opaque rubric callback) is not a run of v3 (rubric + three teeth
    # + infer), and without this the two are indistinguishable afterwards.
    # Owner 2026-08-29: "it is INSTANCE ran per THIS version of the
    # Bot/Agentic flow." Nothing stamped this before: 2,016 gate_decide rows
    # on file, zero carrying a flow_version, which is why every historical
    # instance reads as version UNKNOWN rather than being back-attributed.
    fv = _flow_version(project, step_id)
    stamp = f" flow_version={fv}" if fv is not None else ""
    reason = (f"inference seat ({SEAT_ID}){stamp}: {verdict['reason']}"
              if verdict["reason"] else f"inference seat ({SEAT_ID}){stamp}")

    if decide is None:
        decide = ctx.conductor_svc.gate_decide
    try:
        # conductor_service.gate_decide(task_id, action, reason=..., ...):
        # `action` is the verdict word itself, and `actor` is what the
        # no-self-review rule reads -- this seat names itself there so it
        # can never be mistaken for the session that produced the work.
        return decide(task_id, verdict["verdict"], reason=reason,
                      actor=SEAT_ID, model="claude-cli")
    except Exception:
        return None
