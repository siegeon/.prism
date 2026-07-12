"""Conductor service — wrapper over the Conductor engine with scores.db queries."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


META_MIN_HOLDOUT_DELTA = 0.03
META_MAX_TOKEN_RATIO = 1.15
META_MAX_RETRY_DELTA = 0.0
META_MAX_FOLLOWUP_DELTA = 0.0
META_MAX_REVERT_DELTA = 0.0
META_MIN_SAMPLE_N = 5
META_REQUIRED_CONTEXTPACK_SCORE = 1.0
AUTO_MIN_OUTCOMES = 1

# Epsilon constants (mirror conductor_engine values)
EPSILON_START = 0.3
EPSILON_MIN = 0.05
EPSILON_DECAY = 0.05


def is_weak_proof(value: object) -> bool:
    """Ported from goalbuddy scripts/check-goal-state.mjs isWeakProof().

    A completion proof / oracle signal is "weak" when it is absent or a
    placeholder — i.e. it does not actually evidence the outcome. Used to
    flag (advisory) a green_gate close that carries no real proof.
    """
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in ("", "unknown", "tbd", "todo", "none"):
        return True
    # placeholder tokens like "<fill me>" / "<observable signal>"
    return s.startswith("<") and s.endswith(">")


def green_gate_proof_note(files_modified: int, completion_proof: object) -> str:
    """Advisory note appended to a green_gate close (annotate, never block).

    Encodes the goalbuddy Judge doctrine "lots of files is not completion":
      * code changed (files_modified>0) but no real completion_proof
        -> BUSYWORK RISK: effort without demonstrated outcome.
      * nothing changed and no proof -> ORACLE: no completion signal at all.
      * a real completion_proof -> clean ('').
    """
    if not is_weak_proof(completion_proof):
        return ""
    if files_modified > 0:
        return (f"  ⚠ busywork risk: {files_modified} file-change(s) but no "
                f"completion_proof (effort ≠ outcome)")
    return "  ⚠ oracle: no completion_proof recorded"


def green_gate_misfire_note(likely_misfire: object,
                            completion_proof: object) -> str:
    """Advisory note (annotate, never block) for the goalbuddy GAP-2 misfire.

    A `likely_misfire` records how a task could pass-but-be-WRONG. At
    green_gate we want the completion_proof to VISIBLY address that risk;
    if it doesn't, flag it (the recurring false-green / tests-pass≠
    feature-works failure mode). Mirrors green_gate_proof_note: silent
    when there's nothing to say.

    * no misfire recorded            -> '' (nothing to audit)
    * misfire recorded AND the proof references it -> '' (addressed)
    * misfire recorded but the proof ignores it    -> a "⚠ misfire" note

    "Addresses" is a deliberately cheap heuristic: the proof shares a
    meaningful word (len>4) with the recorded misfire. Advisory only — a
    false-silent here costs nothing the proof-note tooth doesn't catch.
    """
    misfire = str(likely_misfire or "").strip()
    if not misfire:
        return ""
    proof = str(completion_proof or "").lower()
    if proof:
        misfire_words = {w for w in re.findall(r"[a-z]{5,}", misfire.lower())}
        if any(w in proof for w in misfire_words):
            return ""
    return ("  ⚠ misfire: a likely pass-but-wrong risk was recorded but the "
            "completion_proof does not visibly address it "
            f"(\"{misfire[:80]}\")")


def full_outcome_verdict(slice_green: bool, completion_proof: object,
                         incomplete_children: int) -> tuple[bool, str]:
    """GoalBuddy GAP-4 — is the OWNER'S full outcome complete, or just a slice?

    A green slice is not proof the owner's outcome is met (GoalBuddy
    SKILL.md:499-513). Owner-outcome-complete is True ONLY when ALL hold:
      * the slice is green (the gate passed), AND
      * there are no incomplete (non-cancelled) child tasks, AND
      * completion_proof is strong (reuses is_weak_proof).
    Otherwise returns (False, <concrete reason>). Pure + unit-testable.
    """
    if not slice_green:
        return False, "slice is not green"
    if is_weak_proof(completion_proof):
        return False, "completion_proof is weak/absent"
    if incomplete_children > 0:
        return False, f"{incomplete_children} incomplete child task(s)"
    return True, ""


def epic_rollup_verdict(children: list) -> tuple[bool, str]:
    """Issue #171 — an EPIC/parent green_gate is satisfiable by ROLLING UP
    its children. When every non-cancelled child task is status=done AND
    carries a strong completion_proof, the children ARE the parent's proof,
    so the epic need not reproduce its own red->green artifact. A weak or
    incomplete child FAILS with a concrete reason (no false green; NOT a
    blanket override). Returns (False, reason) when there are no children to
    roll up — a childless task is not an epic and takes the normal artifact
    path. Pure + unit-testable; reuses is_weak_proof so the proof bar matches
    full_outcome_verdict.
    """
    active = [c for c in (children or [])
              if str(_task_attr(c, "status", "")) != "cancelled"]
    if not active:
        return False, "no child tasks to roll up (not an epic)"
    incomplete = [c for c in active
                  if str(_task_attr(c, "status", "")) != "done"]
    if incomplete:
        return False, (f"{len(incomplete)} child task(s) not done — epic "
                       "roll-up needs every child done")
    weak = [c for c in active
            if is_weak_proof(_task_attr(c, "completion_proof", ""))]
    if weak:
        ids = ", ".join(str(_task_attr(c, "id", "?"))[:8] for c in weak)
        return False, (f"{len(weak)} child task(s) with weak/absent "
                       f"completion_proof ({ids})")
    return True, (f"epic roll-up: all {len(active)} child task(s) done with "
                  "strong completion_proof")


def green_gate_outcome_note(slice_green: bool, completion_proof: object,
                            incomplete_children: int) -> str:
    """Advisory note (annotate, never block) for the GAP-4 outcome verdict.

    Mirrors green_gate_proof_note/green_gate_misfire_note: SILENT when the
    owner outcome is satisfied; otherwise a conditional
    "slice-complete, not owner-outcome-complete: <reason>" note. Only
    meaningful once the slice is green — a not-yet-green slice has nothing
    to say here (the gate itself speaks).
    """
    if not slice_green:
        return ""
    complete, reason = full_outcome_verdict(
        slice_green, completion_proof, incomplete_children)
    if complete:
        return ""
    return (f"  ⚠ slice-complete, not owner-outcome-complete: {reason}")


def green_gate_conformance_note(violations: object) -> str:
    """Advisory note (annotate, never block) — architecture conformance
    at the terminal green_gate (task 8579d49e, piece c3).

    Takes the intended-vs-observed verdict shaped like violations.json
    ({"count": N, "violations": [{"from","to","principle"}...]}, see
    arc_governance.compute_violations). Mirrors green_gate_proof_note /
    misfire_note / outcome_note: SILENT ('') when conformance is clean,
    a "⚠ conformance" sibling note when principle violations exist.
    """
    v = violations if isinstance(violations, dict) else {}
    count = int(v.get("count") or 0)
    if count <= 0:
        return ""
    cited = "; ".join(
        f"{i.get('from')}->{i.get('to')} ({i.get('principle')})"
        for i in (v.get("violations") or [])[:3])
    return (f"  ⚠ conformance: {count} architecture principle "
            f"violation(s) observed vs intended layer rules — {cited}")


def _task_attr(task: object, name: str, default: str = "") -> object:
    """Read a field off a task whether it's a dict or an object/Namespace."""
    if isinstance(task, dict):
        return task.get(name, default)
    return getattr(task, name, default)


def _is_low_value_completion(task: object) -> bool:
    """A done task is LOW-VALUE on reliable signals only (goalbuddy GAP-5).

    True when the green_gate gate_reason carries the '⚠' advisory marker the
    note helpers stamp (busywork/oracle/misfire/slice-only) OR the
    completion_proof is weak (is_weak_proof). Deliberately does NOT use
    files_modified/diff churn — session file attribution is broken (0/0).
    """
    reason = str(_task_attr(task, "gate_reason", "") or "")
    if "⚠" in reason:
        return True
    return is_weak_proof(_task_attr(task, "completion_proof", ""))


def board_health(tasks) -> dict:
    """Cross-task board "reorient" signal (goalbuddy GAP-5).

    Over DONE ROOT tasks (status=='done', parent_id=='') ordered by
    completed_at DESC, count the LEADING run of low-value completions and set
    reorient=True when that run >= 2 (mirrors goalbuddy state.yaml
    max_consecutive_tiny_tasks:2). Pure + module-level so it unit-tests over
    synthetic task lists; accepts dicts or objects. A strong (clean) done-root
    task at the newest-first head breaks the run (count resets to 0 there).
    """
    done_root = [
        t for t in (tasks or [])
        if str(_task_attr(t, "status", "") or "") == "done"
        and not str(_task_attr(t, "parent_id", "") or "")
    ]
    done_root.sort(
        key=lambda t: str(_task_attr(t, "completed_at", "") or ""),
        reverse=True)
    run = 0
    for t in done_root:
        if _is_low_value_completion(t):
            run += 1
        else:
            break
    reorient = run >= 2
    reason = (
        f"{run} low-confidence slices in a row — reorient toward a milestone"
        if reorient else "")
    return {
        "consecutive_low_value": run,
        "reorient": reorient,
        "reason": reason,
    }


# Artifact-looking signals that evidence a real, demonstrable UI surface:
# an agent-browser / verify screenshot vs the dev :8888 surface, or a
# Playwright assertion. A pytest/unit line alone is NOT a UI artifact.
_UI_ARTIFACT_SIGNALS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    "screenshot", "agent-browser", "playwright", ":8888", "127.0.0.1:8888",
)


def ui_artifact_gate_reason(tags: object, proof_type: object,
                            completion_proof: object) -> str:
    """STRAND C — demonstrable-UI requirement at green_gate (task 56458db1).

    Returns a non-empty REJECTION reason when a `ui`-tagged task's
    green_gate close lacks a real UI artifact; returns "" (no objection)
    otherwise. UI-FIRST mandate: every feature ships a UI surface, so a
    `ui` task cannot green-gate on a pytest/unit line alone.

    PROOF-TYPE DEFERRAL (FR-4, task 0e071d68): the demo/screenshot
    requirement applies ONLY when proof_type is unset or 'demo'. A `ui`
    task that DECLARES a different oracle (metric/build-count/artifact/test)
    opts out of the demo coupling and is judged on its own proof_type shape
    by the green_gate artifact tooth — proof_type picks WHICH artifact.

    When proof_type is unset or 'demo', a `ui` task PASSES only when BOTH:
      * proof_type == 'demo' (an explicit demonstrable-UI proof), and
      * completion_proof cites a real UI artifact path (agent-browser /
        verify screenshot vs :8888, or a Playwright assertion).

    Non-`ui` tasks are UNAFFECTED (returns "").
    """
    tag_set = {str(t).strip().lower() for t in (tags or [])}
    if "ui" not in tag_set:
        return ""
    pt = str(proof_type or "").strip().lower()
    # FR-4: a declared non-demo oracle opts out of the demo/screenshot tooth.
    if pt and pt != "demo":
        return ""
    if pt != "demo":  # proof_type unset -> demo/screenshot requirement applies
        return ("ui task: green_gate requires proof_type='demo' with a real "
                "UI artifact (agent-browser/verify screenshot vs :8888 or a "
                "Playwright assertion) — not a pytest/unit line alone")
    proof = str(completion_proof or "").lower()
    if not any(sig in proof for sig in _UI_ARTIFACT_SIGNALS):
        return ("ui task: completion_proof cites no UI artifact — a "
                "demonstrable UI surface needs an agent-browser/verify "
                "screenshot path (vs :8888) or a Playwright assertion")
    return ""


# --- Proof-carrying artifacts, generalized to ALL gates (task 3826dac3) ---
# The ui-artifact tooth proved that self-attested strings are not proof. We
# generalize it: red_gate requires a committed FAILING-TEST TRACE; green_gate
# requires a captured FULL-SUITE-GREEN artifact. The artifact SHAPE is
# machine-validated (test verb + a failing/passing signal) and override
# CANNOT bypass it — override only skips the verifier, never the artifact.

# A failing-test trace must name a test runner AND a failure signal — a bare
# "trust me" string is rejected. The runner signal is satisfied by a runner
# name OR a committed test reference (a test-file path / nodeid).
_TEST_RUNNER_SIGNALS = ("pytest", "unittest", "jest", "vitest", "go test",
                        "cargo test", "npm test", "tox", " test",
                        "test_", "tests/", "_test.", ".py::", "test suite",
                        "tests pass", "tests passed", "suite")
_FAIL_SIGNALS = ("fail", "error", "assert", "traceback", "raised",
                 " f ", "exit 1", "exitcode=1", "non-zero", " red")
_PASS_SIGNALS = ("passed", "pass", "0 failed", "all green", "green",
                 "exit 0", "exitcode=0", " ok")


def _artifact_text(completion_proof: object, reason: object) -> str:
    """Concatenated lower-cased proof surface: the task's committed
    completion_proof PLUS the decision reason (an independent verifier
    pastes its re-run output into the override reason)."""
    return f"{completion_proof or ''}\n{reason or ''}".lower()


# PROOF-TYPE-DRIVEN SHAPE VALIDATORS (task 0e071d68). proof_type already
# exists end-to-end; the gate teeth now dispatch on it so a non-test oracle
# (metric / build-count / artifact) is judged on its OWN shape instead of
# being forced through the failing-test trace. test/unset keeps the legacy
# test shape — the working non-overridable tooth is GENERALIZED, not weakened.
_METRIC_PROOF_TYPES = ("metric", "build-count", "build_count", "buildcount",
                       "count")
# A metric/build-count receipt is a numeric count-delta or measured value:
# a digit AND a metric signal (a delta arrow, a count/ratio/percent word).
_METRIC_SIGNALS = ("metric", "count", "delta", "ratio", "benchmark",
                   "->", "→", "percent", "%", "reduced", "trimmed",
                   "increase", "decrease", "baseline", "measured")


def _has_metric_shape(text: str) -> bool:
    """True iff *text* carries a numeric count-delta / measured-value shape:
    at least one digit AND a metric signal token."""
    has_number = any(ch.isdigit() for ch in text)
    has_signal = any(s in text for s in _METRIC_SIGNALS)
    return has_number and has_signal


def _metric_gate_reason(text: str, gate_label: str) -> str:
    if _has_metric_shape(text):
        return ""
    return (f"{gate_label} requires a metric/build-count receipt — a numeric "
            "count-delta or measured value (e.g. '41 -> 31') in "
            "completion_proof or the re-run output, not a self-attested string")


def _artifact_path_reason(text: str, gate_label: str) -> str:
    if any(sig in text for sig in _UI_ARTIFACT_SIGNALS) or (
            "/" in text or "\\" in text):
        return ""
    return (f"{gate_label} requires an artifact-path receipt — a committed "
            "file/screenshot path in completion_proof or the re-run output, "
            "not a self-attested string")


def red_gate_artifact_reason(completion_proof: object, reason: object,
                             proof_type: object = None) -> str:
    """Return a REJECTION reason when a red_gate close carries no committed
    proof artifact for its proof_type, else "". proof_type drives the shape:
    metric/build-count -> a numeric count-delta; artifact -> a file path;
    test/demo/unset -> a test-runner invocation + a failure signal. A
    self-attested 'red landed' is rejected on every path."""
    text = _artifact_text(completion_proof, reason)
    pt = str(proof_type or "").strip().lower()
    if pt in _METRIC_PROOF_TYPES:
        return _metric_gate_reason(text, "red_gate")
    if pt == "artifact":
        return _artifact_path_reason(text, "red_gate")
    has_runner = any(s in text for s in _TEST_RUNNER_SIGNALS)
    has_fail = any(s in text for s in _FAIL_SIGNALS)
    if has_runner and has_fail:
        return ""
    return ("red_gate requires a committed failing-test trace artifact "
            "(a test-runner invocation + a real failure signal in "
            "completion_proof or the re-run output) — a self-attested "
            "string is not proof")


def green_gate_artifact_reason(completion_proof: object, reason: object,
                               proof_type: object = None) -> str:
    """Return a REJECTION reason when a green_gate close carries no captured
    proof artifact for its proof_type, else "". proof_type drives the shape:
    metric/build-count -> a numeric count-delta; artifact -> a file path;
    test/demo/unset -> a test-runner invocation + a pass signal OR a
    demonstrable-UI artifact (a ui feature's green proof is its rendered
    surface). A self-attested 'looks green to me' is rejected on every path."""
    text = _artifact_text(completion_proof, reason)
    pt = str(proof_type or "").strip().lower()
    if pt in _METRIC_PROOF_TYPES:
        return _metric_gate_reason(text, "green_gate")
    if pt == "artifact":
        return _artifact_path_reason(text, "green_gate")
    has_runner = any(s in text for s in _TEST_RUNNER_SIGNALS)
    has_pass = any(s in text for s in _PASS_SIGNALS)
    has_ui_artifact = any(sig in text for sig in _UI_ARTIFACT_SIGNALS)
    if (has_runner and has_pass) or has_ui_artifact:
        return ""
    return ("green_gate requires a captured full-suite-green artifact "
            "(a test-runner invocation + a pass/test-count signal, or a "
            "demonstrable-UI screenshot/Playwright artifact in "
            "completion_proof or the re-run output) — a self-attested "
            "string is not proof")


def gate_artifact_reason(gate_step_id: str, completion_proof: object,
                         reason: object, proof_type: object = None) -> str:
    """Dispatch the proof-carrying artifact check by gate + proof_type. ""
    when the gate has no artifact tooth (only red_gate/green_gate do today)."""
    if gate_step_id == "red_gate":
        return red_gate_artifact_reason(completion_proof, reason, proof_type)
    if gate_step_id == "green_gate":
        return green_gate_artifact_reason(completion_proof, reason, proof_type)
    return ""


def same_actor_override_reason(override_actor: object,
                               work_actors: object) -> str:
    """NO SELF-OVERRIDE (task 3826dac3). A gate cannot be cleared by the
    SAME actor that produced the work — the driver overriding its own gate
    is forbidden. Override is demoted to a distinct-actor exception: an
    independent verifier sub-agent (fresh context) must be the one to
    override. Returns a REJECTION reason when override_actor is among the
    work-producing actors, else "" (a distinct actor is allowed)."""
    actor = str(override_actor or "").strip().lower()
    if not actor:
        return ""
    produced = {str(a or "").strip().lower() for a in (work_actors or [])}
    produced.discard("")
    if actor in produced:
        return (f"same-actor override forbidden: actor {override_actor!r} "
                "produced the work on this task and cannot clear its own "
                "gate — an independent verifier (distinct actor) must "
                "re-run the claimed command and override")
    return ""


def overlapping_allowed_files(file_lists: list) -> set:
    """Ported from goalbuddy scripts/parallel-plan.mjs: parallel workers are
    safe ONLY when their allowed_files sets are provably disjoint. Returns the
    set of files claimed by more than one worker (empty set == safe to run in
    parallel)."""
    seen: set = set()
    clash: set = set()
    for files in file_lists or []:
        cur = set(files or [])
        clash |= (cur & seen)
        seen |= cur
    return clash


def can_run_parallel(file_lists: list) -> bool:
    """True iff the given allowed_files sets are pairwise disjoint."""
    return not overlapping_allowed_files(file_lists)


class ConductorService:
    """Service layer for Conductor engine and scores.db queries.

    Provides orchestration methods and direct score database access
    for the UI and MCP layers.
    """

    def __init__(
        self,
        scores_db: str,
        enable_engine: bool = True,
        task_svc: Optional[Any] = None,
        verifier_svc: Optional[Any] = None,
        project_root: Optional[str] = None,
    ) -> None:
        self._scores_db = scores_db
        self._conductor = None
        self._available = False
        # Conductor v2 (task 3826dac3): the host project root = the AGENT
        # CHECKOUT the driver works in. _verify_gate hands this concrete
        # workspace to verifier.run() so Tier0 scopes the real diff rather
        # than the MCP daemon cwd (which has no diff -> status=error). Wired
        # by ProjectContext after construction; None = legacy (daemon cwd).
        self._project_root = project_root
        # Conductor v2 (issue #79 [1/4]): optional TaskService reference
        # consumed by advance_task / gate_decide. Wired by ProjectContext
        # after both services exist; kept optional so legacy callers
        # (and the meta-conductor unit tests) can construct a bare
        # ConductorService without a TaskService.
        self._task_svc = task_svc
        # Conductor v2 (issue #79 [3/4]): optional VerifierService used by
        # gate_decide to convert a caller's 'approve' into a real
        # pass/fail decision. None = legacy behavior (trust the caller).
        self._verifier_svc = verifier_svc
        # Architecture governance (task 8579d49e): optional MemoryService
        # + project name, wired by ProjectContext. Used by the rubric
        # plan_coverage gate (Brain-stored principles) and the green_gate
        # conformance note (intended-vs-observed layer-edge diff).
        self._memory_svc: Optional[Any] = None
        self._project_name: str = ""
        self._ensure_meta_schema()
        if not enable_engine:
            return
        try:
            from prism_service.engines.conductor_engine import Conductor

            self._conductor = Conductor()
            self._available = True
        except Exception as exc:
            print(
                f"ConductorService: Conductor unavailable ({exc})",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Late binding for TaskService — ProjectContext wires this after
    # construction so the two services can stay laziness-friendly.
    # ------------------------------------------------------------------

    def attach_task_service(self, task_svc: Any) -> None:
        """Attach (or replace) the TaskService consumed by advance_task
        and gate_decide. No-op if already attached to the same instance.
        """
        self._task_svc = task_svc

    def attach_verifier_service(self, verifier_svc: Any) -> None:
        """Attach (or replace) the VerifierService consumed by gate_decide
        (issue #79 [3/4]). When None, gate_decide trusts the caller's
        action (legacy [1/4] behavior). When attached, 'approve' without
        override is verified against the prior step's validation kind.
        """
        self._verifier_svc = verifier_svc

    def attach_memory_service(self, memory_svc: Any,
                              project_name: str = "") -> None:
        """Attach the MemoryService (+ owning project name) consumed by
        the governance rubric gates and the green_gate conformance note
        (task 8579d49e). None reverts to principle-less scoring (which
        the plan gate treats as a failure — misfire guard)."""
        self._memory_svc = memory_svc
        if project_name:
            self._project_name = project_name

    def attach_project_root(self, project_root: Optional[str]) -> None:
        """Attach (or replace) the host project root = the agent checkout.
        ProjectContext wires this so _verify_gate scopes verifier.run() to
        the real working tree (the diff under verification), not the daemon
        cwd. None reverts to legacy daemon-cwd scoping."""
        self._project_root = project_root

    # ------------------------------------------------------------------
    # Delegated methods
    # ------------------------------------------------------------------

    def build_instruction(
        self,
        persona: str,
        step_id: str,
        difficulty: Optional[str] = None,
        story_context: Optional[str] = None,
    ) -> dict:
        """Build an agent instruction enriched with Brain context."""
        if not self._available or self._conductor is None:
            return {"instruction": "", "prompt_id": "", "available": False}
        try:
            result = self._conductor.build_agent_instruction(
                step_id=step_id,
                agent=persona,
                action=step_id,
                story_file=story_context or "",
            )
            return {
                "instruction": result,
                "prompt_id": self._conductor.last_prompt_id,
                "available": True,
            }
        except Exception as exc:
            return {"instruction": "", "prompt_id": "", "error": str(exc)}

    def record_outcome(
        self,
        prompt_id: str,
        persona: str,
        step_id: str,
        metrics: dict,
    ) -> None:
        """Record a step outcome for PSP scoring."""
        if not self._available or self._conductor is None:
            return
        self._conductor.record_outcome(prompt_id, persona, step_id, metrics)

    def reindex(self) -> int:
        """Trigger incremental reindex via Conductor."""
        if not self._available or self._conductor is None:
            return 0
        return self._conductor.incremental_reindex()

    # ------------------------------------------------------------------
    # Direct scores.db queries
    # ------------------------------------------------------------------

    def _scores_conn(self) -> sqlite3.Connection:
        """Open a read-only connection to scores.db."""
        conn = sqlite3.connect(self._scores_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_scores(
        self,
        persona: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> list[dict]:
        """Query score_aggregates from scores.db."""
        try:
            conn = self._scores_conn()
            clauses: list[str] = []
            params: list[str] = []
            if persona:
                clauses.append("persona = ?")
                params.append(persona)
            if step_id:
                clauses.append("step_id = ?")
                params.append(step_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM score_aggregates{where} ORDER BY avg_score DESC",
                params,
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_variants(self, persona: Optional[str] = None) -> list[dict]:
        """Query prompt_variants from scores.db."""
        try:
            conn = self._scores_conn()
            if persona:
                rows = conn.execute(
                    "SELECT * FROM prompt_variants WHERE persona = ?",
                    (persona,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM prompt_variants").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_retired(self) -> list[dict]:
        """Query retired_variants from scores.db."""
        try:
            conn = self._scores_conn()
            rows = conn.execute(
                "SELECT * FROM retired_variants ORDER BY retired_at DESC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Per-step fanout telemetry — ephemeral sub-agent dispatch/return counts
    # for the CURRENT workflow step (e.g. "8 test-writers handed out, 8 back").
    # Distinct from phase_progress' children basis, which counts CHILD TASKS.
    # ------------------------------------------------------------------

    def set_step_fanout(
        self, task_id: str, step: str, dispatched: int, returned: int
    ) -> dict:
        """UPSERT the fanout row for (task_id, step) and return it."""
        from datetime import datetime, timezone

        self._ensure_meta_schema()
        now = datetime.now(timezone.utc).isoformat()
        conn = self._scores_conn()
        conn.execute(
            "INSERT INTO step_fanout (task_id, step, dispatched, returned, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id, step) DO UPDATE SET "
            "dispatched=excluded.dispatched, returned=excluded.returned, "
            "updated_at=excluded.updated_at",
            (task_id, step, int(dispatched), int(returned), now),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM step_fanout WHERE task_id = ? AND step = ?",
            (task_id, step),
        ).fetchone()
        conn.close()
        return dict(row) if row else {}

    def _step_fanout(self, task_id: str, step: str) -> tuple[int, int]:
        """Return (dispatched, returned) for a step, or (0, 0) if none."""
        if not step:
            return (0, 0)
        try:
            conn = self._scores_conn()
            row = conn.execute(
                "SELECT dispatched, returned FROM step_fanout "
                "WHERE task_id = ? AND step = ?",
                (task_id, step),
            ).fetchone()
            conn.close()
            if row:
                return (int(row["dispatched"]), int(row["returned"]))
        except Exception:
            pass
        return (0, 0)

    # ------------------------------------------------------------------
    # Meta-Conductor: offline prompt-variant candidate loop
    # ------------------------------------------------------------------

    def _ensure_meta_schema(self) -> None:
        conn = self._scores_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompt_variants (
                prompt_id TEXT PRIMARY KEY,
                persona TEXT,
                content TEXT NOT NULL,
                source TEXT DEFAULT 'learned',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS prompt_scores (
                prompt_id TEXT,
                persona TEXT,
                step_id TEXT,
                score REAL,
                tokens_used INTEGER,
                context_tokens INTEGER,
                duration_s REAL,
                retries INTEGER,
                difficulty TEXT,
                tests_passed INTEGER,
                coverage_pct REAL,
                traceability_pct REAL,
                gate_passed INTEGER,
                probe_accuracy REAL,
                timestamp TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (prompt_id, persona, step_id, timestamp)
            );
            CREATE TABLE IF NOT EXISTS score_aggregates (
                prompt_id TEXT,
                persona TEXT,
                step_id TEXT,
                avg_score REAL DEFAULT 0.0,
                total_runs INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (prompt_id, persona, step_id)
            );
            CREATE TABLE IF NOT EXISTS meta_prompt_candidates (
                candidate_id TEXT PRIMARY KEY,
                prompt_id TEXT UNIQUE NOT NULL,
                persona TEXT NOT NULL,
                step_id TEXT NOT NULL,
                parent_prompt_id TEXT,
                content TEXT NOT NULL,
                rationale TEXT,
                generator TEXT,
                status TEXT DEFAULT 'proposed',
                created_at TEXT DEFAULT (datetime('now')),
                evaluated_at TEXT,
                promoted_at TEXT,
                decision_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_meta_prompt_candidates_status
                ON meta_prompt_candidates(status);
            CREATE INDEX IF NOT EXISTS idx_meta_prompt_candidates_persona_step
                ON meta_prompt_candidates(persona, step_id);
            CREATE TABLE IF NOT EXISTS meta_prompt_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL,
                baseline_score REAL,
                holdout_score REAL,
                train_score REAL,
                contextpack_score REAL,
                tests_passed INTEGER,
                retry_delta REAL,
                token_ratio REAL,
                followup_delta REAL,
                revert_delta REAL,
                sample_n INTEGER,
                score_delta REAL,
                passed INTEGER,
                reason TEXT,
                metrics_json TEXT,
                evaluated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS step_fanout (
                task_id TEXT NOT NULL,
                step TEXT NOT NULL,
                dispatched INTEGER NOT NULL DEFAULT 0,
                returned INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                PRIMARY KEY (task_id, step)
            );
            """
        )
        conn.commit()
        conn.close()

    def _current_prompt_content(self, prompt_id: str) -> str:
        conn = self._scores_conn()
        row = conn.execute(
            "SELECT content FROM prompt_variants WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()
        conn.close()
        if row:
            return str(row["content"])
        if "/" not in prompt_id:
            return ""
        persona, variant = prompt_id.split("/", 1)
        prompt_file = Path(__file__).parent.parent / "prompts" / persona / f"{variant}.md"
        try:
            return prompt_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def meta_brief(
        self,
        persona: str,
        step_id: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return a deterministic brief for an external meta-agent.

        PRISM does not call an LLM here. The caller can use this packet to
        draft a prompt variant, then submit it back through propose/evaluate.
        """
        self._ensure_meta_schema()
        scores = self.get_scores(persona=persona, step_id=step_id)
        current = scores[0] if scores else {
            "prompt_id": f"{persona}/default",
            "avg_score": 0.0,
            "total_runs": 0,
        }
        conn = self._scores_conn()
        top = conn.execute(
            "SELECT prompt_id, score, tokens_used, duration_s, retries, timestamp "
            "FROM prompt_scores WHERE persona=? AND step_id=? "
            "ORDER BY score DESC LIMIT ?",
            (persona, step_id, int(limit)),
        ).fetchall()
        low = conn.execute(
            "SELECT prompt_id, score, tokens_used, duration_s, retries, timestamp "
            "FROM prompt_scores WHERE persona=? AND step_id=? "
            "ORDER BY score ASC LIMIT ?",
            (persona, step_id, int(limit)),
        ).fetchall()
        conn.close()
        prompt_id = str(current.get("prompt_id") or f"{persona}/default")
        return {
            "schema": "prism.meta_conductor.brief.v1",
            "persona": persona,
            "step_id": step_id,
            "current_best": current,
            "current_prompt": self._current_prompt_content(prompt_id),
            "top_outcomes": [dict(r) for r in top],
            "low_outcomes": [dict(r) for r in low],
            "rules": [
                "Submit prompt text only; PRISM owns storage and promotion.",
                "Do not change MCP tool names, context-pack schema, or install hooks.",
                "Optimize for holdout task quality, not live-score gaming.",
            ],
            "promotion_thresholds": self.meta_thresholds(),
        }

    def meta_thresholds(self) -> dict[str, Any]:
        return {
            "min_holdout_delta": META_MIN_HOLDOUT_DELTA,
            "max_token_ratio": META_MAX_TOKEN_RATIO,
            "max_retry_delta": META_MAX_RETRY_DELTA,
            "max_followup_delta": META_MAX_FOLLOWUP_DELTA,
            "max_revert_delta": META_MAX_REVERT_DELTA,
            "min_sample_n": META_MIN_SAMPLE_N,
            "required_contextpack_score": META_REQUIRED_CONTEXTPACK_SCORE,
            "tests_passed_required": True,
        }

    def auto_meta_candidate(
        self,
        *,
        persona: str,
        step_id: str,
        limit: int = 5,
        metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Generate a deterministic prompt candidate from outcome traces.

        This is the no-LLM automatic path. PRISM mines existing scores and
        failure signals, writes a candidate through the same propose path, and
        optionally evaluates it if the caller supplies real benchmark metrics.
        """
        brief = self.meta_brief(persona=persona, step_id=step_id, limit=limit)
        stats = self._meta_outcome_stats(persona, step_id)
        if stats["sample_n"] < AUTO_MIN_OUTCOMES:
            return {
                "created": False,
                "reason": "no outcome traces for persona/step",
                "brief": brief,
                "stats": stats,
            }

        rules = self._auto_prompt_rules(stats)
        parent = str(brief["current_best"].get("prompt_id") or f"{persona}/default")
        content = self._render_auto_prompt(
            persona=persona,
            step_id=step_id,
            current_prompt=str(brief.get("current_prompt") or ""),
            rules=rules,
        )
        rationale = (
            "Deterministic Meta-Conductor candidate from PSP outcome traces: "
            + "; ".join(stats["signals"])
        )
        proposed = self.propose_meta_candidate(
            persona=persona,
            step_id=step_id,
            content=content,
            parent_prompt_id=parent,
            rationale=rationale,
            generator="prism-rule-meta-conductor",
        )
        result: dict[str, Any] = {
            "created": True,
            "candidate": proposed["candidate"],
            "rules_applied": rules,
            "stats": stats,
            "promotion_thresholds": proposed["promotion_thresholds"],
        }
        if metrics is not None:
            result["evaluation"] = self.evaluate_meta_candidate(
                proposed["candidate"]["candidate_id"],
                metrics,
            )
        return result

    def _meta_outcome_stats(self, persona: str, step_id: str) -> dict[str, Any]:
        conn = self._scores_conn()
        rows = conn.execute(
            "SELECT score, tokens_used, duration_s, retries, tests_passed, "
            "gate_passed, coverage_pct, traceability_pct, probe_accuracy "
            "FROM prompt_scores WHERE persona=? AND step_id=?",
            (persona, step_id),
        ).fetchall()
        conn.close()
        sample_n = len(rows)
        if not rows:
            return {
                "sample_n": 0,
                "avg_score": 0.0,
                "avg_tokens": 0.0,
                "avg_retries": 0.0,
                "test_fail_rate": 0.0,
                "gate_fail_rate": 0.0,
                "low_traceability_rate": 0.0,
                "signals": [],
            }

        def present(name: str) -> list[float]:
            vals: list[float] = []
            for row in rows:
                value = row[name]
                if value is not None:
                    vals.append(float(value))
            return vals

        scores = present("score")
        tokens = present("tokens_used")
        retries = present("retries")
        tests = present("tests_passed")
        gates = present("gate_passed")
        traceability = present("traceability_pct")
        coverage = present("coverage_pct")

        def avg(vals: list[float]) -> float:
            return sum(vals) / len(vals) if vals else 0.0

        test_fail_rate = (
            sum(1 for v in tests if v <= 0.0) / len(tests) if tests else 0.0
        )
        gate_fail_rate = (
            sum(1 for v in gates if v <= 0.0) / len(gates) if gates else 0.0
        )
        low_traceability_rate = (
            sum(1 for v in traceability if v < 0.8) / len(traceability)
            if traceability else 0.0
        )
        low_coverage_rate = (
            sum(1 for v in coverage if v < 0.7) / len(coverage)
            if coverage else 0.0
        )
        stats = {
            "sample_n": sample_n,
            "avg_score": round(avg(scores), 4),
            "avg_tokens": round(avg(tokens), 2),
            "avg_retries": round(avg(retries), 2),
            "test_fail_rate": round(test_fail_rate, 4),
            "gate_fail_rate": round(gate_fail_rate, 4),
            "low_traceability_rate": round(low_traceability_rate, 4),
            "low_coverage_rate": round(low_coverage_rate, 4),
            "signals": [],
        }
        signals: list[str] = []
        if stats["avg_retries"] > 0:
            signals.append(f"avg_retries={stats['avg_retries']}")
        if test_fail_rate > 0:
            signals.append(f"test_fail_rate={test_fail_rate:.2f}")
        if gate_fail_rate > 0:
            signals.append(f"gate_fail_rate={gate_fail_rate:.2f}")
        if low_traceability_rate > 0:
            signals.append(f"low_traceability_rate={low_traceability_rate:.2f}")
        if low_coverage_rate > 0:
            signals.append(f"low_coverage_rate={low_coverage_rate:.2f}")
        if stats["avg_tokens"] > 6000:
            signals.append(f"avg_tokens={stats['avg_tokens']}")
        if stats["avg_score"] < 0.7:
            signals.append(f"avg_score={stats['avg_score']}")
        if not signals:
            signals.append("stable_outcomes")
        stats["signals"] = signals
        return stats

    def _auto_prompt_rules(self, stats: dict[str, Any]) -> list[str]:
        rules = [
            "Start from the PRISM context pack and preserve MCP tool contracts.",
        ]
        if stats["avg_retries"] > 0 or stats["gate_fail_rate"] > 0:
            rules.append(
                "Before editing, identify the smallest behavior change and inspect the directly affected files."
            )
        if stats["test_fail_rate"] > 0 or stats["gate_fail_rate"] > 0:
            rules.append(
                "Before completion, run the narrowest relevant verification command and report the exact result."
            )
        if stats["low_traceability_rate"] > 0:
            rules.append(
                "Map each requirement to the files or tests that prove it before declaring the task done."
            )
        if stats["low_coverage_rate"] > 0:
            rules.append(
                "Prefer adding or updating focused regression tests when behavior changes."
            )
        if stats["avg_tokens"] > 6000:
            rules.append(
                "Keep context compact: cite only source files and PRISM memories that directly affect the change."
            )
        if stats["avg_score"] < 0.7:
            rules.append(
                "Call out residual risk explicitly and avoid broad refactors unless required by the task."
            )
        if len(rules) == 1:
            rules.append(
                "Keep the existing working pattern, but make verification and residual risk explicit."
            )
        return rules

    def _render_auto_prompt(
        self,
        *,
        persona: str,
        step_id: str,
        current_prompt: str,
        rules: list[str],
    ) -> str:
        base = current_prompt.strip()
        if not base:
            base = (
                f"# {persona} {step_id}\n"
                "Use PRISM MCP context, task state, memory, and Brain results "
                "before acting."
            )
        bullets = "\n".join(f"- {rule}" for rule in rules)
        return (
            f"{base}\n\n"
            "## Meta-Conductor adjustments\n"
            "These deterministic adjustments were generated from PRISM outcome "
            "signals, not by an LLM.\n"
            f"{bullets}"
        )

    def propose_meta_candidate(
        self,
        *,
        persona: str,
        step_id: str,
        content: str,
        parent_prompt_id: str = "",
        rationale: str = "",
        generator: str = "",
    ) -> dict[str, Any]:
        self._ensure_meta_schema()
        normalized = content.strip()
        if not normalized:
            raise ValueError("candidate content must not be empty")
        parent = parent_prompt_id or f"{persona}/default"
        digest = hashlib.sha256(
            f"{persona}\0{step_id}\0{parent}\0{normalized}".encode("utf-8")
        ).hexdigest()[:12]
        candidate_id = f"mc-{digest}"
        prompt_id = f"{persona}/meta-{digest}"
        conn = self._scores_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta_prompt_candidates "
            "(candidate_id, prompt_id, persona, step_id, parent_prompt_id, "
            " content, rationale, generator, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            " COALESCE((SELECT status FROM meta_prompt_candidates WHERE candidate_id=?), 'proposed'))",
            (
                candidate_id,
                prompt_id,
                persona,
                step_id,
                parent,
                normalized,
                rationale,
                generator,
                candidate_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM meta_prompt_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        conn.close()
        return {
            "candidate": dict(row),
            "promotion_thresholds": self.meta_thresholds(),
        }

    def evaluate_meta_candidate(
        self,
        candidate_id: str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_meta_schema()
        conn = self._scores_conn()
        cand = conn.execute(
            "SELECT * FROM meta_prompt_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if cand is None:
            conn.close()
            raise ValueError(f"unknown candidate_id: {candidate_id}")

        decision = self._meta_decision(metrics)
        now_expr = "datetime('now')"
        conn.execute(
            "INSERT INTO meta_prompt_evaluations "
            "(candidate_id, baseline_score, holdout_score, train_score, "
            " contextpack_score, tests_passed, retry_delta, token_ratio, "
            " followup_delta, revert_delta, sample_n, score_delta, passed, "
            " reason, metrics_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                decision["baseline_score"],
                decision["holdout_score"],
                decision["train_score"],
                decision["contextpack_score"],
                1 if decision["tests_passed"] else 0,
                decision["retry_delta"],
                decision["token_ratio"],
                decision["followup_delta"],
                decision["revert_delta"],
                decision["sample_n"],
                decision["score_delta"],
                1 if decision["passed"] else 0,
                decision["reason"],
                json.dumps(metrics, sort_keys=True, default=str),
            ),
        )
        if decision["passed"]:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_variants "
                "(prompt_id, persona, content, source) VALUES (?, ?, ?, 'meta-conductor')",
                (cand["prompt_id"], cand["persona"], cand["content"]),
            )
            conn.execute(
                f"UPDATE meta_prompt_candidates SET status='promoted', "
                f"evaluated_at={now_expr}, promoted_at={now_expr}, decision_json=? "
                "WHERE candidate_id=?",
                (json.dumps(decision, sort_keys=True), candidate_id),
            )
        else:
            conn.execute(
                f"UPDATE meta_prompt_candidates SET status='rejected', "
                f"evaluated_at={now_expr}, decision_json=? WHERE candidate_id=?",
                (json.dumps(decision, sort_keys=True), candidate_id),
            )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM meta_prompt_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        conn.close()
        return {
            "candidate": dict(updated),
            "decision": decision,
            "promoted": bool(decision["passed"]),
        }

    def _meta_decision(self, metrics: dict[str, Any]) -> dict[str, Any]:
        def f(name: str, default: float = 0.0) -> float:
            value = metrics.get(name, default)
            return float(value if value is not None else default)

        baseline = f("baseline_score")
        holdout = f("holdout_score")
        train = f("train_score")
        contextpack = f("contextpack_score")
        token_ratio = f("token_ratio", 999.0)
        retry_delta = f("retry_delta", 999.0)
        followup_delta = f("followup_delta", 999.0)
        revert_delta = f("revert_delta", 999.0)
        sample_n = int(metrics.get("sample_n") or 0)
        tests_passed = bool(metrics.get("tests_passed"))
        score_delta = holdout - baseline

        failures: list[str] = []
        if sample_n < META_MIN_SAMPLE_N:
            failures.append(f"sample_n {sample_n} < {META_MIN_SAMPLE_N}")
        if score_delta < META_MIN_HOLDOUT_DELTA:
            failures.append(
                f"holdout_delta {score_delta:.3f} < {META_MIN_HOLDOUT_DELTA:.3f}"
            )
        if contextpack < META_REQUIRED_CONTEXTPACK_SCORE:
            failures.append(
                f"contextpack_score {contextpack:.3f} < "
                f"{META_REQUIRED_CONTEXTPACK_SCORE:.3f}"
            )
        if not tests_passed:
            failures.append("tests_passed is false")
        if token_ratio > META_MAX_TOKEN_RATIO:
            failures.append(f"token_ratio {token_ratio:.3f} > {META_MAX_TOKEN_RATIO:.3f}")
        if retry_delta > META_MAX_RETRY_DELTA:
            failures.append(f"retry_delta {retry_delta:.3f} > {META_MAX_RETRY_DELTA:.3f}")
        if followup_delta > META_MAX_FOLLOWUP_DELTA:
            failures.append(
                f"followup_delta {followup_delta:.3f} > {META_MAX_FOLLOWUP_DELTA:.3f}"
            )
        if revert_delta > META_MAX_REVERT_DELTA:
            failures.append(f"revert_delta {revert_delta:.3f} > {META_MAX_REVERT_DELTA:.3f}")

        return {
            "passed": not failures,
            "reason": "passed" if not failures else "; ".join(failures),
            "baseline_score": baseline,
            "holdout_score": holdout,
            "train_score": train,
            "contextpack_score": contextpack,
            "tests_passed": tests_passed,
            "retry_delta": retry_delta,
            "token_ratio": token_ratio,
            "followup_delta": followup_delta,
            "revert_delta": revert_delta,
            "sample_n": sample_n,
            "score_delta": score_delta,
        }

    # ------------------------------------------------------------------
    # Conductor v2 — per-task workflow state machine (issue #79 [1/4])
    # ------------------------------------------------------------------
    #
    # advance_task / gate_decide are the only sanctioned entry points
    # for moving a task across WORKFLOW_STEPS. They consult and write
    # Task.workflow_step / .gate_state / .gate_reason via TaskService,
    # and append explicit task_history rows for every transition so the
    # audit log captures who moved the task and why.
    #
    # Out of scope for [1/4]:
    #   * No MCP surface (deliverable [2/4]).
    #   * No verifier_service consultation (deliverable [3/4]).
    #   * No per-task override of WORKFLOW_STEPS — every task walks the
    #     default sequence from models.workflow.

    @staticmethod
    def _workflow_steps() -> list[dict]:
        """Local import avoids a circular dep with models.workflow."""
        from prism_service.models.workflow import WORKFLOW_STEPS

        return WORKFLOW_STEPS

    @classmethod
    def _step_index(cls, step_id: str) -> int:
        """Return the position of step_id in WORKFLOW_STEPS, or -1.

        An empty step_id means the task has not entered the workflow,
        which is equivalent to index -1 (the next step is index 0).
        """
        if not step_id:
            return -1
        for i, step in enumerate(cls._workflow_steps()):
            if step["id"] == step_id:
                return i
        return -1

    @classmethod
    def _step_by_id(cls, step_id: str) -> Optional[dict]:
        if not step_id:
            return None
        for step in cls._workflow_steps():
            if step["id"] == step_id:
                return step
        return None

    def advance_task(
        self,
        task_id: str,
        validation: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Move a task to the next entry in WORKFLOW_STEPS.

        Rules:
          * Task with workflow_step='' enters the workflow at step 0.
          * If the *current* step is a gate and gate_state='pending', the
            transition is refused — the gate must be decided first.
          * After moving, if the *new* step is a gate, gate_state is set
            to 'pending' (caller must use gate_decide to release it).
          * Every transition appends a task_history row.

        Returns a dict shaped:
          {'ok': bool, 'task_id', 'from_step', 'to_step',
           'gate_state', 'reason' (on refusal)}
        """
        if self._task_svc is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "no TaskService attached"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "unknown task"}

        steps = self._workflow_steps()
        if not steps:
            return {"ok": False, "task_id": task_id,
                    "reason": "WORKFLOW_STEPS is empty"}

        current_id = task.workflow_step or ""
        current_step = self._step_by_id(current_id)

        # Refuse if we're sitting on a gate that hasn't been decided.
        if (current_step is not None
                and current_step["type"] == "gate"
                and task.gate_state == "pending"):
            return {
                "ok": False,
                "task_id": task_id,
                "from_step": current_id,
                "to_step": current_id,
                "gate_state": task.gate_state,
                "reason": (
                    f"gate '{current_id}' is pending; "
                    "call gate_decide before advancing"
                ),
            }

        current_index = self._step_index(current_id)
        next_index = current_index + 1
        if next_index >= len(steps):
            return {
                "ok": False,
                "task_id": task_id,
                "from_step": current_id,
                "to_step": current_id,
                "gate_state": task.gate_state,
                "reason": "task is already at the final workflow step",
            }

        next_step = steps[next_index]
        next_id = next_step["id"]
        new_gate_state = (
            "pending" if next_step["type"] == "gate" else "none"
        )
        # Clear stale gate_reason whenever we leave a gate.
        new_gate_reason = task.gate_reason if new_gate_state == "pending" else ""

        self._task_svc.update(
            task_id,
            workflow_step=next_id,
            gate_state=new_gate_state,
            gate_reason=new_gate_reason,
        )

        detail_bits = [f"from={current_id or '<start>'}", f"to={next_id}"]
        if validation:
            detail_bits.append(f"validation={validation}")
        if new_gate_state == "pending":
            detail_bits.append("gate=pending")
        self._task_svc.record_history(
            task_id,
            action="advance_task",
            details="; ".join(detail_bits),
            actor="conductor",
        )

        # Conductor-path auto-writer: stamp/refresh the task_sessions row
        # from the carried task_id + session so the association is
        # captured even if the session never reaches the Stop hook.
        self._stamp_session(task_id, session_id)

        # Per-role token attribution (role/tier engine): the step we just
        # LEFT (current_id) has completed its work — record it. The window
        # start is when the task ENTERED current_id (its earlier advance_task
        # history row); None falls back to whole-session best-effort.
        if current_id:
            self._record_agent_run(
                task_id, current_id, session_id, model=model,
                started_at=self._step_entry_epoch(task_id, current_id),
            )

        return {
            "ok": True,
            "task_id": task_id,
            "from_step": current_id,
            "to_step": next_id,
            "gate_state": new_gate_state,
        }

    def _stamp_session(
        self, task_id: str, session_id: Optional[str],
    ) -> None:
        """Best-effort upsert of a task_sessions row via the single
        TaskService writer. No-op when no session is carried or the
        writer is unavailable — must never break a transition."""
        if not session_id:
            return
        link = getattr(self._task_svc, "link_session", None)
        if not callable(link):
            return
        try:
            link(task_id, session_id)
        except Exception:
            pass

    def _step_entry_epoch(self, task_id: str, step_id: str) -> Optional[float]:
        """Epoch seconds the task ENTERED step_id — the token-attribution
        window start for that step. Reads the advance_task history row whose
        details recorded ``to=<step_id>`` (the LAST such entry wins on
        re-visits). None when unknown so the caller falls back to whole-session.
        """
        if not step_id or self._task_svc is None:
            return None
        try:
            rows = self._task_svc.history(task_id)
        except Exception:
            return None
        latest: Optional[float] = None
        pat = re.compile(rf"to={re.escape(step_id)}(?:$|[;\s])")
        for r in rows:
            if getattr(r, "action", "") != "advance_task":
                continue
            if pat.search(getattr(r, "details", "") or ""):
                ts = self._parse_iso(getattr(r, "timestamp", "") or "")
                if ts is not None:
                    latest = ts
        return latest

    def _record_agent_run(
        self, task_id: str, step: str, session_id: Optional[str],
        model: Optional[str] = None, gate_state: Optional[str] = None,
        verdict_summary: Optional[str] = None, ok: bool = True,
        started_at: Optional[float] = None,
    ) -> None:
        """Best-effort per-role token attribution: write ONE agent_runs row
        for the just-completed step. role = models.roles.role_for_step(step);
        tokens = live-transcript output tokens in [started_at, now] (whole
        session when started_at is None). NEVER raises — a telemetry failure
        must not break a conductor transition."""
        try:
            import time as _time
            from prism_service.models.roles import role_for_step
            from prism_service.services.agent_runs_data import upsert_agent_run

            role = role_for_step(step)
            now = _time.time()
            tokens = 0
            try:
                if session_id:
                    from prism_service.services.claude_transcripts import (
                        live_token_events_for_session,
                    )
                    events = live_token_events_for_session(
                        session_id,
                        self._project_source_path(),
                        override_dir=self._project_override_dir(),
                    )
                    for epoch_s, tok in events:
                        if started_at is None or epoch_s >= started_at:
                            tokens += int(tok or 0)
            except Exception:
                tokens = 0
            win_start = started_at if started_at is not None else now
            duration_ms = int(max(0.0, now - win_start) * 1000)
            row = {
                "run_id": task_id,
                "workflow_name": "conductor",
                "task_id": task_id,
                "session_id": session_id or "",
                "agent_id": session_id or "conductor",
                "parent_agent_id": None,
                "role": role,
                "step": step,
                "model": model,
                "started_at": win_start,
                "ended_at": now,
                "duration_ms": duration_ms,
                "tokens": tokens,
                "tool_uses": None,
                "ok": ok,
                "gate_state": gate_state,
                "verdict_summary": verdict_summary,
                "evidence_ref": None,
            }
            upsert_agent_run(self._scores_db, row)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Gate verification helpers (issue #79 [3/4])
    # ------------------------------------------------------------------

    @classmethod
    def _validation_for_gate(cls, gate_step_id: str) -> Optional[str]:
        """Return the validation kind the verifier should check at this
        gate. By convention, a gate inherits its expectation from the
        immediately preceding step's ``validation`` field
        (e.g. ``red_gate`` follows ``write_failing_tests`` whose
        validation is ``red_with_trace``)."""
        steps = cls._workflow_steps()
        idx = cls._step_index(gate_step_id)
        if idx <= 0:
            return None
        for prev in reversed(steps[:idx]):
            if prev.get("validation"):
                return str(prev["validation"])
        return None

    # Mapping: validation kind -> (allowed verifier statuses,
    # allowed tier0 statuses, human-readable expectation). Rubric kinds
    # (story_complete, plan_coverage) carry a `rubric` key: they are
    # scored by the PURE YAML-rubric functions in services/arc_governance
    # against the task's own evidence (plan_doc/plan_diagram) — the old
    # forced-override path (value None -> "requires manual review") is
    # RETIRED (task 8579d49e).
    _VERIFIER_RULES: dict[str, Optional[dict]] = {
        "red_with_trace": {
            "expect_status": ("fail",),
            "expect_tier0": ("fail",),
            "expectation": (
                "red_with_trace expects verifier.status=fail with "
                "tier0=fail (failing test scaffold landed)"
            ),
        },
        "green": {
            "expect_status": ("pass",),
            "expect_tier0": ("pass", "not-run"),
            "expectation": "green expects verifier.status=pass with tier0=pass",
        },
        "green_full": {
            # NOT-RUN IS A REFUSAL (inverted-flow #5): green_full no longer
            # accepts tier0=not-run — a run that verified NOTHING is not a
            # pass. The honest green signal is carried by the 3-lane verifier
            # (oracle receipt + red->green continuity + baseline-diff
            # regression); tier0=pass here means the impacted suite really ran.
            "expect_status": ("pass",),
            "expect_tier0": ("pass",),
            "expectation": ("green_full expects verifier.status=pass with "
                            "tier0=pass (not-run is refused, not a pass)"),
        },
        "story_complete": {
            "rubric": "story_complete",
            "expectation": (
                "story_complete is rubric-verified: required sections "
                "present and every AC carries an id + oracle"
            ),
        },
        "plan_coverage": {
            "rubric": "plan_coverage",
            "expectation": (
                "plan_coverage is rubric-verified: every story AC id is "
                "covered, plan_diagram parses, and no Brain-stored "
                "principle is violated"
            ),
        },
    }

    @staticmethod
    def _merge_base_baseline(workspace: str) -> Optional[str]:
        """merge-base(origin/main, HEAD) for the checkout — the branch point,
        so Tier0 scopes the COMMITTED branch diff (committed test/impl files),
        not just the working tree. Falls back to origin/main, then None."""
        import subprocess
        for args in (["git", "merge-base", "origin/main", "HEAD"],
                     ["git", "rev-parse", "origin/main"]):
            try:
                r = subprocess.run(args, cwd=workspace, capture_output=True,
                                   text=True, timeout=10)
                if r.returncode == 0 and r.stdout.strip():
                    return r.stdout.strip().splitlines()[0]
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                continue
        return None

    def _conformance_payload(self) -> dict:
        """Best-effort intended-vs-observed verdict for the green_gate
        conformance note: Brain-stored principles diffed against the
        latest cached architecture_analyzer layers.json. Also persists
        the verdict beside it as violations.json (d1). NEVER raises —
        the note is advisory and a missing artifact means silence."""
        empty = {"count": 0, "violations": []}
        try:
            from prism_service.services import arc_governance as gov
            from prism_service.services import (
                understand_artifact_store as artifact_store)
            if self._memory_svc is None or not self._project_name:
                return empty
            principles = gov.load_principles(self._memory_svc)
            if not principles:
                return empty
            for sha in reversed(
                    artifact_store.list_cached_shas(self._project_name)):
                layers = artifact_store.get(
                    self._project_name, sha, "architecture_analyzer")
                if not isinstance(layers, dict):
                    continue
                verdict = gov.compute_violations(principles, layers)
                try:
                    artifact_store.put(self._project_name, sha,
                                       "violations_analyzer", verdict)
                except Exception:
                    pass
                return verdict
            return empty
        except Exception:
            return empty

    def _oracle_receipt_refusal(self, task, *, override: bool,
                                reason: str):
        """Decide the green_gate ORACLE tooth by a REAL run, not prose shape
        (inverted-flow soundness #2).

        Returns ``(refusal_reason, fresh_receipt)``. ``refusal_reason`` is ""
        (the gate may proceed) when a FRESH PASSING EvidenceReceipt exists —
        fresh = its ``tree_sha`` matches the task's current workspace commit
        AND its ``spec_hash`` matches the task's CURRENT OracleSpec (so a new
        commit or an oracle edit invalidates prior receipts). On the override
        path only, an explicit ``manual_evidence_required`` acknowledgement in
        ``reason`` (backed by a manual-status receipt on file) is also
        accepted — the honest terminal case for an oracle we cannot auto-run.
        Never raises: any error yields a refusal (fail closed), never a pass."""
        try:
            from prism_service.services import oracle_spec as osp
            from prism_service.services import task_workspace
            project = self._project_name or "default"
            ws = task_workspace.workspace_for(getattr(task, "id", ""))
            tree_sha = osp.current_tree_sha(
                (ws or {}).get("path") if ws else None)
            spec = osp.OracleSpec.from_task(task)
            # PINNED CONTROL-PLANE (inverted-flow #3): a receipt minted under a
            # different pinned policy_hash is stale, like a spec edit. Resolved
            # best-effort; "" (unpinnable) keeps the tree+spec behavior.
            from prism_service.services import control_plane as _cp
            _pin = _cp.pinned_policy(getattr(task, "id", ""))
            _policy_hash = _pin.get("policy_hash", "")
            fresh = osp.fresh_passing_receipt(
                project, getattr(task, "id", ""), tree_sha, spec.spec_hash(),
                policy_hash=_policy_hash)
            if fresh is not None:
                return "", fresh
            # Override may accept a logged manual acknowledgement, but only
            # when a manual-status receipt for THIS spec/tree is actually on
            # file — a typed token alone is not evidence.
            if override and "manual_evidence_required" in (reason or "").lower():
                for r in reversed(osp.read_receipts(
                        project, getattr(task, "id", ""))):
                    if (r.status == osp.ST_MANUAL and r.tree_sha == tree_sha
                            and r.spec_hash == spec.spec_hash()):
                        return "", r
            latest = osp.latest_receipt(project, getattr(task, "id", ""))
            if latest is None:
                detail = ("no EvidenceReceipt on file — the oracle was never "
                          "run (green_gate requires a trusted run, not a "
                          "self-attested proof string)")
            elif latest.status == osp.ST_MANUAL:
                detail = ("latest receipt is manual_evidence_required "
                          f"(adapter={latest.adapter}): {latest.reason}")
            elif not latest.passed:
                detail = (f"latest receipt FAILED: {latest.reason}")
            elif (_policy_hash and latest.policy_hash
                  and latest.policy_hash != _policy_hash
                  and latest.tree_sha == tree_sha
                  and latest.spec_hash == spec.spec_hash()):
                detail = ("latest passing receipt is STALE — it was minted "
                          f"under pinned policy {latest.policy_hash[:19]}, but "
                          f"the control-plane is now pinned at "
                          f"{_policy_hash[:19]} (the gate policy changed — "
                          "re-run the oracle under the current pinned policy)")
            else:
                detail = ("latest passing receipt is STALE — it was run at "
                          f"tree={latest.tree_sha[:12] or 'n/a'} / "
                          f"{latest.spec_hash[:19]}, but the task is now at "
                          f"tree={tree_sha[:12] or 'n/a'} / "
                          f"{spec.spec_hash()[:19]} (a new commit or an oracle "
                          "edit invalidated it — re-run the oracle)")
            return (f"green_gate: oracle not evidenced — {detail}. The token "
                    "proof scorer is advisory only.", None)
        except Exception as exc:  # fail closed
            return (f"green_gate: oracle receipt check errored "
                    f"({type(exc).__name__}: {exc}) — refusing (fail closed)",
                    None)

    def mint_green_evidence(self, task_id: str,
                            session_id: Optional[str] = None,
                            model: Optional[str] = None,
                            release: bool = False) -> dict:
        """Run the 3-lane honest green signal and MINT the oracle
        EvidenceReceipt the green_gate later requires (inverted-flow #5).

        Called when the drive reports SUCCESS on verify_green_state — so the
        gate that follows sees a FRESH receipt (produced from the task's
        worktree in a clean isolated env), not a self-attested proof string.
        No-op ok=False when no lane-capable VerifierService is attached."""
        if self._task_svc is None:
            return {"ok": False, "reason": "no TaskService attached"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "reason": "unknown task"}
        verifier = self._verifier_svc
        if verifier is None or not hasattr(verifier, "run_green_lanes"):
            return {"ok": False, "reason": "no lane-capable verifier attached"}
        try:
            report = verifier.run_green_lanes(
                task, release=release, project=self._project_name or "default")
        except Exception as exc:
            return {"ok": False, "reason": f"lanes errored: "
                    f"{type(exc).__name__}: {exc}"}
        return {"ok": report.get("verdict") == "pass", "lanes": report}

    def _verify_rubric_gate(self, task, validation: str) -> dict:
        """Score a rubric validation kind (story_complete/plan_coverage)
        as a PURE function of the task's own evidence + the YAML rubric
        (services/arc_governance). Principles come from memory data via
        the attached MemoryService; an empty/unseeded principle store
        never passes plan conformance (misfire guard)."""
        from prism_service.services import arc_governance as gov
        try:
            rubric = gov.load_rubrics().get(validation) or {}
            evidence = {
                "story_md": getattr(task, "plan_doc", "") or "",
                "plan_doc": getattr(task, "plan_doc", "") or "",
                "plan_diagram": getattr(task, "plan_diagram", "") or "",
            }
            if validation == "story_complete":
                res = gov.score_story_complete(evidence, rubric)
            else:
                principles = (gov.load_principles(self._memory_svc)
                              if self._memory_svc is not None else [])
                res = gov.score_plan_coverage(evidence, rubric, principles)
        except Exception as exc:
            return {"verified": False,
                    "reason": (f"rubric scoring raised "
                               f"{type(exc).__name__}: {exc}"),
                    "verifier": None, "validation": validation}
        return {"verified": bool(res.get("ok")),
                "reason": str(res.get("reason", "")),
                "verifier": res, "validation": validation}

    def _verify_gate(self, task, gate_step_id: str,
                     proof_type: object = None) -> dict:
        """Consult the attached VerifierService for the gate's expected
        validation. Returns a dict shaped:
          {'verified': bool|None, 'reason': str,
           'verifier': <raw verifier dict or None>,
           'validation': <kind or None>}
        'verified' is True/False after a verifier run, or None when no
        verifier is attached or the validation is a manual kind."""
        validation = self._validation_for_gate(gate_step_id)
        if validation is None:
            return {"verified": None, "reason": "gate has no validation kind",
                    "verifier": None, "validation": None}
        rule = self._VERIFIER_RULES.get(validation)
        if rule is None:
            return {
                "verified": None,
                "reason": (
                    f"validation {validation!r} requires manual review; "
                    "re-call gate_decide with override=True"
                ),
                "verifier": None,
                "validation": validation,
            }
        # Rubric kinds (task 8579d49e): scored as a PURE function of the
        # task's own evidence + the YAML rubric — never the shell verifier.
        if rule.get("rubric"):
            return self._verify_rubric_gate(task, validation)
        # PROOF-TYPE-AWARE TIER0 CONSULT (FR-3, task 0e071d68): the
        # test-shaped run_tier0 expectation (red_with_trace/green_full) only
        # applies to a test oracle. A non-test proof_type (metric/build-count/
        # artifact) yields zero claims -> tier0 'error', which used to force
        # override on every gate. Skip the test-shaped consult for those; the
        # proof_type artifact tooth (gate_artifact_reason) is the real check.
        pt = str(proof_type or "").strip().lower()
        if pt and pt not in ("test", "demo"):
            return {
                "verified": True,
                "reason": (f"proof_type={pt!r}: tier0 test-shaped consult "
                           "skipped; judged on the proof_type artifact shape"),
                "verifier": None,
                "validation": validation,
            }
        # gate_decide short-circuits when self._verifier_svc is None
        # (legacy trust-caller path). _verify_gate is only called when
        # a verifier is attached, so we don't need a None-check here.
        try:
            run_kwargs: dict[str, Any] = {"task_id": task.id}
            # HONEST LOOP (task e825e00a): if this task has its OWN scratch
            # workspace, verify against THAT — the worker's real committed
            # tests — scoped to its recorded baseline. A fabricated trace with
            # no committed test yields zero tier0 claims and is refused. Falls
            # back to the shared daemon checkout when no per-task workspace.
            _tw = None
            try:
                from prism_service.services import task_workspace
                _tw = task_workspace.workspace_for(task.id)
            except Exception:
                _tw = None
            if _tw:
                run_kwargs["workspace"] = _tw["path"]
                run_kwargs["baseline_rev"] = _tw["baseline"]
            # Pass the agent checkout so Tier0 scopes the real diff. Without
            # it verifier.run() falls back to the daemon cwd (no diff ->
            # status=error 'nothing to verify'). project_root is the host
            # working tree wired by ProjectContext.attach_project_root.
            elif self._project_root:
                run_kwargs["workspace"] = str(self._project_root)
                # Scope Tier0 to the COMMITTED BRANCH diff, not just the
                # working tree. In a source-run dev checkout the working tree
                # is only untracked .dev-data noise, so a branch's COMMITTED
                # red tests were invisible (git diff HEAD shows nothing for
                # committed files) and Tier0 ran 0 of them — which is why every
                # gate fell back to override. baseline = merge-base(origin/main,
                # HEAD); `git diff <baseline>` then includes the branch's
                # committed test/impl files so the gate verifies on real proof.
                base = self._merge_base_baseline(str(self._project_root))
                if base:
                    run_kwargs["baseline_rev"] = base
            v = self._verifier_svc.run(**run_kwargs)
        except Exception as exc:
            return {
                "verified": False,
                "reason": f"verifier raised {type(exc).__name__}: {exc}",
                "verifier": None,
                "validation": validation,
            }
        status = str(v.get("status") or "")
        tier0 = str(v.get("tier0") or "")
        ok_status = status in rule["expect_status"]
        ok_tier0 = tier0 in rule["expect_tier0"]
        if ok_status and ok_tier0:
            return {
                "verified": True,
                "reason": (
                    f"verifier passed: status={status} tier0={tier0} "
                    f"({rule['expectation']})"
                ),
                "verifier": v,
                "validation": validation,
            }
        summary = v.get("summary") or f"status={status} tier0={tier0}"
        return {
            "verified": False,
            "reason": (
                f"verifier rejected: {summary}; "
                f"{rule['expectation']}"
            ),
            "verifier": v,
            "validation": validation,
        }

    def gate_decide(
        self,
        task_id: str,
        action: str,
        reason: str = "",
        override: bool = False,
        session_id: Optional[str] = None,
        actor: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        """Resolve a pending gate on a task.

        action='approve' flips gate_state to 'passed' and auto-advances
        past the gate to the next non-gate step. action='reject' flips
        gate_state to 'failed' and stores reason in task.gate_reason;
        reject does NOT auto-advance.

        When ``override`` is False (the default) and a VerifierService is
        attached, action='approve' first calls verifier_service.run() and
        only releases the gate if the verifier confirms the prior step's
        validation kind. Manual-only validation kinds (story_complete,
        plan_coverage) require ``override=True``. When ``override`` is
        True, the verifier is bypassed and the audit row carries
        actor='manual-override' plus the supplied reason.

        Returns a dict shaped:
          {'ok': bool, 'task_id', 'gate_step',
           'gate_state', 'to_step' (on approve), 'reason' (on refusal),
           'verifier' (when a verifier run informed the decision)}
        """
        if action not in ("approve", "reject"):
            return {"ok": False, "task_id": task_id,
                    "reason": f"unknown action {action!r}; "
                              "expected 'approve' or 'reject'"}
        if self._task_svc is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "no TaskService attached"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "unknown task"}

        # NO SELF-OVERRIDE (task 3826dac3): capture the actors that PRODUCED
        # the work BEFORE _stamp_session links this decision's own session,
        # so an independent verifier's fresh session isn't counted as a
        # work-producer and wrongly blocked from overriding.
        prior_work_actors: list[str] = []
        try:
            prior_work_actors = [s.get("session_id")
                                 for s in self._task_svc.sessions_for_task(task_id)]
        except Exception:
            prior_work_actors = []

        # Conductor-path auto-writer: stamp/refresh the task_sessions row
        # from the carried task_id + session on every gate decision.
        self._stamp_session(task_id, session_id)

        current_step = self._step_by_id(task.workflow_step)
        if current_step is None or current_step["type"] != "gate":
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": task.workflow_step,
                "gate_state": task.gate_state,
                "reason": "task is not currently on a gate step",
            }
        # Conductor v2 follow-up (#79): allow manual recovery on failed
        # gates. An explicit override=True on action='approve' supersedes
        # the verifier's earlier ruling; the audit row tags actor=
        # 'manual-override' so the recovery stays visible in task_history.
        # 'reject' on a failed gate is still pointless (already failed).
        if task.gate_state == "failed":
            if not (action == "approve" and override):
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": task.workflow_step,
                    "gate_state": task.gate_state,
                    "reason": (
                        "gate_state is 'failed'; recovery requires "
                        "action='approve' with override=True"
                    ),
                }
        elif task.gate_state != "pending":
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": task.workflow_step,
                "gate_state": task.gate_state,
                "reason": (
                    f"gate_state is {task.gate_state!r}; "
                    "gate_decide only acts on 'pending' (or 'failed' with override)"
                ),
            }

        gate_step_id = task.workflow_step

        if action == "reject":
            self._task_svc.update(
                task_id,
                gate_state="failed",
                gate_reason=reason,
            )
            self._task_svc.record_history(
                task_id,
                action="gate_decide",
                details=f"gate={gate_step_id}; action=reject; reason={reason}",
                actor="conductor",
            )
            self._record_agent_run(
                task_id, gate_step_id, session_id, model=model,
                gate_state="failed", ok=False,
                verdict_summary=("reject: " + (reason or ""))[:200],
            )
            return {
                "ok": True,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": "failed",
            }

        # action == 'approve' - validation evidence is REQUIRED.
        # Every approve must describe what was used to satisfy the gate
        # (test run, screenshot, manual review, etc.). The reason text
        # is the validation; without it the gate decision is opaque.
        # This rule applies even when a verifier is consulted - the human
        # narrative augments the machine check.
        if not (reason and reason.strip()):
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": task.gate_state,
                "reason": (
                    "approve requires reason describing the validation "
                    "used (test run, screenshot, manual review, etc.)"
                ),
            }
        # CANDIDATE-CONTROLS-JUDGE REFUSAL (inverted-flow #3): the branch under
        # test must not supply its own judge. If the task's WORKTREE diff (vs
        # its baseline) modifies ANY enumerated gate-policy file — the rubrics
        # YAML, the arc-governance scorer, the verifier ignore/lane rules, the
        # oracle spec/thresholds, the conductor gate logic, or the pin loader —
        # the gate REFUSES on EVERY gate, override or not: a task cannot loosen
        # the rubric/ignores it is graded by. A legitimate policy change routes
        # through the authorized control-plane path (PRISM_POLICY_CHANGE_APPROVED
        # or a 'policy-change'-tagged task), never silently from inside the
        # graded task. Fail-open only on an internal error (never a false pass).
        try:
            from prism_service.services import control_plane as _cp
            _cj_reason = _cp.candidate_controls_judge_reason(task)
        except Exception:
            _cj_reason = ""
        if _cj_reason:
            self._task_svc.update(
                task_id, gate_state="failed", gate_reason=_cj_reason)
            self._task_svc.record_history(
                task_id, action="gate_decide",
                details=(f"gate={gate_step_id}; action=approve; "
                         f"control-plane=fail; reason={_cj_reason}"),
                actor="conductor")
            self._record_agent_run(
                task_id, gate_step_id, session_id, model=model,
                gate_state="failed", ok=False,
                verdict_summary=("control-plane: " + _cj_reason)[:200])
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": "failed",
                "reason": _cj_reason,
            }
        # STRAND C — demonstrable-UI requirement at green_gate (task
        # 56458db1). A `ui`-tagged task cannot green-gate on a pytest/unit
        # line alone; it needs proof_type='demo' + a real UI artifact path.
        # This rule runs ahead of BOTH the override and the verifier paths
        # (the verifier is blind to the working tree per implement.js:46-54),
        # so a `ui` task is rejected here even when the verifier passes or an
        # operator overrides. Non-`ui` tasks return "" and are unaffected.
        if gate_step_id == "green_gate":
            ui_reason = ui_artifact_gate_reason(
                getattr(task, "tags", None),
                getattr(task, "proof_type", ""),
                getattr(task, "completion_proof", ""),
            )
            if ui_reason:
                self._task_svc.update(
                    task_id, gate_state="failed", gate_reason=ui_reason,
                )
                self._task_svc.record_history(
                    task_id,
                    action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"ui-artifact=fail; reason={ui_reason}"),
                    actor="conductor",
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "failed",
                    "reason": ui_reason,
                }
        # EPIC GREEN_GATE ROLL-UP (issue #171): a parent whose children all
        # carry passing completion_proof satisfies its OWN green_gate via the
        # children — it need not reproduce a separate red->green. This is an
        # ADDITIVE pass path (not a blanket override): when it holds, the
        # verifier consult and the artifact tooth are skipped; when it does
        # not, the normal teeth still decide (so a parent with its own proof
        # is unaffected). A weak/incomplete child surfaces a concrete reason
        # in place of the generic artifact text below.
        rollup_ok = False
        rollup_reason = ""
        rollup_has_children = False
        if gate_step_id == "green_gate":
            _kids = [c for c in self._task_svc.list()
                     if _task_attr(c, "parent_id", "") == task_id]
            rollup_has_children = bool(_kids)
            if _kids:
                rollup_ok, rollup_reason = epic_rollup_verdict(_kids)

        # ORACLE-OUTCOME TOOTH (task 3eb67fb3) — promote the ADVISORY
        # oracle/misfire notes (green_gate_proof_note/green_gate_misfire_note,
        # appended to the pass reason at :2159-2201) into a BLOCKING green_gate
        # refusal. On a green_gate approve that is NOT an audited override and
        # NOT an epic roll-up, the task's OWN stored completion_proof must
        # evidence the task's oracle: a real proof that cites the oracle's
        # observable token AND addresses the pre-declared likely_misfire. Reads
        # the LIVE task's fields (re-get) and judges the STORED proof, never
        # the decision reason — a green-looking approve reason must not clear an
        # empty/mismatched proof (the b521af05 false-green). Runs ahead of the
        # pass path; the audited distinct-actor override (below) still bypasses
        # it for the genuine no-machine-oracle terminal case (FR-6).
        # The tooth is ORACLE-BOUND: it only fires when the task DECLARES an
        # oracle (the b521af05 class). A task with no oracle has nothing for
        # the proof to evidence — leave it to the verifier / audited override.
        # Epics (rollup_has_children) are owned by the roll-up path below, so
        # skip them here (their child-proof reason must survive).
        green_outcome_note = ""
        _live = self._task_svc.get(task_id)
        _has_oracle = bool(str(getattr(_live, "oracle", "") or "").strip())
        if (gate_step_id == "green_gate" and not override
                and not rollup_has_children and _has_oracle):
            receipt_reason, _fresh = self._oracle_receipt_refusal(
                _live, override=False, reason=reason)
            if receipt_reason:
                self._task_svc.update(
                    task_id, gate_state="failed", gate_reason=receipt_reason,
                )
                self._task_svc.record_history(
                    task_id, action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"oracle-receipt=fail; reason={receipt_reason}"),
                    actor="conductor",
                )
                self._record_agent_run(
                    task_id, gate_step_id, session_id, model=model,
                    gate_state="failed", ok=False,
                    verdict_summary=("oracle-receipt: " + receipt_reason)[:200],
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "failed",
                    "reason": receipt_reason,
                }
            green_outcome_note = (
                f"oracle evidenced by receipt {_fresh.job_id} "
                f"(adapter={_fresh.adapter}, tree={(_fresh.tree_sha or 'n/a')[:12]}, "
                f"{_fresh.spec_hash[:19]})") if _fresh else ""

        verifier_payload: Optional[dict] = None
        verifier_validation: Optional[str] = None
        verifier_reason = ""
        override_actor = actor   # who is clearing the gate (caller)

        # GATE ACTOR-ROLE ENFORCEMENT (role/tier engine): generalize the
        # no-self-override tooth to the NORMAL approve path too. A gate is
        # adjudicated by its role — role_for_step(gate)='sm', the Steward —
        # acting as an INDEPENDENT reviewer. If the deciding actor produced
        # the work under review (is among prior_work_actors), refuse: a
        # distinct actor must clear the gate. The override path keeps its own
        # override-specific message below, so skip here when override=True.
        if not override:
            same_actor = same_actor_override_reason(override_actor,
                                                    prior_work_actors)
            if same_actor:
                from prism_service.models.roles import ROLES, role_for_step
                _gate_role = ROLES[role_for_step(gate_step_id)].label
                distinct_reason = (
                    f"gate '{gate_step_id}' must be cleared by a DISTINCT "
                    f"actor: the gate role ({_gate_role}) is an independent "
                    f"reviewer, but actor {override_actor!r} produced the "
                    "work under review — self-review is refused. An "
                    "independent verifier (distinct actor) must decide this "
                    "gate."
                )
                self._task_svc.update(
                    task_id, gate_state="failed", gate_reason=distinct_reason,
                )
                self._task_svc.record_history(
                    task_id, action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"same-actor=rejected; actor={override_actor}"),
                    actor="conductor",
                )
                self._record_agent_run(
                    task_id, gate_step_id, session_id, model=model,
                    gate_state="failed", verdict_summary="same-actor refused",
                    ok=False,
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "failed",
                    "reason": distinct_reason,
                }

        if override:
            # NO SELF-OVERRIDE (task 3826dac3): the actor who PRODUCED the
            # work cannot clear its own gate. An independent verifier
            # sub-agent (distinct actor, fresh context) must override. The
            # work-producing actors are the sessions linked PRIOR to this
            # decision (captured before _stamp_session above).
            same_actor = same_actor_override_reason(override_actor,
                                                    prior_work_actors)
            if same_actor:
                self._task_svc.record_history(
                    task_id, action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"override=True; self-override=rejected; "
                             f"actor={override_actor}"),
                    actor="conductor",
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": task.gate_state,
                    "reason": same_actor,
                }
            # NO OVERRIDE-SKIPS-THE-ORACLE (inverted-flow #2): the override
            # path used to bypass the oracle scorer entirely — the biggest
            # hole. On a green_gate with a declared oracle, override STILL
            # requires a fresh passing EvidenceReceipt, OR an explicit, logged
            # `manual_evidence_required` acknowledgement in the reason (the
            # honest terminal case: an oracle we cannot auto-run). Epics
            # (rollup_has_children) keep their child-proof path.
            if (gate_step_id == "green_gate" and not rollup_has_children
                    and _has_oracle):
                receipt_reason, _fresh_ov = self._oracle_receipt_refusal(
                    _live, override=True, reason=reason)
                if receipt_reason:
                    self._task_svc.update(
                        task_id, gate_state="failed",
                        gate_reason=receipt_reason,
                    )
                    self._task_svc.record_history(
                        task_id, action="gate_decide",
                        details=(f"gate={gate_step_id}; action=approve; "
                                 f"override=True; oracle-receipt=fail; "
                                 f"reason={receipt_reason}"),
                        actor="conductor",
                    )
                    return {
                        "ok": False,
                        "task_id": task_id,
                        "gate_step": gate_step_id,
                        "gate_state": "failed",
                        "reason": receipt_reason,
                    }
            # Manual override path — bypass the verifier entirely but
            # tag the audit row so the override is auditable. Override is a
            # separately-logged exception; the DISTINCT override actor is
            # recorded in detail_bits below (the history actor stays
            # 'manual-override' so the recovery class is greppable).
            actor = "manual-override"
            detail_bits = [
                f"gate={gate_step_id}",
                "action=approve",
                "override=True",
                f"override-actor={override_actor or 'unspecified'}",
            ]
            if reason:
                detail_bits.append(f"reason={reason}")
        elif rollup_ok:
            # Epic roll-up satisfies the green_gate WITHOUT override and
            # WITHOUT the epic's own verifier diff — the children's proofs are
            # the proof (issue #171). The artifact tooth is skipped below.
            actor = "conductor"
            detail_bits = [f"gate={gate_step_id}", "action=approve",
                           "epic-rollup=pass"]
            if reason:
                detail_bits.append(f"reason={reason}")
        elif self._verifier_svc is None:
            # Legacy [1/4] behavior — no verifier wired (bare
            # ConductorService used by unit tests and meta-only
            # callers). Trust the caller's approve. ProjectContext
            # always wires a verifier, so this path only fires for
            # explicit no-verifier construction.
            actor = "conductor"
            detail_bits = [f"gate={gate_step_id}", "action=approve"]
            if reason:
                detail_bits.append(f"reason={reason}")
        else:
            # Verifier-driven path. Look up the prior step's validation
            # kind and consult VerifierService. If the verifier rejects
            # or no verifier is attached, fail the gate (do NOT advance)
            # with the verifier's reason recorded on the task.
            outcome = self._verify_gate(
                task, gate_step_id, getattr(task, "proof_type", ""))
            verifier_payload = outcome.get("verifier")
            verifier_validation = outcome.get("validation")
            verifier_reason = outcome.get("reason", "")
            if outcome["verified"] is not True:
                self._task_svc.update(
                    task_id,
                    gate_state="failed",
                    gate_reason=verifier_reason,
                )
                self._task_svc.record_history(
                    task_id,
                    action="gate_decide",
                    details=(
                        f"gate={gate_step_id}; action=approve; "
                        f"verifier=fail; validation="
                        f"{verifier_validation or 'none'}; "
                        f"reason={verifier_reason}"
                    ),
                    actor="conductor",
                )
                refusal = {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "failed",
                    "reason": verifier_reason,
                    "validation": verifier_validation,
                }
                if verifier_payload is not None:
                    refusal["verifier"] = verifier_payload
                return refusal
            actor = "conductor"
            detail_bits = [
                f"gate={gate_step_id}",
                "action=approve",
                f"verifier=pass; validation={verifier_validation}",
            ]
            if reason:
                detail_bits.append(f"reason={reason}")

        # PROOF-CARRYING ARTIFACTS generalized to red_gate + green_gate
        # (task 3826dac3). red_gate requires a committed failing-test trace;
        # green_gate a captured full-suite-green. The artifact SHAPE is
        # machine-validated and override CANNOT bypass it — this runs AFTER
        # the verifier consult (so the non-override path still scopes the
        # real diff) yet ahead of the passing persist, on every approve path.
        artifact_reason = gate_artifact_reason(
            gate_step_id,
            getattr(self._task_svc.get(task_id), "completion_proof", ""),
            reason,
            getattr(task, "proof_type", ""),
        )
        if rollup_ok:
            # The children's proofs ARE the epic's artifact (issue #171).
            artifact_reason = ""
        elif (artifact_reason and rollup_has_children and rollup_reason):
            # An epic with children that did NOT cleanly roll up AND no
            # artifact of its own: surface the actionable roll-up failure
            # instead of the generic "self-attested string is not proof".
            artifact_reason = f"epic green_gate: {rollup_reason}"
        if artifact_reason:
            self._task_svc.update(
                task_id, gate_state="failed", gate_reason=artifact_reason,
            )
            self._task_svc.record_history(
                task_id, action="gate_decide",
                details=(f"gate={gate_step_id}; action=approve; "
                         f"artifact=fail; reason={artifact_reason}"),
                actor="conductor",
            )
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": "failed",
                "reason": artifact_reason,
            }

        # Persist validation evidence to the row so it surfaces wherever
        # gate_reason is rendered (TaskDetailPage, swimlane tooltips).
        # reason is required upstream, so it's always present here.
        if actor == "manual-override":
            passed_gate_reason = f"manual override: {reason}"
        elif verifier_validation:
            passed_gate_reason = f"verified ({verifier_validation}): {reason}"
        else:
            passed_gate_reason = reason
        # Oracle + anti-busywork (goalbuddy): at the terminal green_gate, flag a
        # close with no real completion_proof — and escalate to BUSYWORK RISK
        # when code churned without an outcome ("lots of files is not
        # completion"). Advisory per the hooks doctrine — annotate the reason,
        # never block — so it surfaces without breaking override-driven closes.
        if gate_step_id == "green_gate":
            _proof = getattr(self._task_svc.get(task_id), "completion_proof", "")
            if rollup_ok and is_weak_proof(_proof):
                # The epic's proof is its children's rolled-up proofs (#171);
                # surface that so the advisory notes stay truthful + silent.
                _proof = rollup_reason
            try:
                _churn = sum(int(s.get("files_modified", 0) or 0)
                             for s in self._task_svc.sessions_for_task(task_id))
            except Exception:
                _churn = 0
            passed_gate_reason += green_gate_proof_note(_churn, _proof)
            # goalbuddy GAP-2: audit the recorded pass-but-wrong risk.
            # Advisory — annotate when the completion_proof doesn't address
            # the misfire, silent otherwise.
            _misfire = getattr(self._task_svc.get(task_id),
                               "likely_misfire", "")
            passed_gate_reason += green_gate_misfire_note(_misfire, _proof)
            # goalbuddy GAP-4: distinguish a green SLICE from the owner's full
            # OUTCOME. Count incomplete (non-cancelled, non-done) child tasks;
            # owner-outcome-complete needs slice-green + strong proof + no
            # open children. Append the conditional advisory and set the field
            # truthfully (True only when the outcome is actually mapped).
            try:
                _children = [c for c in self._task_svc.list()
                             if c.parent_id == task_id]
                _incomplete = sum(1 for c in _children
                                  if c.status not in ("done", "cancelled"))
            except Exception:
                _incomplete = 0
            passed_gate_reason += green_gate_outcome_note(
                True, _proof, _incomplete)
            # Architecture governance (task 8579d49e, piece c3): diff the
            # observed layer edges against the Brain-stored principles and
            # ANNOTATE (never block) beside the proof/misfire/outcome notes.
            passed_gate_reason += green_gate_conformance_note(
                self._conformance_payload())
            # Oracle-receipt tooth verdict (inverted-flow #2): when the
            # BLOCKING tooth passed (a fresh passing EvidenceReceipt cleared
            # the gate), surface WHICH receipt so a compliant close is legibly
            # attributed to a real run.
            if green_outcome_note:
                passed_gate_reason += f"  ✓ {green_outcome_note}"
            # TOKEN SCORER DEMOTED TO A WARNING (inverted-flow #2): the old
            # gameable prose-shape scorer (arc_governance.score_green_outcome)
            # is no longer the deciding authority — the fresh-receipt tooth is.
            # Keep it as an ADVISORY omission note only (never blocks): it can
            # still flag a completion_proof that forgot to cite the oracle.
            if _has_oracle:
                try:
                    from prism_service.services import arc_governance as gov
                    _tok = gov.score_green_outcome(
                        {"oracle": getattr(_live, "oracle", ""),
                         "completion_proof": _proof,
                         "likely_misfire": _misfire},
                        gov.load_rubrics().get("green_outcome", {}))
                    if not _tok.get("ok"):
                        passed_gate_reason += (
                            "  ⚠ (advisory) " + str(_tok.get("reason", "")))
                except Exception:
                    pass
            _complete, _ = full_outcome_verdict(True, _proof, _incomplete)
            _outcome_complete = _complete
        # PINNED CONTROL-PLANE PROVENANCE (inverted-flow #3): stamp WHICH pinned
        # policy adjudicated this pass onto the gate_reason + audit detail +
        # response, so every green is attributable to a specific control ref +
        # policy hash (the candidate could not have edited it — the refusal
        # above blocked that). Best-effort; never blocks a pass.
        _pin: dict = {}
        try:
            from prism_service.services import control_plane as _cp
            _pin = _cp.pinned_policy(task_id)
        except Exception:
            _pin = {}
        if _pin.get("policy_hash"):
            passed_gate_reason += (
                f"  ✓ policy pinned {_pin['policy_hash'][:19]} @ "
                f"{(_pin.get('control_ref') or 'n/a')[:12]}")
            detail_bits.append(
                f"policy_hash={_pin['policy_hash'][:19]}; "
                f"control_ref={(_pin.get('control_ref') or '')[:12]}")
        self._task_svc.update(
            task_id,
            gate_state="passed",
            gate_reason=passed_gate_reason,
        )
        if gate_step_id == "green_gate":
            # TERMINAL STEP REACHED (task ae63e375): green_gate is the LAST
            # WORKFLOW_STEPS entry, so a PASSED terminal gate IS the task
            # done — mirrors conductor_work's own `done` predicate (final
            # step + gate_state=passed). Without this a task sat at
            # status=pending forever (never left the board, completed_at
            # never stamped) even though its own SDLC had finished.
            self._task_svc.update(
                task_id, full_outcome_complete=_outcome_complete,
                status="done")
        self._task_svc.record_history(
            task_id,
            action="gate_decide",
            details="; ".join(detail_bits),
            actor=actor,
        )

        # Auto-advance past the passed gate, carrying the session + model so
        # the transition's own attribution row is consistent (agent_id=session).
        _gate_entry = self._step_entry_epoch(task_id, gate_step_id)
        advance_result = self.advance_task(
            task_id, session_id=session_id, model=model)
        # Per-role attribution for the gate itself: record AFTER the advance so
        # this row (carrying gate_state + verdict) UPSERT-wins over the one the
        # auto-advance wrote for the same (task, session, gate) triple.
        _verdict = (verifier_validation and f"verified:{verifier_validation}") \
            or (actor == "manual-override" and "override") or "approved"
        self._record_agent_run(
            task_id, gate_step_id, session_id, model=model,
            gate_state="passed", verdict_summary=str(_verdict)[:200],
            ok=True, started_at=_gate_entry,
        )
        response: dict = {
            "ok": True,
            "task_id": task_id,
            "gate_step": gate_step_id,
            "gate_state": "passed",
            "to_step": advance_result.get("to_step", gate_step_id),
            "auto_advanced": bool(advance_result.get("ok")),
        }
        if verifier_payload is not None:
            response["verifier"] = verifier_payload
        if verifier_validation is not None:
            response["validation"] = verifier_validation
        if override:
            response["override"] = True
        if _pin.get("control_ref") or _pin.get("policy_hash"):
            response["control_ref"] = _pin.get("control_ref", "")
            response["policy_hash"] = _pin.get("policy_hash", "")
            response["policy_version"] = _pin.get(
                "policy_version", "")
        return response

    # Session-id prefixes used by smoke tests, dogfood probes, and the
    # bench harness. Rows with these ids carry near-zero token counts
    # and aren't real sessions — including them in averages drags the
    # mean toward zero and makes real work look like inflation. Filter
    # them out by default; pass include_smoke=True to see everything.
    _SMOKE_SESSION_PREFIXES: tuple[str, ...] = (
        "test-",
        "manual-",
        "sse-smoke-",
        "bridge-",
        "dogfood-",
        "hook-migration-",
        "diagnose-",
        "smoke-",
        "probe-",
    )

    @classmethod
    def _is_smoke_session(cls, row: dict) -> bool:
        """True if this row is a smoke/probe test, not a real session."""
        sid = (row.get("session_id") or "").lower()
        if any(sid.startswith(p) for p in cls._SMOKE_SESSION_PREFIXES):
            return True
        # Rows with zero tokens are incomplete/aborted records — the Stop
        # hook fired but never read the transcript. They aren't useful
        # signal, just noise on the mean.
        if (row.get("tokens_used") or 0) == 0:
            return True
        return False

    def get_session_outcomes(
        self, limit: int = 50, include_smoke: bool = False,
    ) -> list[dict]:
        """Query recent session outcomes from scores.db.

        Reads the ``session_outcomes`` table populated by
        ``record_session_outcome`` (served by the MCP and written by the
        Stop hook that prism_install ships). Maps DB columns onto the
        keys the /sessions UI expects (id, session_id, duration,
        tokens, files_modified, recorded_at).

        When ``include_smoke`` is False (default) rows whose session_id
        matches a known smoke/probe prefix or whose tokens_used is zero
        are dropped — those rows don't represent real sessions and skew
        the mean toward zero.
        """
        try:
            conn = self._scores_conn()
            # Pull a wider window when filtering so the post-filter list
            # still has up to ``limit`` real sessions. 4x is enough given
            # the observed smoke-row ratio in dogfood.
            db_limit = limit if include_smoke else limit * 4
            rows = conn.execute(
                "SELECT session_id, duration_s, tokens_used, files_read, "
                "files_modified, skills_invoked, timestamp "
                "FROM session_outcomes ORDER BY timestamp DESC LIMIT ?",
                (db_limit,),
            ).fetchall()
            conn.close()
        except Exception:
            return []
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            if not include_smoke and self._is_smoke_session(d):
                continue
            # Normalise keys to what sessions_page.py expects.
            d["id"] = d["session_id"]
            d["duration"] = d.get("duration_s")
            d["tokens"] = d.get("tokens_used")
            d["recorded_at"] = d.get("timestamp")
            # Honest per-work-unit metric. Tokens per session are
            # dominated by session scope; tokens per file edited
            # normalises by output and is a better proxy for retrieval
            # efficiency. None when files_modified is 0/missing so the
            # caller can show a dash instead of dividing.
            files_m = d.get("files_modified") or 0
            d["tokens_per_file"] = (
                int((d.get("tokens_used") or 0) / files_m)
                if files_m > 0 else None
            )
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def get_skill_usage(self, session_id: Optional[str] = None) -> list[dict]:
        """Query skill_usage from scores.db."""
        try:
            conn = self._scores_conn()
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM skill_usage WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM skill_usage").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def exploration_rate(self) -> float:
        """Compute the current epsilon for exploration.

        Uses total outcome count to determine how much the system
        should explore vs exploit prompt variants.
        """
        try:
            conn = self._scores_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM prompt_scores"
            ).fetchone()
            conn.close()
            total = row[0] if row else 0
            return max(EPSILON_MIN, EPSILON_START * math.exp(-EPSILON_DECAY * total))
        except Exception:
            return EPSILON_START

    # ------------------------------------------------------------------
    # Conductor v2 visual surface (#79 follow-up):
    # SPA /conductor page reads these to render "what tasks is the
    # conductor driving and where are they in the SDLC?"
    # ------------------------------------------------------------------

    def managed_tasks(self) -> list[dict]:
        """List tasks where conductor is engaged.

        A task is "managed" when workflow_step is non-empty OR gate_state
        is not 'none'. Tasks worked raw (status flips only) are not
        included — they don't appear on the /conductor page.

        v6.1.3: filter out status=done. Conductor swimlanes had been
        accumulating every task that ever reached green_gate, polluting
        the active view (14 of 15 visible tiles were done shipped work).
        Done means done — it stays in the audit trail (workflow_step is
        not cleared) but doesn't show as currently-managed work.
        """
        if self._task_svc is None:
            return []
        try:
            tasks = self._task_svc.list()
        except Exception:
            return []
        out: list[dict] = []
        for t in tasks:
            step = getattr(t, "workflow_step", "") or ""
            gate = getattr(t, "gate_state", "none") or "none"
            status = getattr(t, "status", "") or ""
            # Terminal statuses leave the conductor view: done is shipped,
            # cancelled is abandoned. Both keep their workflow_step in the
            # audit trail but must not render as currently-managed tiles.
            if status in ("done", "cancelled"):
                continue
            # Conductor mirrors the /tasks board: only TOP-LEVEL tasks are
            # tiles. Subtasks (parent_id set) belong under their parent's
            # detail page, never as standalone swimlane cards.
            if getattr(t, "parent_id", ""):
                continue
            if step == "" and gate == "none":
                # Claimed by a workflow but pre-first-advance (the Locate /
                # draft_story intake window): surface in a synthetic leading
                # "intake" lane so just-started work is VISIBLE on /conductor
                # instead of invisible. A truly idle backlog task (pending,
                # untouched) stays hidden.
                if status == "in_progress":
                    step = self.INTAKE_STEP
                else:
                    continue
            pp = self.phase_progress(t.id)
            # Compact ordered slice list for the tile: done first (by
            # completed_at) then the rest by created_at, so the slice bar reads
            # left-to-right as progress. Only title/status/id — keep it small.
            kids = self._children(t)
            subtasks = [
                {"id": c.id, "title": c.title, "status": getattr(c, "status", "") or ""}
                for c in sorted(
                    kids,
                    key=lambda c: (
                        0 if (getattr(c, "status", "") or "") == "done" else 1,
                        getattr(c, "completed_at", "") or getattr(c, "created_at", "") or "",
                    ),
                )
            ]
            out.append({
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "workflow_step": step,
                "gate_state": gate,
                "gate_reason": getattr(t, "gate_reason", "") or "",
                # v6.0.43: extra fields for the /conductor tile redesign so
                # the SPA can render status/priority/age/owner/tags without
                # an N+1 fetch on each tile.
                "priority": getattr(t, "priority", 0) or 0,
                "assigned_agent": getattr(t, "assigned_agent", "") or "",
                "created_at": getattr(t, "created_at", "") or "",
                "updated_at": getattr(t, "updated_at", "") or "",
                "tags": list(getattr(t, "tags", []) or []),
                # Animated SDLC progress bar (a5e0d9f5): blended current-step
                # fill so the tile bar tweens between polls.
                "phase_progress": pp,
                # Honest work state (working/adrift/stalled/awaiting_gate/…) —
                # the tile pill + live pulse read this, NOT the raw status.
                "activity": self.activity_for(t, pp),
                # Real progress: the epic's non-cancelled slices, done-first.
                # Omitted (empty) for leaf tasks — the tile only renders a slice
                # bar when this is non-empty.
                "subtasks": subtasks,
            })
        return out

    def step_buckets(self) -> dict[str, int]:
        """Count of conductor-managed tasks per workflow_step.

        Used by the /conductor stepper to show "12 tasks at implement_tasks,
        3 at red_gate" at a glance. v6.1.3: status=done excluded — same
        rationale as managed_tasks (counters were inflated by historical
        shipped work).
        """
        if self._task_svc is None:
            return {}
        try:
            tasks = self._task_svc.list()
        except Exception:
            return {}
        from collections import Counter
        counter: Counter[str] = Counter()
        for t in tasks:
            step = getattr(t, "workflow_step", "") or ""
            status = getattr(t, "status", "") or ""
            if status in ("done", "cancelled"):
                continue
            if getattr(t, "parent_id", ""):
                continue
            if not step:
                # Mirror managed_tasks: claimed-but-pre-step in_progress work
                # counts under the synthetic intake lane; idle backlog doesn't.
                if status == "in_progress":
                    step = self.INTAKE_STEP
                else:
                    continue
            counter[step] += 1
        return dict(counter)

    # ------------------------------------------------------------------
    # phase_progress — blended current-step fill for the SDLC bar
    # ------------------------------------------------------------------
    #   pct = min(0.95, in_step_s / typical_s)  (time baseline)
    #   OVERRIDDEN by children_done / children_total when child tasks exist.
    # Drives the animated current-segment fill on the conductor tiles +
    # TaskDetailPage header so the bar tweens between 5s polls instead of
    # snapping at each advance.
    # Synthetic leading lane for tasks a workflow has claimed (status
    # in_progress) but not yet advanced into review_previous_notes — keeps
    # just-started work visible on /conductor instead of invisible. NOT a real
    # WORKFLOW_STEPS entry (advance/gate state machine is untouched); display
    # only. The first conductor_advance moves the task into review_previous_notes.
    INTAKE_STEP = "intake"

    _TYPICAL_S_FALLBACK = 900.0  # 15 min — positive default when no history
    # Floor for the wall-clock token fallback window: a young task whose work is
    # already on disk (just-linked unresolvable MCP handle, #137) gets at least
    # this much lookback so the burn graph isn't an empty [created, now] window.
    _FALLBACK_LOOKBACK_S = 6 * 3600.0  # 6h

    def _project_source_path(self) -> str:
        """Resolve this project's source tree from the scores.db location
        (.../projects/<project_id>/scores.db) so phase_progress can read the
        live transcript on disk. Cached; '' when not in folder mode."""
        cached = getattr(self, "_src_path_cache", None)
        if cached is not None:
            return cached
        src = ""
        try:
            from pathlib import Path
            from prism_service.services.claude_transcripts import _project_source_path
            project_id = Path(self._scores_db).parent.name
            src = _project_source_path(project_id) or ""
        except Exception:
            src = ""
        self._src_path_cache = src
        return src

    def _project_override_dir(self) -> str:
        """Resolve this project's explicit Claude transcript dir
        (claude_project_dir / override_dir) so the live token read works for a
        folder-mode project whose source_path is empty or cwd-mismatched (#134).
        Cached; '' when unconfigured."""
        cached = getattr(self, "_override_dir_cache", None)
        if cached is not None:
            return cached
        val = ""
        try:
            from pathlib import Path
            from prism_service.services import claude_memory
            project_id = Path(self._scores_db).parent.name
            val = claude_memory.configured_project_dir(project_id) or ""
        except Exception:
            val = ""
        self._override_dir_cache = val
        return val

    @staticmethod
    def _now_epoch() -> float:
        """Server clock as POSIX seconds (UTC)."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).timestamp()

    def _task_window_start(self, task_id: str) -> float:
        """Earliest history timestamp for the task (its work window start) so
        the wall-clock fallback brackets the FULL task span, not just the
        current step. Falls back to (now - in_step_s) when history is empty."""
        if self._task_svc is not None:
            try:
                rows = self._task_svc.history(task_id)
                stamps = [self._parse_iso(getattr(r, "timestamp", "") or "")
                          for r in rows]
                stamps = [s for s in stamps if s is not None]
                if stamps:
                    return min(stamps)
            except Exception:
                pass
        return self._now_epoch() - self._in_step_s(task_id)

    @staticmethod
    def _parse_iso(ts: str) -> Optional[float]:
        """ISO-8601 timestamp -> epoch seconds; None if unparseable."""
        if not ts:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts).timestamp()
        except Exception:
            return None

    def _median_step_s(self) -> float:
        """Median gap between consecutive advance_task rows across all task
        history — the empirical 'typical' time a task dwells in one step.
        Falls back to a positive constant when there is no history yet."""
        if self._task_svc is None:
            return self._TYPICAL_S_FALLBACK
        try:
            tasks = self._task_svc.list()
        except Exception:
            return self._TYPICAL_S_FALLBACK
        gaps: list[float] = []
        for t in tasks:
            try:
                rows = self._task_svc.history(t.id)
            except Exception:
                continue
            advs = [self._parse_iso(getattr(r, "timestamp", "") or "")
                    for r in rows
                    if getattr(r, "action", "") == "advance_task"]
            advs = [a for a in advs if a is not None]
            for a, b in zip(advs, advs[1:]):
                if b > a:
                    gaps.append(b - a)
        if not gaps:
            return self._TYPICAL_S_FALLBACK
        gaps.sort()
        n = len(gaps)
        mid = n // 2
        med = gaps[mid] if n % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        return med if med > 0 else self._TYPICAL_S_FALLBACK

    def _per_step_typical(self) -> tuple[dict, dict]:
        """Per-step median dwell time, LEARNED from advance_task history (so it
        sharpens as tasks complete). Returns ({step: median_s}, {step: n}). A
        step's dwell = gap between the advance INTO it and the next advance;
        the gap is attributed to the to-step of the EARLIER advance row."""
        import re as _re
        out: dict = {}
        counts: dict = {}
        if self._task_svc is None:
            return out, counts
        try:
            tasks = self._task_svc.list()
        except Exception:
            return out, counts
        buckets: dict = {}
        for t in tasks:
            try:
                rows = self._task_svc.history(t.id)
            except Exception:
                continue
            advs = []
            for r in rows:
                if getattr(r, "action", "") != "advance_task":
                    continue
                ts = self._parse_iso(getattr(r, "timestamp", "") or "")
                m = _re.search(r"to=(\w+)", getattr(r, "details", "") or "")
                if ts is not None and m:
                    advs.append((ts, m.group(1)))
            for (a_ts, a_step), (b_ts, _b) in zip(advs, advs[1:]):
                if b_ts > a_ts:
                    buckets.setdefault(a_step, []).append(b_ts - a_ts)
        for step, gaps in buckets.items():
            gaps.sort()
            n = len(gaps)
            mid = n // 2
            med = gaps[mid] if n % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
            if med > 0:
                out[step] = med
                counts[step] = n
        return out, counts

    def _eta_s(self, current_step: str, in_step_s: float,
               global_typical: float) -> tuple[Optional[float], int, Optional[float]]:
        """Forward-projected seconds remaining to the terminal gate: per-step
        learned medians for the current + remaining WORKFLOW_STEPS (global
        median fallback for a step with < 2 samples). Returns
        (eta_s, sample_n, total_s) — sample_n is the current step's sample count
        (confidence); total_s is the full-SDLC budget (the countdown bar drains
        eta_s against it). Recomputed every call, so it auto-sharpens."""
        try:
            from prism_service.models.workflow import WORKFLOW_STEPS
            steps = [s["id"] for s in WORKFLOW_STEPS]
        except Exception:
            return None, 0, None
        if current_step not in steps:
            return None, 0, None
        per, counts = self._per_step_typical()

        def typ(step: str) -> float:
            v = per.get(step)
            return v if (v and counts.get(step, 0) >= 2) else global_typical

        idx = steps.index(current_step)
        remaining = max(0.0, typ(current_step) - in_step_s)
        for s in steps[idx + 1:]:
            remaining += typ(s)
        total = sum(typ(s) for s in steps)
        return remaining, counts.get(current_step, 0), total

    def _in_step_s(self, task_id: str) -> float:
        """Seconds since the most recent advance_task row for this task —
        how long it has dwelt in the current step. 0.0 when unknown."""
        if self._task_svc is None:
            return 0.0
        try:
            rows = self._task_svc.history(task_id)
        except Exception:
            return 0.0
        last: Optional[float] = None
        latest: Optional[float] = None
        for r in rows:
            ts = self._parse_iso(getattr(r, "timestamp", "") or "")
            if ts is None:
                continue
            if latest is None or ts > latest:
                latest = ts
            if getattr(r, "action", "") == "advance_task":
                last = ts
        if last is None:
            return 0.0
        from datetime import datetime, timezone
        # FREEZE for terminal tasks: a done/cancelled task must not keep
        # accruing dwell against wall-clock forever (task 7bdb5701 read 5.5h+
        # in green_gate hours after completing). End at completed_at, falling
        # back to the last recorded history event.
        end: Optional[float] = None
        try:
            _t = self._task_svc.get(task_id)
            status = (getattr(_t, "status", "") or "") if _t else ""
            if status in ("done", "cancelled"):
                end = self._parse_iso(getattr(_t, "completed_at", "") or "") if _t else None
                if end is None:
                    end = latest
        except Exception:
            end = None
        if end is None:
            end = datetime.now(timezone.utc).timestamp()
        elapsed = end - last
        return elapsed if elapsed > 0 else 0.0

    def phase_progress(self, task_id: str) -> dict:
        """Blended estimate of how far through the CURRENT workflow step a
        task is. Shape:
          {pct, basis, in_step_s, typical_s,
           children_done, children_total, tokens_since_step}
        - baseline (basis='time'): min(0.95, in_step_s / typical_s) from the
          median step history; the 0.95 ceiling means it never reads 'done'
          before the actual advance.
        - override (basis='children'): when child tasks exist (parent_id ==
          task_id), pct is the exact children_done/children_total ratio.
        """
        typical_s = self._median_step_s()
        in_step_s = self._in_step_s(task_id)

        # ETA — forward projection over the remaining steps (learned per-step
        # medians; sharpens over time). Only meaningful while in the workflow.
        cur_step = ""
        task_status = ""
        if self._task_svc is not None:
            try:
                _t = self._task_svc.get(task_id)
                if _t:
                    cur_step = (getattr(_t, "workflow_step", "") or "")
                    task_status = (getattr(_t, "status", "") or "")
            except Exception:
                cur_step = ""
        eta_s, eta_n, eta_total = self._eta_s(cur_step, in_step_s, typical_s)

        children_done = 0
        children_total = 0
        if self._task_svc is not None:
            try:
                for t in self._task_svc.list():
                    if (getattr(t, "parent_id", "") or "") == task_id:
                        st = (getattr(t, "status", "") or "")
                        # CANCELLED children (e.g. the implement workflow's
                        # disposable ephemeral-fixture tasks) are abandoned, not
                        # pending work — counting them in the denominator dragged
                        # a green-gated parent's tile to 0% (task 7bdb5701).
                        if st == "cancelled":
                            continue
                        children_total += 1
                        if st == "done":
                            children_done += 1
            except Exception:
                children_total = 0

        # Per-step ephemeral fanout (test-writers etc.) for the CURRENT step —
        # a real returned/dispatched signal that beats the wall-clock estimate.
        fanout_dispatched, fanout_returned = self._step_fanout(task_id, cur_step)

        # A finished task is 100%, period — never a stale children/time ratio.
        if task_status == "done":
            pct = 1.0
            basis = "done"
        elif children_total > 0:
            pct = children_done / children_total
            basis = "children"
        elif fanout_dispatched > 0:
            # write_failing_tests has no child tasks, so fanout wins over time.
            pct = fanout_returned / fanout_dispatched
            basis = "fanout"
        else:
            ratio = in_step_s / typical_s if typical_s > 0 else 0.0
            pct = min(0.95, ratio)
            basis = "time"

        # Real token effort, summed across the sessions linked to this task.
        # session_outcomes.tokens_used is only written once a session is
        # imported (post-hoc), so an IN-PROGRESS task reads 0 there — we also
        # read the LIVE transcript on disk and take the larger of the two, so
        # a running session ("seeing ourselves") shows a real, growing number.
        tokens = 0
        # Per-turn burn series (oldest..newest) merged across the task's live
        # sessions — the conductor tile's "work getting done" graph. Each entry
        # is {out, dt_s, tok_s}; complementary to the cumulative `tokens_since_
        # step` (the graph is the derivative, the number is the integral — no
        # duplication). Empty for a task with no on-disk transcript yet.
        token_turns: list[dict] = []
        total_turns = 0
        # Recency of the linked session's live transcript: server-now minus the
        # newest token event across the task's sessions. Powers activity_for's
        # 'adrift' vs 'stalled' split (session alive but task idle). None when
        # there is no on-disk transcript. Server-side now() is fine here — this
        # is the daemon reading its own clock, not a workflow script.
        session_quiet_s: Optional[float] = None
        # 'linked' = series came from authoritative per-session live events (or
        # is honestly empty); 'wallclock' = the project-wide fallback supplied
        # it. The SPA dims/labels the 'wallclock' case as approximate.
        tokens_source = "linked"
        if self._task_svc is not None:
            try:
                source_path = self._project_source_path()
                override_dir = self._project_override_dir()
                # Fire the live read when EITHER a source_path OR an explicit
                # claude_project_dir (override_dir) is set — a folder-mode
                # project with no source_path still reads tokens via the
                # registered transcript dir (#134).
                live_enabled = bool(source_path or override_dir)
                live_fn = None
                events_fn = None
                turns_from = None
                if live_enabled:
                    from prism_service.services.claude_transcripts import (
                        live_tokens_for_session as live_fn,
                        live_token_events_for_session as events_fn,
                        token_turns_from_events as turns_from,
                    )
                # Build the FULL uncapped per-turn event timeline across the
                # task's live sessions; total_turns is computed off the UNCAPPED
                # series so `turns` stays honest even past the 40-turn tail cap.
                live_events: list[tuple[float, int]] = []
                for s in self._task_svc.sessions_for_task(task_id):
                    sid = s.get("session_id") if isinstance(s, dict) else getattr(s, "session_id", "")
                    used = s.get("tokens_used") if isinstance(s, dict) else getattr(s, "tokens_used", 0)
                    outcome_tok = int(used or 0)
                    live_tok = 0
                    if live_fn and sid:
                        try:
                            live_tok = int(live_fn(sid, source_path, override_dir=override_dir) or 0)
                        except Exception:
                            live_tok = 0
                    tokens += max(outcome_tok, live_tok)
                    if events_fn and sid:
                        try:
                            live_events.extend(events_fn(sid, source_path, override_dir=override_dir) or [])
                        except Exception:
                            pass
                live_events.sort(key=lambda e: e[0])
                if live_events:
                    session_quiet_s = max(0.0, self._now_epoch() - live_events[-1][0])
                # PER-TASK-EXCLUSIVE (owner decision, task 5ecbbfb8): the tile
                # shows ONLY this task's authoritative linked-session activity.
                # The old #134/#137 project-WIDE wall-clock fallback (bucket the
                # project's transcript spend across the task window when the
                # task's only links are unresolvable MCP handles) is DROPPED —
                # it painted parked/worked tiles with another task's burn and
                # produced the "2625 turns / 0 tokens" contradiction. With no
                # authoritative live_events the series stays honestly empty:
                # tokens_source=='linked', token_turns==[], turns==0,
                # tokens_since_step==0. The empty state ("awaiting first turn")
                # is the honest render; never borrow project-wide activity.
                if turns_from:
                    full_turns = turns_from(live_events)
                    total_turns = len(full_turns)
                    token_turns = full_turns[-40:]
            except Exception:
                tokens = 0

        return {
            "pct": round(max(0.0, min(1.0, pct)), 6),
            "basis": basis,
            "in_step_s": round(in_step_s, 3),
            "typical_s": round(typical_s, 3),
            # Forward-projected seconds remaining to the terminal gate, + the
            # current step's sample count (confidence). null when not in-flow.
            "eta_s": round(eta_s, 1) if eta_s is not None else None,
            "eta_sample_n": eta_n,
            "eta_total_s": round(eta_total, 1) if eta_total is not None else None,
            "children_done": children_done,
            "children_total": children_total,
            # Ephemeral per-step sub-agent fanout (0/0 when the step dispatched
            # no disposable units) — powers the "N/M agents back" chip.
            "fanout_dispatched": fanout_dispatched,
            "fanout_returned": fanout_returned,
            # Task-total linked-session tokens (see note above).
            "tokens_since_step": tokens,
            # Per-turn burn rate series + honest total turn count.
            "token_turns": token_turns,
            "turns": total_turns,
            # 'linked' (authoritative/empty) | 'wallclock' (project-wide
            # approximate fallback — the SPA dims + labels it).
            "tokens_source": tokens_source,
            # Recency of the linked session's live transcript (s). None when no
            # transcript. activity_for reads this for the adrift/stalled split.
            "session_quiet_s": round(session_quiet_s, 3) if session_quiet_s is not None else None,
        }

    # ------------------------------------------------------------------
    # activity — the HONEST per-task work state (not the raw status)
    # ------------------------------------------------------------------
    def _task_motion_s(self, task) -> Optional[float]:
        """Seconds since the last CONDUCTOR TRANSITION on this task: the newest
        advance_task/gate_decide row in task_history (both written by
        advance_task/gate_decide via TaskService.record_history). Falls back to
        task.updated_at ONLY when the task has no transition history. None when
        neither resolves."""
        latest: Optional[float] = None
        tid = getattr(task, "id", "") or ""
        if self._task_svc is not None and tid:
            try:
                for r in self._task_svc.history(tid):
                    if getattr(r, "action", "") in ("advance_task", "gate_decide"):
                        ts = self._parse_iso(getattr(r, "timestamp", "") or "")
                        if ts is not None and (latest is None or ts > latest):
                            latest = ts
            except Exception:
                latest = None
        # A SUB-TASK completing/moving IS the parent moving, even though the
        # parent's own step didn't transition — otherwise an epic reads "stalled"
        # for hours while its slices are actively getting done underneath it.
        if self._task_svc is not None and tid:
            try:
                for c in self._task_svc.list():
                    if (getattr(c, "parent_id", "") or "") != tid:
                        continue
                    for fld in ("completed_at", "updated_at"):
                        ts = self._parse_iso(getattr(c, fld, "") or "")
                        if ts is not None and (latest is None or ts > latest):
                            latest = ts
            except Exception:
                pass
        if latest is None:
            latest = self._parse_iso(getattr(task, "updated_at", "") or "")
        if latest is None:
            return None
        return max(0.0, self._now_epoch() - latest)

    def _children(self, task) -> list:
        """Non-cancelled child tasks of this task (parent_id == task.id)."""
        tid = getattr(task, "id", "") or ""
        if self._task_svc is None or not tid:
            return []
        try:
            return [c for c in self._task_svc.list()
                    if (getattr(c, "parent_id", "") or "") == tid
                    and (getattr(c, "status", "") or "") != "cancelled"]
        except Exception:
            return []

    def activity_for(self, task, phase_progress: dict) -> dict:
        """Honest {state, task_motion_s, session_quiet_s} for a task. 'working'
        means a REAL recent conductor transition on THIS task (<=120s); when
        uncertain we UNDER-claim (stalled/adrift over working). session_quiet_s
        rides in on the already-computed phase_progress dict (transcript recency)
        so we don't re-read the transcript."""
        status = (getattr(task, "status", "") or "")
        step = (getattr(task, "workflow_step", "") or "")
        gate = (getattr(task, "gate_state", "none") or "none")
        motion = self._task_motion_s(task)
        quiet = phase_progress.get("session_quiet_s") if isinstance(phase_progress, dict) else None
        if status == "done":
            state = "done"
        elif status == "blocked":
            state = "blocked"
        elif status == "pending":
            state = "pending"
        elif status == "in_progress":
            kids = self._children(task)
            if kids:
                # An EPIC's activity is its slices': a slice actively moving =>
                # working; some slices done but none active => paused (real
                # progress, between bursts — NOT the alarming "stalled");
                # nothing done and nothing active => stalled.
                active = any((getattr(k, "status", "") or "") == "in_progress"
                             and (self._task_motion_s(k) or 1e9) <= 120 for k in kids)
                done = sum(1 for k in kids if (getattr(k, "status", "") or "") == "done")
                if active:
                    state = "working"
                elif done > 0:
                    state = "paused"         # progress made, idle between slices
                else:
                    state = "stalled"
            elif step.endswith("_gate") and gate in ("pending", "failed"):
                state = "awaiting_gate"      # a WAIT for review, not work
            elif motion is not None and motion <= 120:
                state = "working"            # a real recent transition on THIS task
            elif quiet is not None and quiet <= 90:
                state = "adrift"             # session alive but busy elsewhere
            else:
                state = "stalled"            # nothing is driving it
        else:
            state = status or "pending"
        return {
            "state": state,
            "task_motion_s": round(motion, 3) if motion is not None else None,
            "session_quiet_s": round(quiet, 3) if isinstance(quiet, (int, float)) else None,
        }
