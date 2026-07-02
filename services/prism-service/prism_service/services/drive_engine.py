"""Deterministic drive engine — the PI's planning-half state machine
(task a7d96437, C1 of the PI-orchestration build, parent 81b23574 FR-1).

Orchestration logic lives in CODE the PI calls, not prose it follows.
``DriveEngine.plan(task_id)`` walks a task through the planning half of
WORKFLOW_STEPS — review_previous_notes -> draft_story -> story_gate ->
verify_plan -> plan_gate — by calling ConductorService.advance_task /
gate_decide DIRECTLY in-process:

  * ZERO model round-trips on mechanical advance/gate steps;
  * authoring steps delegate to injectable seams (defaults: the C3
    plan scaffolder, whose model use is confined to C2 pi_slots) —
    at most ONE model bundle per authoring step, counted in stats;
  * DESYNC TOLERANT: an advance refused with "gate ... pending" folds
    into the gate handler and "already past / already at the final
    workflow step" continues forward — never a fatal halt (the known
    auto-advance desync, [[project_implement_verify_green_pending_gate_desync]]);
  * NO OVERRIDE CODE PATH: story_gate/plan_gate are approved only with
    concrete pre-shaped rubric evidence; a failed rubric gate stops the
    drive and surfaces the scorer's reason verbatim
    ([[feedback_gate_enforcement_doctrine]]);
  * BOUNDED: an iteration guard stops a non-progressing conductor and
    names the stuck step.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional

from prism_service.services import arc_governance as gov

# The planning half this engine owns. Order mirrors
# models/workflow.WORKFLOW_STEPS; plan_gate is the terminal state.
PLANNING_STEPS = (
    "review_previous_notes",
    "draft_story",
    "story_gate",
    "verify_plan",
    "plan_gate",
)
AUTHORING_STEPS = ("draft_story", "verify_plan")
GATE_STEPS = ("story_gate", "plan_gate")

# Iteration guard: the walk is 5 steps + gates; anything past this is a
# conductor that stopped making progress.
MAX_ITERATIONS = 24

AuthorFn = Callable[[Any, dict], tuple[str, int]]


class DriveEngine:
    """Planning-half orchestrator. ``conductor`` needs advance_task +
    gate_decide (duck-typed — tests wrap it with spies); ``task_svc``
    needs get/update."""

    def __init__(
        self,
        conductor: Any,
        task_svc: Any,
        *,
        model: Optional[Callable[..., str]] = None,
        memory_svc: Any = None,
        principles: Optional[list[dict]] = None,
        story_author: Optional[AuthorFn] = None,
        diagram_author: Optional[AuthorFn] = None,
    ) -> None:
        self._conductor = conductor
        self._task_svc = task_svc
        self._model = model
        self._memory_svc = memory_svc
        self._principles = principles
        self._story_author = story_author or self._default_story_author
        self._diagram_author = diagram_author or self._default_diagram_author
        self._build_cache: Optional[dict] = None

    # ------------------------------------------------------------------
    # Default authoring seams — C3 scaffolder, model confined to C2 slots
    # ------------------------------------------------------------------

    def _build(self, ctx: dict) -> dict:
        if self._build_cache is None:
            from prism_service.services import plan_scaffold

            self._build_cache = plan_scaffold.build_plan(
                ctx, model=self._model, memory_svc=self._memory_svc,
                principles=self._principles)
        return self._build_cache

    def _default_story_author(self, task: Any, ctx: dict) -> tuple[str, int]:
        return self._build(ctx)["plan_doc"], 1

    def _default_diagram_author(self, task: Any, ctx: dict) -> tuple[str, int]:
        # Reuse the diagram from the draft_story build: 0 extra model calls.
        return self._build(ctx)["plan_diagram"], 0

    # ------------------------------------------------------------------
    # Gate evidence — concrete receipts from the task's own artifacts
    # ------------------------------------------------------------------

    @staticmethod
    def _gate_evidence(gate_step: str, task: Any) -> str:
        doc = getattr(task, "plan_doc", "") or ""
        if gate_step == "story_gate":
            acs = sorted(set(re.findall(r"\bAC-\d+\b", doc)))
            oracles = doc.lower().count("oracle:")
            return (f"drive-engine rubric evidence: plan_doc carries "
                    f"{len(acs)} AC id(s) ({', '.join(acs) or 'none'}) with "
                    f"{oracles} inline oracle marker(s) and the rubric's "
                    "required sections; scored by score_story_complete.")
        diagram = getattr(task, "plan_diagram", "") or ""
        edges = gov.mermaid_edges(diagram)
        return (f"drive-engine rubric evidence: plan_diagram parses="
                f"{gov.mermaid_parses(diagram)} with {len(edges)} edge(s); "
                "plan_doc covers its own AC ids; scored by "
                "score_plan_coverage against the seeded principles.")

    # ------------------------------------------------------------------
    # Desync classification (FR-3)
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_refusal(reason: str) -> str:
        r = (reason or "").lower()
        if "gate" in r and "pending" in r:
            return "gate_pending"
        if "already past" in r or "already at the final" in r:
            return "already_past"
        return "fatal"

    # ------------------------------------------------------------------
    # The walk
    # ------------------------------------------------------------------

    def plan(self, task_id: str, session_id: str = "") -> dict:
        stats: dict[str, Any] = {
            "advances": 0, "authoring_steps": 0, "model_calls": 0,
            "overrides": 0, "gates": {}, "steps": [],
        }
        authored: set[str] = set()
        last_to_step = ""

        def _result(ok: bool, task: Any, reason: str = "") -> dict:
            out = {
                "ok": ok,
                "task_id": task_id,
                "final_step": getattr(task, "workflow_step", "") if task else "",
                "gate_state": getattr(task, "gate_state", "") if task else "",
                "stats": stats,
            }
            if reason:
                out["reason"] = reason
            return out

        for _ in range(MAX_ITERATIONS):
            task = self._task_svc.get(task_id)
            if task is None:
                return _result(False, None, f"unknown task {task_id!r}")
            step = task.workflow_step or ""
            stats["steps"].append(step or "<start>")

            # Terminal: plan_gate decided.
            if step == "plan_gate" and task.gate_state == "passed":
                return _result(True, task)
            if step in GATE_STEPS and task.gate_state == "failed":
                # Latched rubric failure: stop and report — NEVER override
                # (FR-4, gate doctrine). Recovery is a human/author fix.
                return _result(False, task,
                               getattr(task, "gate_reason", "") or
                               f"{step} latched failed")
            if step not in PLANNING_STEPS and step:
                # Already past the planning half — nothing left to drive.
                return _result(True, task,
                               f"task already past planning at {step!r}")

            ctx = {"feature_ask": getattr(task, "title", "") or "",
                   "title": getattr(task, "title", "") or ""}

            # Authoring steps: at most one seam call each (FR-2).
            if step in AUTHORING_STEPS and step not in authored:
                if step == "draft_story":
                    doc, calls = self._story_author(task, ctx)
                    self._task_svc.update(task_id, plan_doc=doc)
                else:
                    diagram, calls = self._diagram_author(task, ctx)
                    self._task_svc.update(task_id, plan_diagram=diagram)
                stats["authoring_steps"] += 1
                stats["model_calls"] += int(calls)
                authored.add(step)

            # Gate steps: approve with pre-shaped evidence, no override.
            if step in GATE_STEPS and task.gate_state == "pending":
                task = self._task_svc.get(task_id)  # fresh artifacts
                res = self._conductor.gate_decide(
                    task_id, action="approve",
                    reason=self._gate_evidence(step, task),
                    session_id=session_id or None,
                )
                stats["gates"][step] = {
                    "ok": bool(res.get("ok")),
                    "gate_state": res.get("gate_state"),
                }
                if not res.get("ok"):
                    fresh = self._task_svc.get(task_id)
                    return _result(False, fresh,
                                   str(res.get("reason", ""))
                                   or f"{step} refused")
                if step == "plan_gate":
                    # Terminal: gate_decide auto-advances PAST the gate,
                    # so success is detected here, not on a re-read.
                    return {
                        "ok": True,
                        "task_id": task_id,
                        "final_step": "plan_gate",
                        "gate_state": str(res.get("gate_state") or "passed"),
                        "stats": stats,
                    }
                continue  # gate_decide auto-advanced past the gate

            # Mechanical advance (0 model calls).
            adv = self._conductor.advance_task(
                task_id,
                validation=f"drive-engine: leaving {step or '<start>'}",
                session_id=session_id or None,
            )
            stats["advances"] += 1
            last_to_step = str(adv.get("to_step") or "") or last_to_step
            if not adv.get("ok"):
                kind = self._classify_refusal(str(adv.get("reason", "")))
                if kind == "gate_pending":
                    continue  # fold into the gate handler next iteration
                if kind == "already_past":
                    continue  # harmless desync — keep walking
                fresh = self._task_svc.get(task_id)
                return _result(False, fresh,
                               f"advance refused at {step or '<start>'}: "
                               f"{adv.get('reason', '')}")
            if adv.get("to_step") == step and step:
                # Progress check rides the guard loop: same step returned
                # ok repeatedly will exhaust MAX_ITERATIONS below.
                continue

        fresh = self._task_svc.get(task_id)
        stuck = (getattr(fresh, "workflow_step", "") if fresh else "") \
            or last_to_step
        return _result(False, fresh,
                       f"iteration guard: no progress after "
                       f"{MAX_ITERATIONS} iterations (stuck at "
                       f"{stuck or '<start>'})")
