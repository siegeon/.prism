"""Conductor service — wrapper over the Conductor engine with scores.db queries."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from prism_service.services import drive_heartbeat, sqlite_db


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


# The conductor's own gate-deciding identity (owner directive 2026-07-15:
# customers cannot click our board — a green_gate whose oracle the server can
# run itself must clear itself). Distinct from every producing session by
# construction, same trust model as the story/plan 'conductor-autoclear' seat.
ADJUDICATOR_SEAT = "conductor-adjudicator"

# Machine JUDGE seats are never work PRODUCERS (defect found 2026-07-16 on
# d09bee0b: the adjudicator's red_gate decision stamped it into the task's
# sessions, so its green_gate approval was refused as self-review and the
# gate flipped failed). Seats in this set are excluded from session stamping
# and from the distinct-actor producer set — a judge deciding two gates on
# one task is not reviewing its own work.
MACHINE_SEATS = frozenset(
    {ADJUDICATOR_SEAT, "conductor-autoclear", "gate-card-rerun"})
# Task 8582921d: how many CONSECUTIVE machine rewinds (green_gate -> back
# to implement/verify) may happen before the seat parks loudly for a human.
MAX_AUTO_REWINDS = 3


# DELIVERY AUTO-MERGE (task cb1dc6f4, owner 2026-07-29: "i approved the green
# gate ... make sure to fix prism so that when approved we finish the
# pipeline there"). SHIPS OFF BY DEFAULT, mirroring gate_adjudicator's
# PRISM_GATE_ADJUDICATOR_INTERVAL switch: an environment opts in with
# PRISM_DELIVERY_AUTOMERGE=1 before a green_gate approve starts landing a
# task's own branch on local main unattended. ConductorService.deliver_task
# itself is unconditional and directly callable by tests/tools — only the
# AUTOMATIC trigger wired into gate_decide honors this switch, so approving
# THIS task's own green_gate cannot merge its branch into the real repo's
# main unless an owner has explicitly turned delivery on.
def _delivery_enabled() -> bool:
    raw = os.environ.get("PRISM_DELIVERY_AUTOMERGE", "").strip().lower()
    return raw in ("1", "true", "on", "yes")


def _red_pytest_spec(task: object):
    """The spec used to DEMONSTRATE RED — always a pytest concern.

    Task a9215794 (owner 2026-07-21). Red and completion are different
    questions and were wrongly sharing one spec. ``OracleSpec.from_task``
    describes how the FINISHED outcome is proved, so a ticket whose oracle
    names a URL derives ``http_probe`` — right for completion, useless for
    red, because a red gate must observe the PINNED TESTS failing at the red
    anchor. Worse, the old red paths ALSO demanded ``proof_type == "test"``,
    so task 89e90d1a — proof_type=artifact (its proof is browser
    screenshots) with a textbook tests-only anchor at 53c23a4 — could never
    be machine-decided and dead-ended at red_gate forever.

    Resolve red from the task's PINNED TEST material instead, independent of
    proof_type. Returns None when there is no pytest material, which keeps
    the honest boundary: a demo/browser ticket with nothing to run still has
    no machine red and correctly stays with a human. This widens WHICH
    tickets the seat will attempt; it does NOT weaken the proof burden — the
    caller still requires the trusted runner to observe those tests FAILING
    at a tests-only anchor before red is granted.
    """
    from prism_service.services import oracle_spec as osp
    try:
        ids = osp._pytest_ids_from_task(task)
        if not ids:
            return None
        return osp.OracleSpec._pytest_spec(ids)
    except Exception:
        return None


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
#
# NOTE (task 9afd1b72): the bare word "screenshot" is DELIBERATELY excluded
# here — "I took a screenshot" with no path/extension/host is gameable
# self-attested prose, not proof. A concrete signal (a file extension, an
# agent-browser/Playwright citation, or a dev-surface host:port) is trusted
# on its own; a bare mention with none of those must instead be corroborated
# by a REAL captured file via has_captured_evidence (evidence_dir on disk,
# or a fresh EvidenceReceipt's artifacts[] naming a file that exists) at the
# gate-decide call sites below.
_UI_ARTIFACT_SIGNALS = (
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg",
    "agent-browser", "playwright", ":8888", "127.0.0.1:8888",
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


# A "surface" the oracle names for a person to look at, distinct from a
# METRIC that merely mentions the word "surface" in prose (task 8a737f2f
# test A2: "default MCP tool surface 41 -> 31" must NOT trip this). Keyed on
# concrete, machine-recognizable locators a person actually navigates to —
# a URL, the dev :8888 host, or an explicit route/nav-entry phrase — never
# on the bare English word "surface".
_SCREEN_LOCATOR_SIGNALS = (
    "http://", "https://", "127.0.0.1", ":8888", "/tasks/", "nav entry",
    "landing page",
)


def _screen_claim_gate_reason(tags: object, proof_type: object,
                             oracle: object) -> str:
    """A test-proof ticket cannot claim a screen (task 8a737f2f, HOLE 1).

    Narrows FR-4's proof_type opt-out (ui_artifact_gate_reason above): that
    tooth lets ANY declared non-demo proof_type (typically 'test') bypass
    the demonstrable-UI requirement entirely, with no check of what the
    oracle TEXT actually claims. When a `ui`-tagged task declares a
    non-demo/non-empty proof_type AND its oracle names a user-reachable
    surface (a URL, the :8888 dev host, a route, a nav entry — something a
    PERSON looks at), a pytest receipt proves a suite ran, not what renders
    there — so this stays a HUMAN sign-off.

    Returns a non-empty reason when the shape is flagged, else "". Never
    fires for proof_type='demo' (STRAND C's own territory) or an unset
    proof_type (the existing demo/screenshot requirement already applies).
    Never fires for a non-`ui` task (out of scope, not this tooth's
    business).

    PRIVATE ON PURPOSE (`_` prefix): both production callers live in THIS
    module (adjudicate_green_gate and the human-judgment branch of
    gate_decide), so this is a module-internal helper like
    _metric_gate_reason / _artifact_path_reason / _unshipped_gate_reason,
    NOT an entry point. The public siblings here (ui_artifact_gate_reason,
    green_gate_artifact_reason, board_health) are public precisely because
    api/conductor.py imports them; publishing a name with no external
    consumer is the "construction site" reachability_check refuses. If a
    readiness/API surface ever needs this verdict, make it public IN THAT
    slice, together with the call site that consumes it."""
    tag_set = {str(t).strip().lower() for t in (tags or [])}
    if "ui" not in tag_set:
        return ""
    pt = str(proof_type or "").strip().lower()
    if not pt or pt == "demo":
        return ""
    text = str(oracle or "").lower()
    if not any(sig in text for sig in _SCREEN_LOCATOR_SIGNALS):
        return ""
    return (
        f"ui task declares proof_type={pt!r} (a machine-runnable receipt), "
        "but its oracle names a user-reachable SCREEN — a pytest/http "
        "receipt proves a suite ran, not what renders on that surface. "
        "This stays a HUMAN sign-off: a distinct person must open the "
        "surface and confirm (their own look at the screen is the "
        "evidence); a plain Approve from them still closes the gate."
    )


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


def has_captured_evidence(task_id: object, project: object = "") -> bool:
    """True iff the task has REAL captured evidence: at least one image/video
    file directly under data_dir/evidence/<id>/, OR (task 9afd1b72) a fresh
    EvidenceReceipt's ``artifacts[]`` naming a file that exists on disk — the
    mint may write a walkthrough capture the receipt cites without also
    landing it in the evidence_dir glob, so receipt.artifacts is consulted
    directly rather than trusting a self-attested completion_proof substring.
    Owner 2026-07-19 ('implement to evidence'): a captured screenshot/video in
    the task's evidence store SATISFIES the demonstrable-UI requirement — the
    user (or the drive) captures it, and the gate passes; no hand-cited path
    in completion_proof required."""
    try:
        from prism_service.data_dir import evidence_dir
        d = evidence_dir(str(task_id or ""))
        exts = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".webm")
        if any(p.is_file() and p.suffix.lower() in exts for p in d.glob("*")):
            return True
    except Exception:
        pass
    try:
        from prism_service.services import oracle_spec as _osp
        receipt = _osp.latest_receipt(str(project or "default"),
                                      str(task_id or ""))
        if receipt is not None:
            for art in (receipt.artifacts or []):
                path = art.get("path") if isinstance(art, dict) else None
                if path and Path(str(path)).is_file():
                    return True
    except Exception:
        pass
    return False


def demo_evidence_gate_reason(task: object, project: object = "") -> str:
    """A human-judgment oracle (proof_type=demo/review, or any browser/
    manual oracle per oracle_spec.is_human_judgment) is DEMONSTRATED, not
    measured -- someone watches something happen and captures it. Returns
    a non-empty refusal reason when the task's oracle is human-judgment
    AND has_captured_evidence() is False; returns "" (no objection)
    otherwise, including for any non-human-judgment task.

    PUBLIC ON PURPOSE (task 3baadd19 qa discovery, 2026-08-24): unlike
    most single-caller teeth in this module, this one has TWO real
    production callers by design -- ConductorService.advance_task (the
    enforcement path, refuses the verify_green_state -> green_gate
    advance) and api/workflows.py's green-gate-status governance-
    visibility endpoint (the reporting path, so what a human sees on the
    Workflows page can never drift from what is actually enforced -- both
    read the SAME function, never two independent judgments of the same
    question).

    Live regression this closes: task 3baadd19 (proof_type=demo, tagged
    conductor/architecture/owner-directive/drive-worker/github/jira -- no
    "ui" tag) reported SUCCESS at verify_green_state with completion_proof
    that admitted "the epic's actual oracle... is NOT yet true in
    production" and ZERO captured evidence, and advanced to green_gate
    anyway -- because ui_artifact_gate_reason (the only prior evidence
    tooth) is scoped to "ui"-tagged tasks and never ran."""
    pt = str(getattr(task, "proof_type", "") or "").strip().lower()
    is_demo_claim = pt in ("demo", "review")
    if not is_demo_claim:
        try:
            from prism_service.services import oracle_spec as _osp
            is_demo_claim = _osp.is_human_judgment(
                _osp.OracleSpec.from_task(task))
        except Exception:
            is_demo_claim = False
    if not is_demo_claim:
        return ""
    task_id = str(getattr(task, "id", "") or "")
    if has_captured_evidence(task_id, project or ""):
        return ""
    return (
        f"verify_green_state: proof_type={pt!r} means this oracle is "
        "DEMONSTRATED, not measured -- but no captured screenshot/video "
        "exists in the evidence store for this task, and no oracle "
        "receipt carries an artifact either. A self-attested demo claim "
        "cannot advance to green_gate alone: capture real evidence "
        "(agent-browser/verify screenshot, a recording of the drive) "
        "into the evidence store and re-report."
    )


def gate_artifact_reason(gate_step_id: str, completion_proof: object,
                         reason: object, proof_type: object = None) -> str:
    """Dispatch the proof-carrying artifact check by gate + proof_type. ""
    when the gate has no artifact tooth (only red_gate/green_gate do today)."""
    if gate_step_id == "red_gate":
        return red_gate_artifact_reason(completion_proof, reason, proof_type)
    if gate_step_id == "green_gate":
        return green_gate_artifact_reason(completion_proof, reason, proof_type)
    return ""


def _resolve_actor_identity(raw: str):
    """Resolve `raw` to an Actor via the identity resolver introduced by the
    team-work-hub epic (models/actor.py, services/actor_service.py, task
    0784729f, branch prism/ws/0784729f-5e34-4195-87db-5b54f8ad91cc). A real
    module-not-found here means that branch has not merged into this
    checkout yet — callers must treat that as ImportError specifically (see
    same_actor_override_reason), never swallow it here. This is its own
    function (rather than inlined) so tests can monkeypatch the seam without
    the real resolver existing."""
    from prism_service.services.actor_service import get_actor_service
    return get_actor_service().resolve(raw)


def same_actor_override_reason(override_actor: object,
                               work_actors: object) -> str:
    """NO SELF-OVERRIDE (task 3826dac3), joined on IDENTITY not text (task
    a4e41c35). A gate cannot be cleared by the SAME actor that produced the
    work — the driver overriding its own gate is forbidden. Override is
    demoted to a distinct-actor exception: an independent verifier
    sub-agent (fresh context) must be the one to override. Returns a
    REJECTION reason when override_actor resolves to the same Actor as a
    work-producing actor, else "" (a distinct actor is allowed).

    Two DIFFERENT raw strings (a session id vs. that same actor's email, or
    two aliases logged by two callers) can be the SAME real actor — a plain
    text compare lets that same-actor override slip through as "distinct".
    _resolve_actor_identity() joins on prism_service.models.actor.Actor.id
    when the epic's resolver is wired into this checkout; when it is not
    (ImportError, checked ONCE per call pair, not per-row) this falls back
    to the exact pre-join case-insensitive string compare for BOTH sides —
    never a per-row fallback, which would defeat the join for precisely the
    case it exists to catch. Once the resolver IS wired, an unexpected
    resolve() failure (it is documented to never raise, but should one ever
    happen) fails CLOSED — refuses the override — rather than falling back
    to a string compare that could read a same-actor bypass as "distinct"
    (the exact regression this join exists to prevent)."""
    actor_raw = str(override_actor or "").strip()
    if not actor_raw:
        return ""
    produced_raw = [s for s in (str(a or "").strip() for a in (work_actors or []))
                    if s]
    if not produced_raw:
        return ""

    try:
        overriding_identity = _resolve_actor_identity(actor_raw)
    except ImportError:
        # STRUCTURAL: the resolver module is not present in this checkout
        # (epic branch not merged) — the identity join cannot run at all,
        # so use the untouched legacy string compare for the whole call.
        produced_l = {a.lower() for a in produced_raw}
        if actor_raw.lower() in produced_l:
            return (f"same-actor override forbidden: actor {override_actor!r} "
                    "produced the work on this task and cannot clear its "
                    "own gate — an independent verifier (distinct actor) "
                    "must re-run the claimed command and override")
        return ""
    except Exception:
        return (f"same-actor override forbidden: identity of actor "
                f"{override_actor!r} could not be verified as distinct "
                "(actor lookup failed) — refusing rather than risk a "
                "same-actor bypass")

    for raw in produced_raw:
        try:
            producer_identity = _resolve_actor_identity(raw)
        except Exception:
            return (f"same-actor override forbidden: identity of producer "
                    f"{raw!r} could not be verified as distinct (actor "
                    "lookup failed) — refusing rather than risk a "
                    "same-actor bypass")
        if producer_identity.id == overriding_identity.id:
            return (f"same-actor override forbidden: actor {override_actor!r} "
                    f"resolves to the same identity as producer {raw!r} "
                    f"({overriding_identity.display_name!r}) and cannot "
                    "clear its own gate — an independent verifier (distinct "
                    "actor) must re-run the claimed command and override")
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
        conn = sqlite_db.connect(self._scores_db, timeout=5.0)
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
    #
    # Per-task workflow lookup (task 6f22d0ad): the step LIST resolves via
    # models.workflow.steps_for(task.workflow) — a blank/"implement"/unknown
    # value still walks WORKFLOW_STEPS, the default sequence, byte-for-byte
    # unchanged; task.workflow="triage" walks TRIAGE_STEPS instead. Every
    # helper below defaults its `workflow` arg to "implement" so an external
    # caller that never passes one is unaffected.

    @staticmethod
    def _workflow_steps(workflow: str = "implement") -> list[dict]:
        """Resolve `workflow`'s step list via models.workflow.steps_for
        (task 6f22d0ad) — a blank/"implement"/unknown value returns
        WORKFLOW_STEPS itself (steps_for's own default fallback), so every
        caller that leaves `workflow` at its default sees byte-for-byte the
        same list as before this task. Local import avoids a circular dep
        with models.workflow."""
        from prism_service.models.workflow import steps_for

        return steps_for(workflow)

    @classmethod
    def _step_index(cls, step_id: str, workflow: str = "implement") -> int:
        """Return the position of step_id in `workflow`'s step list, or -1.

        An empty step_id means the task has not entered the workflow,
        which is equivalent to index -1 (the next step is index 0).
        """
        if not step_id:
            return -1
        for i, step in enumerate(cls._workflow_steps(workflow)):
            if step["id"] == step_id:
                return i
        return -1

    @classmethod
    def _step_by_id(cls, step_id: str, workflow: str = "implement") -> Optional[dict]:
        if not step_id:
            return None
        for step in cls._workflow_steps(workflow):
            if step["id"] == step_id:
                return step
        return None

    def advance_task(
        self,
        task_id: str,
        validation: Optional[str] = None,
        session_id: Optional[str] = None,
        model: Optional[str] = None,
        usage: Optional[dict] = None,
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

        from prism_service.models.task import normalize_workflow
        task_workflow = normalize_workflow(getattr(task, "workflow", "") or "")
        steps = self._workflow_steps(task_workflow)
        if not steps:
            return {"ok": False, "task_id": task_id,
                    "reason": "WORKFLOW_STEPS is empty"}

        current_id = task.workflow_step or ""
        current_step = self._step_by_id(current_id, task_workflow)

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

        # AGENT-STEP rubric validation (task 3a63190b / issue #222): a
        # validation kind whose _VERIFIER_RULES entry carries
        # check_at_step=True is scored HERE, at the step's OWN report,
        # rather than deferred to a downstream gate — premise_grounded on
        # review_previous_notes is the first such kind (draft_story's own
        # story_complete validation would otherwise shadow it for
        # story_gate's inheritance; see _validation_for_gate). A failing
        # score refuses the advance in place and records an actionable
        # gate_reason so a driver polling conductor_work/task_list can
        # self-diagnose, exactly like a pending rubric gate does.
        if current_step is not None and current_step.get("type") == "agent":
            step_validation = current_step.get("validation")
            rule = (self._VERIFIER_RULES.get(step_validation)
                    if step_validation else None)
            if rule and rule.get("rubric") and rule.get("check_at_step"):
                check = self._verify_rubric_gate(task, step_validation)
                if check.get("verified") is not True:
                    reason = (check.get("reason", "")
                              or f"{step_validation}: not verified")
                    self._task_svc.update(task_id, gate_reason=reason)
                    self._task_svc.record_history(
                        task_id, action="advance_refused",
                        details=(f"step={current_id}; "
                                 f"validation={step_validation}; "
                                 f"reason={reason}"),
                        actor=session_id or "")
                    return {
                        "ok": False,
                        "task_id": task_id,
                        "from_step": current_id,
                        "to_step": current_id,
                        "gate_state": task.gate_state,
                        "reason": reason,
                    }

        # DEMO/REVIEW EVIDENCE CHECK AT verify_green_state (task 3baadd19 qa
        # discovery, 2026-08-24): see demo_evidence_gate_reason's own
        # docstring for the full live regression. Checked HERE, at
        # advance_task's one choke point, so every caller (flow_report's
        # server-driven loop, the legacy conductor_advance MCP tool)
        # inherits it rather than patching each caller separately. The
        # SAME function also backs api/workflows.py's green-gate-status
        # reporting endpoint, so the Workflows page can never show a
        # different answer than what is actually enforced here.
        if current_id == "verify_green_state":
            reason = demo_evidence_gate_reason(task, self._project_name or "")
            if reason:
                self._task_svc.update(task_id, gate_reason=reason)
                self._task_svc.record_history(
                    task_id, action="advance_refused",
                    details=(f"step={current_id}; "
                             f"validation=demo-evidence; reason={reason}"),
                    actor=session_id or "")
                return {
                    "ok": False,
                    "task_id": task_id,
                    "from_step": current_id,
                    "to_step": current_id,
                    "gate_state": task.gate_state,
                    "reason": reason,
                }

        current_index = self._step_index(current_id, task_workflow)
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
                usage=usage,
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
        writer is unavailable — must never break a transition. Machine
        JUDGE seats are never stamped: a gate decision is not work on the
        task, and stamping one poisons the distinct-actor producer set
        for every later gate (d09bee0b, 2026-07-16)."""
        if not session_id or session_id in MACHINE_SEATS:
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
        started_at: Optional[float] = None, usage: Optional[dict] = None,
    ) -> None:
        """Best-effort per-role token attribution: write ONE agent_runs row
        for the just-completed step. role = models.roles.role_for_step(step);
        tokens = live-transcript output tokens in [started_at, now] (whole
        session when started_at is None). NEVER raises — a telemetry failure
        must not break a conductor transition."""
        try:
            import time as _time
            from prism_service.models.roles import role_for_step
            from prism_service.services.agent_runs_data import (
                session_tokens_total,
                upsert_agent_run,
            )

            role = role_for_step(step)
            now = _time.time()
            # CARRY, never re-derive (task 9a51e670). When the caller hands us
            # the run's own usage -- the authoritative whole-run figures
            # claude_cli._usage_from_result took from the single `result`
            # event -- that IS the answer and the transcript is not consulted.
            # The transcript branch only ever worked for real (UUID) human
            # sessions: a bot reports under a SEAT NAME that has no transcript
            # on disk, so re-deriving pinned every bot row at 0 regardless of
            # which project dir the meter watched. Reading BOTH would
            # double-count the same turn -- this task's named likely_misfire.
            usage = usage if isinstance(usage, dict) and usage else None
            tokens = 0
            cost_usd = None
            if usage:
                tokens = (int(usage.get("input_tokens") or 0)
                          + int(usage.get("output_tokens") or 0))
                cost_usd = float(usage.get("cost_usd") or 0.0)
                model = usage.get("model") or model
            else:
                try:
                    if session_id:
                        from prism_service.services import claude_transcripts
                        events = (
                            claude_transcripts.live_token_events_for_session(
                                session_id,
                                self._project_source_path(),
                                override_dir=self._project_override_dir(),
                            )
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
                "cost_usd": cost_usd,
                "tool_uses": None,
                "ok": ok,
                "gate_state": gate_state,
                "verdict_summary": verdict_summary,
                "evidence_ref": None,
            }
            upsert_agent_run(self._scores_db, row)
            # gamify walking skeleton: publish onto the bus so /sse/work and
            # the /live graph can add/update this session's node live.
            # Same try/except umbrella as the write above -- a telemetry
            # publish must never break a conductor transition.
            from prism_service.events import bus
            bus.publish({
                "project": self._project_name or "default",
                "type": "agent.run",
                "task_id": task_id,
                "session_id": session_id,
                "agent_id": row["agent_id"],
                "parent_agent_id": row.get("parent_agent_id"),
                "step": step,
                "role": role,
                "model": model,
                "ok": ok,
                "ts": now,
            })
            # A bot step has no human transcript, so work_stream's ticker can
            # never meter it -- publish its burn here instead, in the SAME
            # `tokens.turn` shape the ticker uses (work_stream.py:154-165) so
            # the existing /live consumer needs no change. Only when usage was
            # CARRIED: a transcript-attributed (human) step is already metered
            # by the ticker, and publishing here too would double-count it.
            if usage and tokens > 0:
                out_tokens = int(usage.get("output_tokens") or 0)
                dt_s = max(0.1, (duration_ms or 0) / 1000.0)
                bus.publish({
                    "project": self._project_name or "default",
                    "type": "tokens.turn",
                    "task_id": task_id,
                    "parent_id": self._parent_id_of(task_id),
                    "session_id": session_id or "",
                    "out_tokens": out_tokens,
                    "dt_s": round(dt_s, 2),
                    "tok_s": round(out_tokens / dt_s, 1),
                    # RUNNING per-session total, read back from the spine
                    # (this row included). graphState.ts:1057 discards any
                    # update whose total is lower than the node already holds,
                    # so a per-step figure would make later steps vanish.
                    "tokens_total": session_tokens_total(
                        self._scores_db, task_id, session_id or ""),
                    "ts": now,
                })
        except Exception:
            pass

    def _parent_id_of(self, task_id: str) -> str:
        """The task's parent id for the atomic card+wire contract, or "".
        Mirrors work_stream's per-task resolution; never raises."""
        try:
            obj = self._task_svc.get(task_id)
            return (getattr(obj, "parent_id", "") or "") if obj else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Gate verification helpers (issue #79 [3/4])
    # ------------------------------------------------------------------

    @classmethod
    def _validation_for_gate(cls, gate_step_id: str,
                             workflow: str = "implement") -> Optional[str]:
        """Return the validation kind the verifier should check at this
        gate. By convention, a gate inherits its expectation from the
        immediately preceding step's ``validation`` field
        (e.g. ``red_gate`` follows ``write_failing_tests`` whose
        validation is ``red_with_trace``)."""
        steps = cls._workflow_steps(workflow)
        idx = cls._step_index(gate_step_id, workflow)
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
        "premise_grounded": {
            "rubric": "premise_grounded",
            # (task 3a63190b / issue #222) UNLIKE story_complete/
            # plan_coverage — which are only ever consulted through a
            # downstream GATE's inheritance — premise_grounded must be
            # checked at the review_previous_notes AGENT step itself
            # (draft_story's own validation always shadows it for
            # story_gate's inheritance). check_at_step tells advance_task
            # to consult this rubric BEFORE letting the current step
            # advance, not just at a gate.
            "check_at_step": True,
            "expectation": (
                "premise_grounded is rubric-verified: every load-bearing "
                "claim in review_previous_notes carries a citation "
                "(file:line, run/PR/commit/issue id, or command output) "
                "or an explicit REFUTED/UNVERIFIED marker"
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

    def _uncommitted_changes_refusal(self, task) -> str:
        """green_gate MECHANICAL tooth: the task's own workspace must have
        ZERO uncommitted changes (scoped to allowed_files when set, the
        whole tree otherwise) before a fresh passing EvidenceReceipt is
        trusted. Deterministic, never an LLM call -- multiple requirements
        belong as separate CHECKS, not stacked into one inference prompt
        and trusted to be followed (owner 2026-08-21).

        Observed live on task 4f74dafc: a fully compliant EvidenceReceipt
        existed (osp.current_tree_sha hashes the WORKING TREE's content, so
        it's genuinely "fresh" for whatever is on disk, committed or not) --
        verify_green_state's tests ran clean against an uncommitted
        working-tree diff, the drive reached green_gate/status=done, and
        the actual implementation was never reachable from any commit at
        all. The 7.12.34 instruction fix (implement_tasks must commit)
        reduces how often this happens; it does not GUARANTEE it, since
        nothing stops a driver from ignoring an instruction. This is the
        guarantee. Returns "" (clear) or a refusal reason; never raises
        (fail closed on any git/subprocess error, same discipline as the
        oracle-receipt check this augments)."""
        import subprocess
        from prism_service.services import task_workspace
        task_id = getattr(task, "id", "")
        try:
            ws = task_workspace.workspace_for(task_id)
            path = (ws or {}).get("path") if ws else None
            if not path:
                return ""  # no workspace at all; the oracle-receipt tooth already refuses
            allowed = list(getattr(task, "allowed_files", None) or [])
            args = ["git", "status", "--porcelain"]
            if allowed:
                args += ["--"] + allowed
            result = subprocess.run(args, cwd=path, capture_output=True,
                                    text=True, timeout=10)
            if result.returncode != 0:
                return (f"green_gate: could not verify the workspace has no "
                        f"uncommitted changes (git status exit "
                        f"{result.returncode}: {result.stderr.strip()[:200]}) "
                        "— refusing (fail closed)")
            dirty = [ln for ln in result.stdout.splitlines() if ln.strip()]
        except Exception as exc:
            return (f"green_gate: could not verify the workspace has no "
                    f"uncommitted changes ({type(exc).__name__}: {exc}) — "
                    "refusing (fail closed)")
        if dirty:
            return (f"green_gate: {len(dirty)} uncommitted change(s) remain "
                    "in the task's own workspace — the implementation was "
                    "never committed, so it cannot be shipped even though "
                    "the pinned tests passed against it. Commit with a "
                    "[task:<id8>] trailer before this gate can clear.")
        return ""

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
            # ONE resolution path (task 68e5c699): the check resolves the
            # SAME workspace-anchored pin the mint stamped — see
            # control_plane.task_pin.
            _pin = _cp.task_pin(getattr(task, "id", ""))
            _policy_hash = _pin.get("policy_hash", "")
            fresh = osp.fresh_passing_receipt(
                project, getattr(task, "id", ""), tree_sha, spec.spec_hash(),
                policy_hash=_policy_hash)
            if fresh is not None:
                dirty_reason = self._uncommitted_changes_refusal(task)
                if dirty_reason:
                    return dirty_reason, None
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

    def adjudicate_green_gate(self, task_id: str,
                              mint: bool = True) -> Optional[dict]:
        """MACHINE ADJUDICATOR SEAT for a PENDING green_gate.

        Decides the gate as ``conductor-adjudicator`` — the distinct actor
        the flow's gate jobs ask for, filled by the server itself. Approves
        ONLY on the oracle-receipt tooth (a FRESH PASSING EvidenceReceipt:
        tree+spec+policy-pin matched). When the CURRENT tree+spec has no
        receipt at all and the derived oracle is machine-runnable
        (pytest_ids / http_probe), it exercises the oracle ONCE itself —
        the gate card's Re-run action, machine-initiated — then re-checks.
        One attempt per evidence state: a failing/tried oracle never loops
        and stays with a human.

        Never flips pending->failed: every other green_gate tooth
        (ui-artifact, candidate-controls-judge) is pre-flighted and any
        objection leaves the gate pending for a human. Epics (children),
        oracle-less tasks, and manual_evidence_required oracles are all
        left for a human too. Returns the gate_decide result on approve,
        else None."""
        if self._task_svc is None:
            return None
        task = self._task_svc.get(task_id)
        if task is None \
                or getattr(task, "workflow_step", "") != "green_gate":
            return None
        if getattr(task, "status", "") in ("cancelled", "archived",
                                           "deleted", "done"):
            return None
        gate_state = getattr(task, "gate_state", "")
        if gate_state == "failed":
            # Re-present ONLY a machine refusal artifact (a refused APPROVE
            # attempt — control-plane / same-actor / receipt tooth), never a
            # decision: an explicit human reject is final for this seat.
            if not self._failed_gate_is_refused_approve(task_id,
                                                        "green_gate"):
                return None
        elif gate_state != "pending":
            return None
        if not str(getattr(task, "oracle", "") or "").strip():
            return None
        # Epics stay with a human: their roll-up verdict reads children.
        # Unconditional on proof_type (task 457b38db, belt-and-suspenders
        # with the gate_decide consume-site fix): a demo/review
        # (human-judgment) epic with all children done abstains here too —
        # pinned by test_adjudicator_seat_abstains_for_demo_epic_with_children.
        try:
            if self._task_svc.list(parent_id=task_id):
                return None
        except Exception:
            return None
        # Pre-flight the OTHER green_gate teeth — adjudication may only
        # ever flip pending->passed, never pending->failed.
        if ui_artifact_gate_reason(getattr(task, "tags", None),
                                   getattr(task, "proof_type", ""),
                                   getattr(task, "completion_proof", "")):
            return None
        try:
            from prism_service.services import control_plane as _cp
            if _cp.candidate_controls_judge_reason(task):
                return None
        except Exception:
            return None
        # EXIT-half reachability tooth (task c944cac2): refuse a slice whose
        # diff adds a new production entry point with no non-test production
        # caller anywhere in the tree -- the "green tests, mocked collaborator,
        # nothing actually constructs it" shape. Reads the task's real
        # worktree GIT DIFF (never allowed_files, measured inert on 48/48
        # active tasks). Pre-flight, abstain-only like the two teeth above:
        # a truthy refusal PARKS pending with the reason (never failed).
        try:
            from prism_service.services import reachability_check
            _reach_reason = reachability_check.unreachable_entry_point_reason(
                task)
        except Exception:
            _reach_reason = ""
        if _reach_reason:
            self._park_green_refusal(task_id, _reach_reason)
            return None
        # PROMOTED-LAW TOOTH (task 2bfe49db, epic 61821448): a memory
        # promoted to a SHACL rule (services/law_promotion.py, task
        # c5650403) used to run only on a full ontology rebuild. This runs
        # the SAME promoted rule over the task's own diff, cheaply. Reads
        # ONLY the project's own promoted-shapes.ttl (services/law_check.py
        # docstring). Pre-flight, abstain-only like the tooth above.
        try:
            from prism_service.services import law_check
            _law_reason = law_check.law_violation_reason(
                task, self._project_name or "default")
        except Exception:
            _law_reason = ""
        if _law_reason:
            self._park_green_refusal(task_id, _law_reason)
            return None
        # SCREEN-CLAIM + SHIPPED-NESS PRE-FLIGHT (task 8a737f2f). Two
        # ABSTAIN-ONLY teeth, computed together so a single refusal names
        # BOTH counts when they co-occur (b22576bb replay, test C3):
        # screen-claim narrows FR-4 (HOLE 1 — a ui task's non-demo proof_type
        # opts out of the demo tooth even when its oracle names a screen a
        # pytest receipt cannot see); shipped-ness (HOLE 2) refuses to let
        # the machine stamp done on a trailer origin/main cannot see. Both
        # PARK pending with the reason recorded (never failed, task
        # e0149f1f's precedent) rather than silently `return None`. A
        # captured screenshot does NOT satisfy the screen-claim half (unlike
        # STRAND C above) — the oracle names something SEEN, not measured,
        # so this stays a human sign-off even with real evidence on file.
        _screen_reason = _screen_claim_gate_reason(
            getattr(task, "tags", None), getattr(task, "proof_type", ""),
            getattr(task, "oracle", ""))
        _ship_reason = self._unshipped_gate_reason(task)
        if _screen_reason or _ship_reason:
            self._park_green_refusal(
                task_id,
                " | ".join(r for r in (_screen_reason, _ship_reason) if r),
            )
            return None
        # UN-PARK (task 3baadd19 qa discovery, 2026-08-24): _park_green_refusal
        # above WRITES blocked_reason when this sweep objects, but nothing
        # symmetric ever CLEARED it once the objection stopped firing (e.g.
        # the branch this tooth was complaining about got landed) --
        # gate_reason self-heals every sweep via
        # gate_adjudicator._write_pending_reason, but blocked_reason does not,
        # so the "commit trailer not reachable" banner stayed on the task
        # forever after the trailer WAS reachable, with no code path to clear
        # it short of the owner clicking Approve (the ship-on-approve queue
        # path, conductor_service.py ~3894, is the only other blocked_reason=""
        # writer for this tooth). Scoped to status != "blocked" so this never
        # touches resume_actuator's unrelated dependency-retry park (which
        # always sets status="blocked" alongside its own blocked_reason,
        # never leaves status untouched the way this tooth does).
        if str(getattr(task, "blocked_reason", "") or "").strip() \
                and str(getattr(task, "status", "") or "") != "blocked":
            try:
                self._task_svc.update(task_id, blocked_reason="")
            except Exception:
                pass
        # Owner rule (2026-07-18, task eaafdf75): the machine seat signs off
        # ONLY an OBJECTIVE-OBSERVABLE oracle — a test suite passes
        # (pytest_ids) or an http probe returns ok (http_probe). Anything
        # validated VISUALLY — screenshots, videos, prototype/demo components,
        # "does this look right" — is a HUMAN judgment and stays PENDING for
        # the person: a render receipt proves the pixels exist, not that the
        # owner approves them. This is the single-sign-off the machine used to
        # eat (both 7cc4f0cf and a1e4120f auto-passed, one a false-green).
        try:
            from prism_service.services import oracle_spec as _osp
            _pt = str(getattr(task, "proof_type", "") or "").strip().lower()
            if _pt in ("demo", "review") \
                    or _osp.is_human_judgment(_osp.OracleSpec.from_task(task)):
                return None
        except Exception:
            return None
        refusal, receipt = self._oracle_receipt_refusal(
            task, override=False, reason="")
        _tried, _reminted = False, False
        if refusal and mint:
            try:
                from prism_service.services import oracle_spec as osp
                from prism_service.services import task_workspace
                spec = osp.OracleSpec.from_task(task)
                if spec.adapter not in (osp.ADAPTER_PYTEST,
                                        osp.ADAPTER_HTTP):
                    return None
                ws = task_workspace.workspace_for(task_id)
                tree = osp.current_tree_sha(
                    (ws or {}).get("path") if ws else None)
                tried = any(
                    r.tree_sha == tree and r.spec_hash == spec.spec_hash()
                    for r in osp.read_receipts(
                        self._project_name or "default", task_id))
            except Exception:
                return None
            _tried = tried
            if tried:
                self._evaluate_green_gate_rewind(task, tree, True, refusal,
                                                 False)
                return None
            self.mint_green_evidence(task_id, session_id=ADJUDICATOR_SEAT)
            _reminted = True
            refusal, receipt = self._oracle_receipt_refusal(
                task, override=False, reason="")
        if refusal or receipt is None:
            if refusal:  # task 8582921d: backward edge instead of abstaining
                try:
                    from prism_service.services import oracle_spec as _o
                    from prism_service.services import task_workspace as _tw
                    _ws = _tw.workspace_for(task_id)
                    _tree = _o.current_tree_sha(
                        (_ws or {}).get("path") if _ws else None)
                except Exception:
                    _tree = ""
                self._evaluate_green_gate_rewind(
                    task, _tree, bool(_tried), refusal, bool(_reminted))
            return None
        # FALSE-GREEN TOOTH (task e0149f1f): the receipt tooth above matches
        # the receipt's tree against the WORKTREE's tree — so when the
        # scratch worktree is stale BOTH agree, on the wrong tree, and the
        # gate closes on evidence that never saw this task's code. Refuse a
        # tree that does not even contain the task's own pinned tests.
        _tree_reason = self._receipt_tree_missing_reason(task, receipt)
        if _tree_reason:
            self._park_green_refusal(task_id, _tree_reason)
            return None
        # ...and the same question about the ADAPTER: 5a6837a0's receipt was an
        # http_probe on a task pinning pytest. A probe returning ok is not
        # evidence for a suite it never ran.
        _adapter_reason = self._receipt_adapter_mismatch_reason(task, receipt)
        if _adapter_reason:
            self._park_green_refusal(task_id, _adapter_reason)
            return None
        _rsn = (
            "machine adjudication: fresh passing EvidenceReceipt "
            f"{getattr(receipt, 'job_id', '')} "
            f"(adapter={getattr(receipt, 'adapter', '')}, "
            f"tree={(getattr(receipt, 'tree_sha', '') or 'n/a')[:12]}) — "
            "the server exercised the oracle and approves on the receipt "
            "tooth; manual-evidence and failed cases stay with a human")
        res = self.gate_decide(task_id, "approve", reason=_rsn,
                               session_id=ADJUDICATOR_SEAT,
                               actor=ADJUDICATOR_SEAT, model="machine")
        return res if res and res.get("ok") else None

    # -- green_gate backward edge (task 8582921d) ------------------------
    def _record_seat_row(self, task_id, action, details):
        """Audit row stamped with the machine seat; older TaskService
        signatures have no `model` kwarg, so fall back without it."""
        try:
            self._task_svc.record_history(task_id, action, details=details,
                                          actor=ADJUDICATOR_SEAT,
                                          model="machine")
        except TypeError:
            self._task_svc.record_history(task_id, action, details=details,
                                          actor=ADJUDICATOR_SEAT)

    def _human_reject_stands(self, task_id) -> bool:
        """AC-5: the newest green_gate decision is a HUMAN reject with no
        forward row after it -> the machine never crosses it."""
        for r in reversed(list(self._task_svc.history(task_id) or [])):
            act = getattr(r, "action", "")
            if act == "advance_task":
                return False
            if act != "gate_decide":
                continue
            if "reject" not in str(getattr(r, "details", "")).lower():
                continue
            actor = str(getattr(r, "actor", "") or "")
            machine = (actor in MACHINE_SEATS
                       or getattr(r, "model", "") == "machine")
            return not machine
        return False

    def _consecutive_auto_rewinds(self, task_id) -> int:
        """Auto rewinds since the last forward/human row. Parking rows and
        machine refusals do not reset the count (or the budget would never
        bind)."""
        n = 0
        for r in reversed(list(self._task_svc.history(task_id) or [])):
            act = getattr(r, "action", "")
            if act == "auto_rewind":
                n += 1
            elif act == "auto_rewind_exhausted" or (
                    act == "gate_decide"
                    and "machine=refused" in str(getattr(r, "details", ""))):
                continue
            else:
                break
        return n

    def _auto_rewind(self, task_id, target_step, reason, evidence_ref):
        """AC-6: move the task back with the seat and evidence on the row;
        the refusal text becomes the next step's work order."""
        self._task_svc.update(task_id, workflow_step=target_step,
                              gate_state="none", gate_reason=reason,
                              blocked_reason="")
        self._record_seat_row(
            task_id, "auto_rewind",
            f"green_gate -> {target_step}; {evidence_ref}; reason={reason}")
        return {"ok": True, "rewound_to": target_step}

    def _evaluate_green_gate_rewind(self, task, tree_sha, tried, refusal,
                                    remint_attempted):
        """Decide the backward edge for a refused green_gate.
        AC-2 red AT this tree -> implement_tasks; AC-3 stale -> verify_green_state;
        AC-4 bounded by MAX_AUTO_REWINDS; AC-5 never crosses a human reject."""
        task_id = getattr(task, "id", "")
        if self._task_svc is None or not refusal:
            return None
        if self._human_reject_stands(task_id):
            return None
        text = refusal.upper()
        if "FAILED" in text and (tried or remint_attempted):
            target = "implement_tasks"
        elif "STALE" in text or "FAILED" in text:
            target = "verify_green_state"
        else:
            return None  # manual / no-receipt cases stay with a human
        n = self._consecutive_auto_rewinds(task_id)
        if n >= MAX_AUTO_REWINDS:
            msg = (f"auto-rewind budget exhausted: {n} consecutive machine "
                   f"rewinds (max {MAX_AUTO_REWINDS}) — parked for a human; "
                   f"latest: {refusal}")
            self._task_svc.update(task_id, gate_state="pending",
                                  gate_reason=msg, blocked_reason=msg)
            rows = list(self._task_svc.history(task_id) or [])
            if not (rows and getattr(rows[-1], "action", "")
                    == "auto_rewind_exhausted"):
                self._record_seat_row(task_id, "auto_rewind_exhausted", msg)
            return None
        return self._auto_rewind(task_id, target, refusal,
                                 f"tree={(tree_sha or '')[:12]}")

    @staticmethod
    def pinned_test_paths(task) -> list:
        """Repo-relative .py test paths named by task.verify. Entries are
        sometimes whole COMMANDS ("python -m pytest a.py b.py"), so paths
        are EXTRACTED, never assumed to be the bare string."""
        import re as _re
        out: list = []
        for v in (getattr(task, "verify", None) or []):
            out += _re.findall(r"(\S+?/tests/\S+?\.py)",
                               str(v).replace("\\", "/"))
        return sorted(set(out))

    def _receipt_tree_missing_reason(self, task, receipt) -> Optional[str]:
        """Refuse a receipt measured on a tree that does not CONTAIN this
        task's pinned tests (task e0149f1f, 2026-07-21). 5a6837a0 closed on
        adapter=http_probe, tree=c162b66 — a commit belonging to a DIFFERENT
        task — because its scratch worktree was never advanced to the lane's
        work, so `git cat-file -e <tree>:<its own test file>` failed. v7.1.24
        taught the gate CARD to distrust a stale receipt; the deciding seat
        never learned it.

        Returns a one-line reason to refuse on, or None when the tree is
        sound. NOTHING pinned -> None: other teeth own that case. A tree we
        cannot RESOLVE also refuses — an unverifiable tree is exactly what
        must not auto-close; a human can still approve."""
        paths = self.pinned_test_paths(task)
        tree = str(getattr(receipt, "tree_sha", "") or "").strip()
        if not paths or not tree:
            return None
        import subprocess
        from prism_service.services import task_workspace
        try:
            ws = task_workspace.workspace_for(
                getattr(task, "id", "") or "") or {}
            cwd = ws.get("path") or self._project_source_path()
            if not cwd:
                return f"cannot resolve a checkout to verify tree {tree[:12]}"
            missing = [
                p for p in paths
                if subprocess.run(["git", "cat-file", "-e", f"{tree}:{p}"],
                                  cwd=cwd, capture_output=True).returncode != 0]
        except Exception as exc:
            return (f"could not verify receipt tree {tree[:12]} "
                    f"({type(exc).__name__})")
        if not missing:
            return None
        return (f"receipt measured at tree {tree[:12]}, which does NOT "
                f"contain this task's pinned test(s) {', '.join(missing)} — "
                "it cannot have exercised this change; a distinct actor "
                "must decide")

    def _park_green_refusal(self, task_id: str, reason: str) -> None:
        """RECORD a machine refusal instead of discarding it (task e0149f1f).

        Both receipt teeth compute a precise, actionable one-liner and used to
        `return None` on it — so the gate parked with an EMPTY gate_reason and
        neither the owner nor the driving agent could tell WHY, which is the
        exact 'pings a human at every gate' failure. Mirrors the ui-artifact
        precedent: NOT a failure (a refused approve must never strand a ticket
        into 'failed'), just pending WITH the reason, plus an audit row.

        Re-sweeps every ~20s, so the history row is written only when the
        reason CHANGES — otherwise the audit trail fills with duplicates.

        Also mirrors the reason onto blocked_reason (task e4e6cd44): the
        actionable half ("land the [task:...] commits") was reaching the owner
        ONLY through gate_reason, which oscillates as the teeth re-sweep, so
        the one line telling them what to DO kept disappearing."""
        if self._task_svc is None:
            return
        try:
            task = self._task_svc.get(task_id)
            prior = str(_task_attr(task, "gate_reason", "") or "")
            self._task_svc.update(task_id, gate_state="pending",
                                  gate_reason=reason, blocked_reason=reason)
            if prior.strip() != reason.strip():
                self._task_svc.record_history(
                    task_id, action="gate_decide",
                    details=(f"gate=green_gate; action=approve; "
                             f"machine=refused; reason={reason}"))
        except Exception:
            return

    def _recorded_ship_approval(self, task_id: str) -> Optional[dict]:
        """The owner's approve, recovered from history — task 5b6aefc1.

        The recorded `ship=queued` gate_decide row IS the queue and the
        receipt: no new table, and the decision survives a ship failure, a
        daemon restart and a retry because it was written before anything
        was attempted. Reads the LATEST such row, exactly the way
        `_failed_gate_is_refused_approve` already recovers a prior decision
        from history.

        Returns {"actor", "reason"} or None. A row whose ship has since
        SUCCEEDED is not cleared — the shipped-ness tooth stops objecting on
        its own, which is what ends the retry loop."""
        if self._task_svc is None:
            return None
        try:
            rows = list(self._task_svc.history(task_id))
        except Exception:
            return None
        for row in reversed(rows):
            if str(_task_attr(row, "action", "")) != "gate_decide":
                continue
            details = str(_task_attr(row, "details", "") or "")
            if "ship=queued" not in details:
                continue
            actor = ""
            reason = ""
            for part in details.split(";"):
                part = part.strip()
                if part.startswith("actor="):
                    actor = part[len("actor="):].strip()
                elif part.startswith("reason="):
                    reason = part[len("reason="):].strip()
            return {"actor": actor or str(_task_attr(row, "actor", "") or ""),
                    "reason": reason}
        return None

    def _ship_on_approve_reason(self, task, action: str, override: bool,
                                actor: object) -> str:
        """Should this approve TRIGGER the ship bot instead of being refused?

        Returns the gate_reason to park with when yes, else "". Three
        conditions, each narrowing deliberately:

        1. the environment opted in (PRISM_SHIP_ON_APPROVE) — OFF by default,
           so nothing changes for anyone who did not ask for it;
        2. the gate's proof burden is a PERSON'S judgment (demo/review). A
           proof_type=test task is machine-graded and the adjudicator seat
           already owns its green — sweeping it into the ship path is the
           task's own pre-declared likely_misfire;
        3. the approver RESOLVES TO A REAL HUMAN. The ship bot exists to
           carry out an owner's decision; a machine seat's approve must not
           be able to trigger a push/merge. Resolution failure fails CLOSED
           (no ship), mirroring same_actor_override_reason.
        """
        if action != "approve" or override:
            return ""
        try:
            from prism_service.services import ship_worker
            if not ship_worker.is_enabled():
                return ""
        except Exception:
            return ""
        pt = str(_task_attr(task, "proof_type", "") or "").strip().lower()
        if pt not in ("demo", "review"):
            return ""
        try:
            from prism_service.models.actor import ActorKind
            ident = _resolve_actor_identity(str(actor or "").strip())
            if getattr(ident, "kind", None) is not ActorKind.HUMAN:
                return ""
        except Exception:
            return ""
        return (
            f"approved by {actor} — shipping: push → PR → CI → merge in "
            "progress (conductor-shipper). Your decision is recorded; the "
            "gate releases as soon as the branch lands on origin/main."
        )

    def _unshipped_gate_reason(self, task) -> str:
        """HOLE 2 (task 8a737f2f): DONE means SHIPPED (mx:
        feedback_done_means_shipped) — refuse to stamp
        full_outcome_complete/status=done while this task's own
        `[task:<id8>]` commit trailer has not reached origin/main.

        Resolved the SAME way the Dashboard's stranded-work panel already
        does (api/tasks.py:_is_shipped_on_main — `git log origin/main
        --grep`), never `merge-base --is-ancestor` (false-negative on
        squash merges, task 499ba9c9) and never the daemon's own checkout
        HEAD — this reads the TASK'S OWN worktree.

        Matches the trailer as a PREFIX (`[task:a205eb7a`, no closing
        bracket required in the grep pattern) — task a205eb7a itself is the
        live regression that forced this: its real driver wrote the FULL
        UUID trailer (`[task:a205eb7a-d46b-4d1c-a2a0-809a0c1e3ff0]`) instead
        of the documented 8-char short form, so the old exact-bracket
        pattern found no local commit at all and silently fail-opened this
        tooth on a task that was genuinely unshipped. `_is_shipped_on_main`
        and `_compute_stranded` (api/tasks.py) carried the identical
        assumption and are fixed the same way.

        FAIL-OPEN (returns ""), never a refusal, when: no workspace is
        resolvable for the task, that workspace has no `origin/main` ref at
        all (e.g. a synthetic/local-only repo with no remote — an
        unverifiable shipped-ness is not this tooth's business; other teeth
        own the un-evidenced case), OR the task never committed anything
        under its own `[task:<id8>]` trailer ANYWHERE in this workspace
        (local history included) — a measurement-only ticket (metric/
        http_probe oracle, zero code changes) has nothing to ship, so
        "unshipped" does not apply to it. Regression guard: this last case
        is what test_conductor_work_honest_green.py's bare-loop walk hit
        (task 8a737f2f qa discovery) before this fail-open existed — that
        fixture's workspace resolves to a real repo+origin/main but the
        task itself never makes a commit, so the naive origin/main-only
        check misread "nothing committed yet" as "committed but unshipped"."""
        task_id = str(getattr(task, "id", "") or "")
        if not task_id:
            return ""
        try:
            from prism_service.services import task_workspace
            ws = task_workspace.workspace_for(task_id) or {}
            repo = str(ws.get("path") or "")
        except Exception:
            repo = ""
        if not repo:
            return ""
        import subprocess
        try:
            # --all: the task's own commit may live on ITS OWN branch while
            # HEAD sits elsewhere (e.g. checked back out to main) — a bare
            # `git log` only walks the current branch and would misread a
            # real unpushed commit as "never committed at all" (test C2/C3).
            local_trailer = subprocess.run(
                ["git", "-C", repo, "log", "--all", "--fixed-strings",
                 "--grep", f"[task:{task_id[:8]}", "-n", "1",
                 "--format=%H"],
                capture_output=True, text=True, timeout=10)
        except Exception:
            return ""
        if local_trailer.returncode != 0 or not local_trailer.stdout.strip():
            return ""
        try:
            ref_check = subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "-q",
                 "origin/main"],
                capture_output=True, text=True, timeout=10)
        except Exception:
            return ""
        if ref_check.returncode != 0:
            return ""
        try:
            from prism_service.api.tasks import _is_shipped_on_main
            shipped = _is_shipped_on_main(repo, task_id)
        except Exception:
            return ""
        if shipped:
            return ""
        return (
            f"green_gate: this task's [task:{task_id[:8]}] commit trailer "
            "is not yet reachable from origin/main — DONE means SHIPPED "
            "(mx: feedback_done_means_shipped); merge/land the branch "
            "before full_outcome_complete/status=done can be set"
        )

    def _receipt_adapter_mismatch_reason(self, task, receipt) -> Optional[str]:
        """Refuse a receipt whose ADAPTER cannot evidence what the task pins
        (task e0149f1f, 2026-07-21). 5a6837a0 closed on adapter=http_probe
        while its task.verify pinned a pytest file — an http probe returning
        ok says NOTHING about that suite.

        Fires ONLY when the task itself pins pytest paths, so a probe- or
        browser-oracle task keeps machine adjudication untouched: the tooth
        narrows the seat, it can never become a blanket refusal (the
        pre-declared likely_misfire). Returns a one-line reason naming BOTH
        what was pinned and what was measured, or None when sound."""
        if not self.pinned_test_paths(task):
            return None
        try:
            from prism_service.services import oracle_spec as osp
            got = str(getattr(receipt, "adapter", "") or "").strip()
            if not got or got == osp.ADAPTER_PYTEST:
                return None
            want = osp.ADAPTER_PYTEST
        except Exception as exc:
            return f"could not verify the receipt adapter ({type(exc).__name__})"
        return (f"receipt was measured by adapter={got}, but this task pins a "
                f"pytest suite (adapter={want}) in task.verify — {got} cannot "
                "evidence those tests; a distinct actor must decide")

    def _failed_gate_is_refused_approve(self, task_id: str,
                                        gate_step_id: str) -> bool:
        """True when the LATEST gate_decide history row for this gate
        records a refused APPROVE attempt (a tooth refusal: control-plane,
        same-actor, oracle-receipt) rather than an explicit reject. A
        reject is a decision the machine seat must never re-litigate; a
        tooth refusal is 'not yet' — re-presentable once the evidence or
        authorization is cured (d09bee0b/b07fd46e, 2026-07-16)."""
        try:
            rows = self._task_svc.history(task_id) or []
        except Exception:
            return False
        for row in reversed(list(rows)):
            s = str(row)
            if "gate_decide" not in s or f"gate={gate_step_id}" not in s:
                continue
            return "action=reject" not in s
        return False

    def adjudicate_demo_red_gate(self, task_id: str) -> Optional[dict]:
        """MACHINE ADJUDICATOR SEAT for a PENDING red_gate on a
        proof_type=demo ticket (task 59ddfcbc). A demo ticket has no test
        suite by design, so the red_with_trace expectation can never be
        met by evidence — the demo rubric in _verify_gate confirms
        instead, and the real proof burden stays with green_gate's
        demo-artifact teeth. Approves as ``conductor-adjudicator``; only
        opted-in environments call this (see gate_adjudicator.is_enabled).
        Test-proofed tickets are untouched. Returns the gate_decide result
        on approve, else None (never flips pending->failed)."""
        if self._task_svc is None:
            return None
        task = self._task_svc.get(task_id)
        if (task is None
                or getattr(task, "workflow_step", "") != "red_gate"
                or getattr(task, "gate_state", "") != "pending"):
            return None
        if getattr(task, "status", "") in ("cancelled", "archived",
                                           "deleted", "done"):
            return None
        pt = str(getattr(task, "proof_type", "") or "").strip().lower()
        if pt != "demo":
            return None
        check = self._verify_gate(task, "red_gate", pt)
        if check.get("verified") is not True:
            return None
        try:
            from prism_service.services import control_plane as _cp
            if _cp.candidate_controls_judge_reason(task):
                return None
        except Exception:
            return None
        res = self.gate_decide(
            task_id, "approve",
            reason=("machine adjudication (demo rubric): "
                    + str(check.get("reason", ""))),
            session_id=ADJUDICATOR_SEAT, actor=ADJUDICATOR_SEAT,
            model="machine")
        return res if res and res.get("ok") else None

    def _park_red_gate(self, task_id: str, reason: str) -> None:
        """Record a COMPUTED red_gate refusal onto the task — gate_state
        stays 'pending' (never 'failed') and gate_reason carries the real,
        actionable reason so a driving agent can self-diagnose instead of
        pinging a human at an empty gate_reason (task ed3263b4)."""
        if self._task_svc is None:
            return
        self._task_svc.update(task_id, gate_state="pending",
                              gate_reason=reason)
        self._task_svc.record_history(
            task_id, action="gate_decide",
            details=f"gate=red_gate; action=park; reason={reason}",
            actor="conductor")

    def mint_red_evidence(self, task_id: str,
                          session_id: Optional[str] = None) -> dict:
        """Mint the RED EvidenceReceipt at the write_failing_tests report
        (task a5e8d877, owner 2026-07-16): record the red-step commit and
        let the trusted runner demonstrate the task's own pinned tests
        FAILING there, so the red_gate that follows can be machine-decided
        on evidence instead of stranding every test-proof drive as
        'evidence not on file'. Best-effort mirror of mint_green_evidence:
        a mint error never blocks the advance — the gate simply stays with
        a human until evidence exists."""
        if self._task_svc is None:
            return {"ok": False, "reason": "no TaskService attached"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "reason": "unknown task"}
        from prism_service.services import oracle_spec as osp
        # Red is resolved from the task's PINNED TESTS, not from proof_type
        # (task a9215794). The proof burden is unchanged: run_red_oracle below
        # must still observe those tests FAILING at the anchor.
        spec = _red_pytest_spec(task)
        if spec is None:
            return {"ok": False,
                    "reason": ("no pinned pytest material to demonstrate red "
                               "with — set task.verify to the failing test "
                               "path(s), or decide this gate manually")}
        # Anchor on the committed red-tests commit, NOT the scratch-worktree
        # HEAD (mx-6decaa): when the failing tests were committed to the
        # working branch, the worktree lags a commit and a HEAD anchor lands
        # pre-tests -> 'no tests ran' -> permanent strand. Fall back to the
        # worktree HEAD only when no tests-only [task:<id>] commit resolves.
        red_sha, red_repo = self._red_tests_commit(task_id)
        # TIER 2 (task ed3263b4): no tests-only commit — try a driver-
        # attested pre-change ref. The SEAT resolves it in the task's own
        # worktree (never trusts the driver's word) and, if it resolves,
        # OVERLAYS the pinned test files onto that pre-change checkout
        # before running them, because the pre-change commit predates the
        # tests by construction (the 19e4e7f7 bundled-commit shape).
        overlay_from = ""
        if not (red_sha and red_repo):
            attested = self._attested_red_ref(task)
            if attested:
                a_sha, a_repo = self._resolve_attested_ref(task_id, attested)
                if a_sha and a_repo:
                    red_sha, red_repo, overlay_from = a_sha, a_repo, a_repo
        if not (red_sha and red_repo):
            red_repo, red_sha = self._workspace_and_head(task_id)
        if not (red_repo and red_sha):
            return {"ok": False,
                    "reason": "no workspace/commit to anchor red to"}
        self._task_svc.record_history(
            task_id, action="red_step_sha", details=red_sha,
            actor=session_id or "conductor")
        ctx = {"project": self._project_name or "default",
               "workspace": red_repo}
        if overlay_from:
            ctx["overlay_from"] = overlay_from
        receipt = osp.run_red_oracle(spec, task, red_sha, ctx=ctx)
        return {"ok": receipt.status == osp.ST_RED,
                "reason": receipt.reason, "red_sha": red_sha}

    def adjudicate_test_red_gate(self, task_id: str) -> Optional[dict]:
        """MACHINE ADJUDICATOR SEAT for a PENDING red_gate on a
        proof_type=test ticket (task a5e8d877, owner directive 2026-07-16:
        test-proof red gates dead-ended as 'evidence not on file' with no
        machine path — the sweep only knew the demo rubric). Approves ONLY
        on a fresh RED EvidenceReceipt: the trusted runner observed the
        task's own pinned tests FAILING at the recorded red-step commit.
        Mints once when unevidenced (backfilling tasks parked before red
        receipts existed); a run that PASSES at the red commit — or an
        unresolvable red-step commit — stays with a human. Never flips
        pending->failed."""
        if self._task_svc is None:
            return None
        task = self._task_svc.get(task_id)
        if (task is None
                or getattr(task, "workflow_step", "") != "red_gate"
                or getattr(task, "gate_state", "") != "pending"):
            return None
        if getattr(task, "status", "") in ("cancelled", "archived",
                                           "deleted", "done"):
            return None
        try:
            from prism_service.services import control_plane as _cp
            if _cp.candidate_controls_judge_reason(task):
                return None
        except Exception:
            return None
        from prism_service.services import oracle_spec as osp
        # Resolve red from the PINNED TESTS, not proof_type (task a9215794):
        # an artifact/demo-proofed ticket can still own a perfectly good
        # tests-only red anchor. No pytest material -> no machine red, and the
        # gate honestly stays with a human.
        spec = _red_pytest_spec(task)
        if spec is None:
            return None
        red_sha = self._red_step_sha(task_id)
        overlay_from = ""
        if not red_sha:
            # TIER 2 / TIER 3 PARK (task ed3263b4): a tooth that computes a
            # refusal and returns None has only half-shipped it — record the
            # reason on the task (pending, never failed) so a driving agent
            # can self-diagnose instead of pinging a human at an empty
            # gate_reason.
            attested = self._attested_red_ref(task)
            if not attested:
                self._park_red_gate(task_id, (
                    "red_gate: no anchor to demonstrate red from — neither "
                    "a tests-only [task:<id8>] commit exists in history "
                    "(commit the failing tests as their OWN commit before "
                    "the fix) nor an attested pre-change ref is on file "
                    "(add a `red-anchor-ref: <sha>` marker line to the "
                    "red-step completion_proof naming a commit before the "
                    "fix landed)."))
                return None
            a_sha, a_repo = self._resolve_attested_ref(task_id, attested)
            if not (a_sha and a_repo):
                self._park_red_gate(task_id, (
                    f"red_gate: the attested pre-change ref "
                    f"{attested[:12]!r} could not be resolved in this "
                    "task's own worktree — verify the sha is correct and "
                    "reachable there, then re-attest."))
                return None
            red_sha, overlay_from = a_sha, a_repo
            self._task_svc.record_history(
                task_id, action="red_step_sha", details=red_sha,
                actor=ADJUDICATOR_SEAT)
        project = self._project_name or "default"
        fresh = osp.fresh_red_receipt(project, task_id, red_sha,
                                      spec.spec_hash())
        if fresh is None:
            tried = any(
                r.tree_sha == red_sha and r.spec_hash == spec.spec_hash()
                for r in osp.read_receipts(project, task_id))
            if tried:
                return None
            _s, red_repo = self._red_tests_commit(task_id)
            ws_path = (red_repo or overlay_from
                      or self._workspace_and_head(task_id)[0])
            if not ws_path:
                return None
            _ctx = {"project": project, "workspace": ws_path}
            if overlay_from:
                _ctx["overlay_from"] = overlay_from
            osp.run_red_oracle(spec, task, red_sha, ctx=_ctx)
            fresh = osp.fresh_red_receipt(project, task_id, red_sha,
                                          spec.spec_hash())
        if fresh is None:
            return None
        res = self.gate_decide(
            task_id, "approve",
            reason=("machine adjudication: RED demonstrated — trusted "
                    "runner observed the pinned tests FAILING at red-step "
                    f"commit {red_sha[:12]} (receipt {fresh.job_id}); a "
                    "passing or unrunnable red stays with a human"),
            session_id=ADJUDICATOR_SEAT, actor=ADJUDICATOR_SEAT,
            model="machine")
        return res if res and res.get("ok") else None

    def adjudicate_rubric_gate(self, task_id: str) -> Optional[dict]:
        """MACHINE ADJUDICATOR SEAT for a PENDING story_gate/plan_gate
        (task a5e8d877 gap 2, strand mx-2812f9): the rubric autoclear runs
        exactly once at gate entry, so a near-miss cured moments later
        (e.g. plan_diagram set right after the report) stranded pending
        forever with no re-sweep. Re-scores the SAME rubric the entry-time
        autoclear uses and approves a green score as the adjudicator seat.
        PENDING only — a rubric-FAILED gate or a human reject stays
        decided; never flips pending->failed."""
        if self._task_svc is None:
            return None
        task = self._task_svc.get(task_id)
        step = getattr(task, "workflow_step", "") if task else ""
        if (task is None or step not in ("story_gate", "plan_gate")
                or getattr(task, "gate_state", "") != "pending"):
            return None
        if getattr(task, "status", "") in ("cancelled", "archived",
                                           "deleted", "done"):
            return None
        from prism_service.models.task import normalize_workflow
        task_workflow = normalize_workflow(getattr(task, "workflow", "") or "")
        validation = self._validation_for_gate(step, task_workflow)
        rule = (self._VERIFIER_RULES.get(validation)
                if validation else None)
        if not (rule and rule.get("rubric")):
            return None
        check = self._verify_rubric_gate(task, validation)
        if check.get("verified") is not True:
            return None
        if step == "plan_gate":
            # FR-4/FR-5 (task c016667f): the re-sweep seat withholds too — the
            # rubric alone no longer clears plan_gate, unconditionally (AC-10:
            # no ui-tag narrowing). Mirrors the entry-time autoclear check in
            # api/conductor_flow.py so every seat consults the same ledger.
            from prism_service.services import design_packet as dp
            project = self._project_name or "default"
            status = dp.approval_status(project, task_id, task)
            if not status.get("approved"):
                _r = str(status.get("reason", "") or "")
                if _r and _r != (getattr(task, "gate_reason", "") or ""):
                    try:
                        self._task_svc.update(task_id, gate_reason=_r)
                    except Exception:
                        pass
                return None
        try:
            from prism_service.services import control_plane as _cp
            if _cp.candidate_controls_judge_reason(task):
                return None
        except Exception:
            return None
        res = self.gate_decide(
            task_id, "approve",
            reason=("machine adjudication (rubric re-sweep): "
                    + str(check.get("reason", ""))),
            session_id=ADJUDICATOR_SEAT, actor=ADJUDICATOR_SEAT,
            model="machine")
        return res if res and res.get("ok") else None

    def _red_tests_commit(self, task_id: str) -> tuple[str, str]:
        """(sha, repo_path) of the newest commit trailered ``[task:<id8>]``
        whose diff touches ONLY test files — the red-tests commit — searched
        in the task's own scratch worktree FIRST, then the shared project
        checkout. The shared checkout is essential when a session commits the
        failing tests to the WORKING BRANCH rather than the per-task scratch
        worktree (mx-6decaa): the scratch worktree then lags by a commit, so a
        HEAD-based anchor lands one commit early (pre-tests) and strands the
        red_gate as 'no tests ran'. Anchoring on the committed tests instead of
        a possibly-stale HEAD is robust to either convention. ('', '') when
        none resolves."""
        import subprocess as _sp
        tag = f"[task:{task_id[:8]}"
        repos: list[str] = []
        ws_path, _head = self._workspace_and_head(task_id)
        if ws_path:
            repos.append(ws_path)
        root = str(self._project_root or "")
        if root and root not in repos:
            repos.append(root)
        for repo in repos:
            try:
                r = _sp.run(["git", "log", "--format=%H%x09%s", "-n", "80"],
                            cwd=repo, capture_output=True, text=True,
                            timeout=15)
                if r.returncode != 0:
                    continue
                for line in r.stdout.splitlines():
                    sha, _, subj = line.partition("\t")
                    if tag not in subj:
                        continue
                    if self._commit_is_tests_only(repo, sha.strip()):
                        return sha.strip(), repo
            except Exception:
                continue
        return "", ""

    def _commit_is_tests_only(self, repo: str, sha: str) -> bool:
        """True when ``sha``'s diff in ``repo`` is non-empty and touches only
        test files — the shape of a red-tests commit. Used both to find the
        anchor and to reject a stale ``red_step_sha`` row that points one
        commit early (at a pre-tests commit whose diff is not tests-only)."""
        if not (repo and sha):
            return False
        import subprocess as _sp
        try:
            f = _sp.run(["git", "diff-tree", "--no-commit-id", "--name-only",
                         "-r", sha], cwd=repo, capture_output=True, text=True,
                        timeout=15)
            files = [p.strip() for p in f.stdout.splitlines() if p.strip()]
            return bool(files) and all("tests/" in p.replace("\\", "/")
                                       for p in files)
        except Exception:
            return False

    def _commit_is_reachable(self, repo: str, sha: str) -> bool:
        """True when ``sha`` is an ancestor of ``repo``'s current HEAD — i.e.
        still part of that checkout's real history, not a dangling object a
        rewrite (soft-reset + recommit, correcting an earlier red-tests
        commit) has moved the branch past. A superseded commit stays
        resolvable in the shared object store — ``git diff-tree`` on it still
        succeeds, so it still LOOKS tests-only by content — which otherwise
        shadows the real, current anchor forever (task cb1dc6f4: a lane
        corrected its red-tests commit after an initial rc=2 collection-error
        refusal; the OLD commit kept winning in ``_red_step_sha`` below, and
        the adjudicator's ``tried`` cache then permanently refused to
        re-attempt at that stale, un-heal-able anchor)."""
        if not (repo and sha):
            return False
        import subprocess as _sp
        try:
            r = _sp.run(["git", "merge-base", "--is-ancestor", sha, "HEAD"],
                        cwd=repo, capture_output=True, timeout=15)
            return r.returncode == 0
        except Exception:
            return False

    _RED_ANCHOR_REF_RE = re.compile(r"red-anchor-ref:\s*([0-9a-fA-F]{6,40})")

    def _attested_red_ref(self, task: object) -> str:
        """The pre-change ref a driver ATTESTS in the red-step
        completion_proof via a ``red-anchor-ref: <sha>`` marker line (task
        ed3263b4, tier 2): when tests+fix land in ONE commit (the 19e4e7f7
        shape), tier 1 (``_red_tests_commit``) finds no tests-only commit.
        Parsing the marker is NOT trusting the driver — ``_resolve_attested_
        ref`` below re-derives the sha in the task's own worktree, and
        ``mint_red_evidence`` checks it out and runs the pinned tests there
        itself (distinct-actor rule: a pasted transcript is never evidence).
        '' when no marker line is present."""
        proof = str(_task_attr(task, "completion_proof", "") or "")
        m = self._RED_ANCHOR_REF_RE.search(proof)
        return m.group(1) if m else ""

    def _resolve_attested_ref(self, task_id: str,
                              ref: str) -> tuple[str, str]:
        """Resolve an attested ref against the TASK'S OWN worktree — the
        seat rev-parses it itself, never the driver's word. Returns
        (sha, repo) on success, ('', '') when ``ref`` does not resolve
        there (task ed3263b4)."""
        ref = (ref or "").strip()
        if not ref:
            return "", ""
        ws_path, _head = self._workspace_and_head(task_id)
        if not ws_path:
            return "", ""
        import subprocess as _sp
        try:
            r = _sp.run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
                        cwd=ws_path, capture_output=True, text=True,
                        timeout=15)
        except Exception:
            return "", ""
        if r.returncode != 0:
            return "", ""
        sha = r.stdout.strip()
        return (sha, ws_path) if sha else ("", "")

    def _red_step_sha(self, task_id: str) -> str:
        """The commit red is anchored to. Prefer a recorded ``red_step_sha``
        history row (stamped by mint_red_evidence at the write_failing_tests
        report) — but ONLY when that commit genuinely carries the pinned tests
        AND is still reachable from the checkout's current HEAD. A row
        stamped from a lagging scratch worktree points one commit early
        (mx-6decaa), and a row whose commit was later superseded by a
        soft-reset + recommit (task cb1dc6f4) points at a dangling object;
        either must NOT shadow the real red-tests commit, so both fall
        through to ``_red_tests_commit``, which self-heals the anchor.
        '' when neither resolves (the seat then leaves the gate with a human)."""
        import re as _re
        try:
            rows = self._task_svc.history(task_id) or []
        except Exception:
            rows = []
        recorded = ""
        for row in reversed(list(rows)):
            if "red_step_sha" not in str(row):
                continue
            m = _re.search(r"\b[0-9a-f]{40}\b", str(row))
            if m:
                recorded = m.group(0)
                break
        tests_sha, repo = self._red_tests_commit(task_id)
        if recorded and recorded != tests_sha:
            # Keep the recorded row only if it truly is a tests-only commit
            # AND still reachable from that checkout's HEAD; a lagging-
            # worktree mis-stamp (points pre-tests) or a superseded
            # (rewritten-past) commit both yield to the real red-tests
            # commit resolved above.
            ws_path, _h = self._workspace_and_head(task_id)
            for r in (repo, ws_path, str(self._project_root or "")):
                if (r and self._commit_is_tests_only(r, recorded)
                        and self._commit_is_reachable(r, recorded)):
                    return recorded
            if tests_sha:
                return tests_sha
        return recorded or tests_sha

    def _workspace_and_head(self, task_id: str) -> tuple[str, str]:
        """(workspace_path, HEAD sha) for a task — its own scratch workspace
        when one exists, else the shared project checkout. ('', '') when
        neither resolves; callers refuse rather than fall back to cwd."""
        from prism_service.services import oracle_spec as osp
        from prism_service.services import task_workspace
        ws = None
        try:
            ws = task_workspace.workspace_for(task_id)
        except Exception:
            ws = None
        ws_path = (ws or {}).get("path") or ""
        if not ws_path and self._project_root:
            ws_path = str(self._project_root)
        if not ws_path:
            return "", ""
        return ws_path, osp.current_tree_sha(ws_path)

    def rewind_task(self, task_id: str, reason: str = "",
                    actor: str = "owner") -> dict:
        """AUDITED one-step rewind for an overshot task (task b07fd46e).

        The repair lever for a double-advance (mx-f8ed3f): moves the task
        back EXACTLY one step in WORKFLOW_STEPS, resets the gate state for
        the target step ('pending' on a gate, 'none' otherwise), clears
        gate_reason, and records a task_history row carrying the mandatory
        reason + actor. Refuses a blank reason and refuses to rewind off
        the first step — a guarded lever, never a silent hand-drive."""
        if self._task_svc is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "no TaskService attached"}
        if not (reason and reason.strip()):
            return {"ok": False, "task_id": task_id,
                    "reason": "rewind requires a reason (audited lever)"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "task_id": task_id, "reason": "unknown task"}
        # The task's OWN sequence (task 6f22d0ad): a triage task rewinds
        # along intake->classify->decide->done, the default along
        # WORKFLOW_STEPS -- same per-task lookup advance_task uses.
        from prism_service.models.task import normalize_workflow
        steps = self._workflow_steps(normalize_workflow(getattr(task, "workflow", "")))
        ids = [s["id"] for s in steps]
        cur = getattr(task, "workflow_step", "") or ""
        if cur not in ids:
            return {"ok": False, "task_id": task_id,
                    "reason": f"task is not on a workflow step ({cur!r})"}
        i = ids.index(cur)
        if i == 0:
            return {"ok": False, "task_id": task_id,
                    "reason": "already on the first workflow step; "
                              "cannot rewind further"}
        target = steps[i - 1]
        self._task_svc.update(
            task_id, workflow_step=target["id"],
            gate_state="pending" if target.get("type") == "gate" else "none",
            gate_reason="")
        self._task_svc.record_history(
            task_id, action="rewind_task",
            details=f"{cur} -> {target['id']}; reason={reason.strip()}",
            actor=actor or "owner")
        return {"ok": True, "task_id": task_id, "from_step": cur,
                "to_step": target["id"]}

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
                # premise_grounded (task 3928b7ac, issue #222 continued):
                # review_previous_notes' report reads task.premise_notes, a
                # DEDICATED field — not completion_proof (task 3a63190b's
                # original opt-in scoping read the shared completion_proof,
                # which several fixtures also stage with unrelated
                # green-proof content; the dedicated field removes that
                # collision so the check below can be unconditional).
                "notes_md": getattr(task, "premise_notes", "") or "",
            }
            if validation == "story_complete":
                res = gov.score_story_complete(evidence, rubric)
            elif validation == "premise_grounded":
                res = gov.score_premise_grounded(evidence, rubric)
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
        from prism_service.models.task import normalize_workflow
        task_workflow = normalize_workflow(getattr(task, "workflow", "") or "")
        validation = self._validation_for_gate(gate_step_id, task_workflow)
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
        # DEMO-PROOF RED GATE (task 59ddfcbc): a demo ticket has no test
        # suite BY DESIGN, so tier0's red_with_trace expectation can never
        # be met and every demo ticket parked at red_gate forever. The
        # honest red state for a demo is the ABSENT artifact; the proof
        # burden lives at green_gate (ui-artifact + oracle-receipt teeth).
        # Confirm on that basis instead of consulting the test-shaped
        # verifier. green_full for demo is NOT relaxed here.
        if pt == "demo" and gate_step_id == "red_gate":
            return {
                "verified": True,
                "reason": ("demo-proof ticket: no test suite by design — "
                           "red state is the absent artifact; proof burden "
                           "carried by green_gate's demo-artifact teeth"),
                "verifier": None,
                "validation": validation,
            }
        # TEST-PROOF RED GATE (task a5e8d877): when the trusted runner has
        # DEMONSTRATED red — this task's own pinned tests observed FAILING
        # at the recorded red-step commit, receipt on file — that receipt
        # IS the red_with_trace artifact, and it is stronger evidence than
        # the tier0 diff heuristic (which reads the CURRENT workspace,
        # where a committed implementation already turns the tests green
        # and made every honest red drive override-only). Unevidenced red
        # gates fall through to the tier0 consult unchanged.
        if pt == "test" and gate_step_id == "red_gate":
            try:
                from prism_service.services import oracle_spec as osp
                spec = osp.OracleSpec.from_task(task)
                tid = getattr(task, "id", "")
                proj = self._project_name or "default"
                red_sha = self._red_step_sha(tid)
                fresh = (osp.fresh_red_receipt(proj, tid, red_sha,
                                               spec.spec_hash())
                         if red_sha else None)
                if fresh is None and red_sha:
                    # Mint the red demonstration ON DEMAND (mx-6decaa): a
                    # test-proof red_gate must not dead-end on 'no receipt
                    # yet'. Demonstrate the pinned tests FAILING at the
                    # anchored red-tests commit so this approve resolves on
                    # merit. Guarded like the adjudicator: never re-run a
                    # spec_hash already attempted at this sha (no mint loop).
                    already = any(
                        r.tree_sha == red_sha
                        and r.spec_hash == spec.spec_hash()
                        for r in osp.read_receipts(proj, tid))
                    if not already:
                        _s, red_repo = self._red_tests_commit(tid)
                        ws_path = red_repo or self._workspace_and_head(tid)[0]
                        if ws_path:
                            osp.run_red_oracle(
                                spec, task, red_sha,
                                ctx={"project": proj, "workspace": ws_path})
                            fresh = osp.fresh_red_receipt(
                                proj, tid, red_sha, spec.spec_hash())
                if fresh is not None:
                    return {
                        "verified": True,
                        "reason": ("red demonstrated by trusted runner at "
                                   f"{red_sha[:12]}: {fresh.reason}"),
                        "verifier": None,
                        "validation": validation,
                    }
            except Exception:
                pass
        if pt and pt not in ("test", "demo"):
            return {
                "verified": True,
                "reason": (f"proof_type={pt!r}: tier0 test-shaped consult "
                           "skipped; judged on the proof_type artifact shape"),
                "verifier": None,
                "validation": validation,
            }
        # HUMAN-JUDGMENT GREEN GATE (owner 2026-07-19): a demo/visual/manual
        # oracle has NO test suite — the person's Approve IS the sign-off. The
        # shell verifier finds no claims (no diff in scope) and refuses, which
        # forced an override on every visual gate (68e5c699). Skip the
        # test-shaped verifier consult here; the proof burden is the human
        # decision (distinct-actor) + the ui-artifact/oracle-receipt teeth.
        if gate_step_id == "green_gate":
            _hj = pt in ("demo", "review")
            if not _hj:
                try:
                    from prism_service.services import oracle_spec as _osp
                    _hj = _osp.is_human_judgment(_osp.OracleSpec.from_task(task))
                except Exception:
                    _hj = False
            if _hj:
                return {
                    "verified": True,
                    "reason": ("human-judgment gate: visual/demo sign-off "
                               "carried by the person's decision — test-shaped "
                               "verifier skipped (no suite by design)"),
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

    def deliver_task(self, task_id: str, actor: str = "conductor") -> dict:
        """Finish the pipeline (task cb1dc6f4): land a task's own committed
        branch on local main. Approving green_gate marked a task verified
        and stopped there — the task read DONE on the board while its code
        sat on a branch forever (measured 2026-07-29: 19 of 137
        prism/ws/* branches carried commits that never reached main).

        The guardrails ARE the feature, not an afterthought:
          * RE-VERIFIES the task's pinned tests at the CURRENT tip of its
            own branch — never trusts a stale EvidenceReceipt. A receipt
            minted at an older tree must not become a blank cheque for a
            later commit (this session's own e58a6df + follow-up 767aa63
            is exactly that shape).
          * On red tests, REFUSES and leaves main untouched.
          * Computes the merge HEADLESSLY (`git merge-tree --write-tree`) —
            this never touches any working tree/index, so a conflict PARKS
            with an actionable reason and main is provably byte-identical
            to before the attempt.
          * Never forces, resets, or rewrites main; landing is a
            fast-forward-only `update-ref` compare-and-swap against the
            main sha this call itself resolved, so a main that moved
            concurrently is refused rather than overwritten.
          * Confirms success from GIT FACTS (the commits are ancestors of
            main afterwards), never from a command's exit code alone.

        Returns {"ok", "state" ("delivered"|"already_delivered"|"refused"|
        "parked"|"error"), "reason", "main_sha", "commits"}. Never raises —
        a delivery failure is a recorded, actionable outcome, not an
        exception the gate-decide caller must handle."""
        import subprocess

        from prism_service.services import task_workspace

        def _res(state: str, reason_: str, **extra: Any) -> dict:
            out: dict = {"ok": state in ("delivered", "already_delivered"),
                        "state": state, "reason": reason_, "task_id": task_id}
            out.update(extra)
            return out

        def _git(cwd: str, *args: str) -> tuple[int, str, str]:
            try:
                r = subprocess.run(["git", *args], cwd=str(cwd),
                                   capture_output=True, text=True, timeout=60)
                return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                return 1, "", str(exc)

        def _merge_tree(cwd: str, ours: str, theirs: str) -> tuple[int, str, str]:
            """Compute a merge tree without touching a checkout or its index.

            Git 2.38 added ``merge-tree --write-tree``. Ubuntu 22.04 still
            ships 2.34, so use a private temporary index there while keeping
            the same headless, concurrency-safe contract.
            """
            rc, tree, err = _git(cwd, "merge-tree", "--write-tree", ours, theirs)
            if rc == 0:
                return rc, tree, err
            if "unknown rev --write-tree" not in (tree + err) and "unknown option" not in (tree + err):
                return rc, tree, err

            import tempfile
            rc, base, base_err = _git(cwd, "merge-base", ours, theirs)
            if rc != 0 or not base:
                return 1, "", base_err or "could not resolve merge base"
            index_path = tempfile.mktemp(prefix="prism-merge-index-")
            env = os.environ.copy()
            env["GIT_INDEX_FILE"] = index_path
            try:
                read = subprocess.run(
                    ["git", "read-tree", "-m", base, ours, theirs], cwd=cwd,
                    env=env, capture_output=True, text=True, timeout=60,
                )
                if read.returncode != 0:
                    return read.returncode, read.stdout.strip(), read.stderr.strip()
                unresolved = subprocess.run(
                    ["git", "ls-files", "-u"], cwd=cwd, env=env,
                    capture_output=True, text=True, timeout=60,
                )
                if unresolved.stdout.strip():
                    return 1, unresolved.stdout.strip(), "merge conflict"
                written = subprocess.run(
                    ["git", "write-tree"], cwd=cwd, env=env,
                    capture_output=True, text=True, timeout=60,
                )
                return written.returncode, written.stdout.strip(), written.stderr.strip()
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
                return 1, "", str(exc)
            finally:
                try:
                    os.unlink(index_path)
                except FileNotFoundError:
                    pass

        if self._task_svc is None:
            return _res("error", "no TaskService attached")
        task = self._task_svc.get(task_id)
        if task is None:
            return _res("error", "unknown task")
        try:
            ws = task_workspace.workspace_for(task_id)
        except Exception as exc:
            return _res("error", f"could not resolve task workspace ({exc})")
        if not ws or not ws.get("path") or not ws.get("repo_root"):
            return _res("error", "no task workspace to deliver from — the "
                        "task never entered the conductor's per-task worktree")
        branch = str(ws.get("branch") or "")
        repo_root = str(ws.get("repo_root"))
        ws_path = str(ws.get("path"))
        if not branch:
            return _res("error", "workspace has no recorded branch")

        # 1) RE-VERIFY at the tree actually being merged — the workspace's
        # CURRENT HEAD, not whatever tree an earlier receipt was minted at.
        rc, tip_sha, err = _git(ws_path, "rev-parse", "HEAD")
        if rc != 0 or not tip_sha:
            return _res("error", f"could not resolve workspace HEAD ({err})")
        paths = self.pinned_test_paths(task)
        if paths:
            try:
                pr = subprocess.run(
                    [sys.executable, "-m", "pytest", *paths, "-q",
                     "-p", "no:cacheprovider"],
                    cwd=ws_path, capture_output=True, text=True, timeout=600)
            except subprocess.TimeoutExpired:
                return _res("refused",
                           f"pinned tests timed out at tree {tip_sha[:12]} — "
                           "refusing to merge; main is unchanged")
            if pr.returncode != 0:
                tail = (pr.stdout or pr.stderr or "").strip().splitlines()
                tail = tail[-1] if tail else ""
                return _res("refused",
                           f"pinned tests are RED at tree {tip_sha[:12]} "
                           f"(rc={pr.returncode}: {tail[:200]}) — refusing "
                           "to merge a red tree; main is unchanged",
                           verified_tree=tip_sha)

        # 2) Resolve main and short-circuit if this work already landed.
        rc, before_main, err = _git(repo_root, "rev-parse", "refs/heads/main")
        if rc != 0 or not before_main:
            return _res("error", f"could not resolve refs/heads/main ({err})")
        rc, _, _ = _git(repo_root, "merge-base", "--is-ancestor",
                        tip_sha, before_main)
        if rc == 0:
            return _res("already_delivered",
                        f"{branch} @ {tip_sha[:12]} is already an ancestor "
                        "of main", main_sha=before_main)

        # 3) Compute the merge HEADLESSLY. `git merge-tree --write-tree`
        # never touches a working tree or index — a conflict here has made
        # NO change to the repo at all, which is how a park leaves main
        # provably byte-identical (likely_misfire: resolving a conflict by
        # force/reset/rewriting main is exactly what must never happen).
        rc, mt_out, mt_err = _merge_tree(repo_root, before_main, tip_sha)
        if rc != 0:
            detail = (mt_out or mt_err or "").strip().splitlines()
            detail = detail[0] if detail else "conflict"
            return _res("parked",
                        f"{branch} @ {tip_sha[:12]} conflicts with main — "
                        f"{detail[:300]}; main is unchanged",
                        main_sha=before_main, verified_tree=tip_sha)
        merged_tree = (mt_out.strip().splitlines() or [""])[0].strip()
        if not merged_tree:
            return _res("parked",
                        "merge-tree produced no result; main is unchanged",
                        main_sha=before_main)

        # 4) Audit: the commits this delivery is about to carry.
        rc, log_out, _ = _git(repo_root, "log", "--format=%H%x09%s",
                              f"{before_main}..{tip_sha}")
        commits: list[dict] = []
        if rc == 0:
            for line in log_out.splitlines():
                sha, _, subject = line.partition("\t")
                if sha.strip():
                    commits.append({"sha": sha.strip(),
                                    "subject": subject.strip()})

        # 5) Materialize the merge commit (still no working tree touched)
        # and land it with a fast-forward-only, compare-and-swap ref update
        # — if main moved since step 2 this refuses instead of overwriting
        # a concurrent advance.
        msg = (f"Merge {branch} into main [task:{task_id[:8]}]\n\n"
              f"delivered by {actor}; re-verified green at {tip_sha[:12]}; "
              f"{len(commits)} commit(s)")
        rc, new_main, err = _git(repo_root, "commit-tree", merged_tree,
                                 "-p", before_main, "-p", tip_sha, "-m", msg)
        if rc != 0 or not new_main:
            return _res("error", f"could not create the merge commit ({err})",
                        main_sha=before_main)
        rc, _, err = _git(repo_root, "update-ref", "-m",
                          f"delivery: {msg.splitlines()[0]}",
                          "refs/heads/main", new_main, before_main)
        if rc != 0:
            return _res("parked",
                        f"main moved during delivery — refusing to "
                        f"overwrite it ({err}); main is unchanged",
                        main_sha=before_main)

        # 6) Confirm from GIT FACTS, never the update-ref exit code alone.
        rc, _, _ = _git(repo_root, "merge-base", "--is-ancestor",
                        tip_sha, "refs/heads/main")
        if rc != 0:
            return _res("error",
                        "update-ref reported success but the commits are "
                        "not ancestors of main — treating as undelivered",
                        main_sha=before_main)

        reason = (f"delivered {branch} ({len(commits)} commit(s)) to main "
                 f"as {new_main[:12]}; re-verified green at {tip_sha[:12]}")
        try:
            self._task_svc.record_history(
                task_id, action="delivery",
                details=(f"merged {branch} -> main @ {new_main[:12]}; "
                        f"verified_tree={tip_sha[:12]}; approver={actor}; "
                        "commits=" + ",".join(c["sha"][:12] for c in commits)),
                actor=actor)
        except Exception:
            pass
        return _res("delivered", reason, main_sha=new_main, commits=commits,
                    verified_tree=tip_sha)

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
            # Machine JUDGE seats are excluded — rows stamped before the
            # MACHINE_SEATS exemption existed must not poison the set.
            prior_work_actors = [
                s.get("session_id")
                for s in self._task_svc.sessions_for_task(task_id)
                if s.get("session_id") not in MACHINE_SEATS]
        except Exception:
            prior_work_actors = []

        # Conductor-path auto-writer: stamp/refresh the task_sessions row
        # from the carried task_id + session on every gate decision.
        self._stamp_session(task_id, session_id)

        from prism_service.models.task import normalize_workflow
        task_workflow = normalize_workflow(getattr(task, "workflow", "") or "")
        current_step = self._step_by_id(task.workflow_step, task_workflow)
        if current_step is None or current_step["type"] != "gate":
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": task.workflow_step,
                "gate_state": task.gate_state,
                "reason": "task is not currently on a gate step",
            }
        # Conductor v2 follow-up (#79) + rewind verb (2026-07-13 ease-up):
        # a failed gate recovers two ways —
        #   (a) EVIDENCE-DRIVEN: action='approve' WITHOUT override re-runs the
        #       gate's own machine check against the task's CURRENT evidence;
        #       a verified=True recheck falls through to the normal approve
        #       path (released on merit, no override signature). This is the
        #       fix-the-evidence-then-re-present flow that used to be
        #       impossible (override was the ONLY exit from 'failed').
        #   (b) MANUAL: override=True supersedes the earlier ruling; audit row
        #       tags actor='manual-override' as before.
        # 'reject' on a failed gate is still pointless (already failed).
        if task.gate_state == "failed":
            if action == "approve" and not override:
                # ORACLE-RECEIPT PRECEDENCE on recovery, checked FIRST
                # (68e5c699): a fresh passing EvidenceReceipt clears the
                # recheck in seconds without invoking the env-fragile /
                # hanging shell verifier.
                recheck = None
                if task.workflow_step == "green_gate":
                    _rr, _rc = self._oracle_receipt_refusal(
                        task, override=False, reason=reason or "")
                    if not _rr and _rc is not None:
                        recheck = {"verified": True}
                if recheck is None:
                    recheck = self._verify_gate(
                        task, task.workflow_step,
                        getattr(task, "proof_type", None))
                if recheck.get("verified") is not True:
                    return {
                        "ok": False,
                        "task_id": task_id,
                        "gate_step": task.workflow_step,
                        "gate_state": task.gate_state,
                        "reason": (
                            "gate_state is 'failed'; the machine recheck did "
                            "not pass on current evidence ("
                            + str(recheck.get("reason", "no verdict"))
                            + ") — fix the evidence, or recover manually "
                            "with override=True"
                        ),
                        "verifier": recheck.get("verifier"),
                    }
                # verified recheck: fall through to the normal approve path.
            elif not (action == "approve" and override):
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": task.workflow_step,
                    "gate_state": task.gate_state,
                    "reason": (
                        "gate_state is 'failed'; recovery requires a passing "
                        "machine recheck via action='approve' (no override), "
                        "or action='approve' with override=True"
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

        # ACTOR PASSTHROUGH (task 1bc13307): persist the REAL deciding actor
        # — the caller-supplied `actor`, falling back to `session_id`, and
        # only a literal as the last resort when neither is given. Every
        # branch below that used to shadow `actor` with a hardcoded literal
        # now derives it from this same helper so a machine seat
        # (ADJUDICATOR_SEAT) or a real human identity is never discarded.
        def _decided_by(fallback: str) -> str:
            return actor or session_id or fallback

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
                actor=_decided_by("conductor"),
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
        # UNCONDITIONAL, byte-for-byte unchanged (task 69233ca0 follow-up split
        # this OFF the combined judge check — see control_plane.
        # candidate_policy_edit_reason vs candidate_controls_judge_reason —
        # so the DIFFERENT, non-blocking dirty-judge caveat right below cannot
        # soften this real security boundary by so much as a character).
        try:
            from prism_service.services import control_plane as _cp
            _cj_reason = _cp.candidate_policy_edit_reason(task)
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
        # DIRTY JUDGE CAVEAT (task 69233ca0 follow-up): the DAEMON'S OWN
        # checkout — never this task's worktree — carrying an uncommitted edit
        # to a gate-policy file means the running judge cannot prove it is
        # executing the pinned code. Unlike the refusal above, this is NOT
        # about this task being disqualified: the MACHINE seats
        # (adjudicate_green_gate et al.) already pre-flight the COMBINED check
        # (control_plane.candidate_controls_judge_reason) and abstain — leave
        # the gate pending — on this exact same signal. A HUMAN approving here
        # has SEEN the caveat (surfaced at GET /gate/readiness before they
        # click); that sight is the escape hatch the doctrine preserves (owner
        # 2026-07-15/16: the human always keeps visibility + override, never
        # a required click). So: do NOT fail — record the caveat on the audit
        # history now, and fold it into `reason` so it survives into whatever
        # gate_reason gets persisted below (this approve, or a later refusal
        # from an unrelated tooth further down).
        try:
            from prism_service.services import control_plane as _cp
            _dirty_reason = _cp.dirty_judge_reason()
        except Exception:
            _dirty_reason = ""
        if _dirty_reason:
            self._task_svc.record_history(
                task_id, action="gate_decide",
                details=(f"gate={gate_step_id}; action=approve; "
                         f"dirty-judge-caveat={_dirty_reason}"),
                actor="conductor")
            reason = (reason + "  " if reason else "") + \
                f"[decided while the judge was dirty: {_dirty_reason}]"
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
            # 'Implement to evidence': a captured screenshot in the task's
            # evidence gallery satisfies the demonstrable-UI requirement, even
            # if completion_proof doesn't hand-cite the path.
            if ui_reason and has_captured_evidence(
                    task_id, self._project_name or "default"):
                ui_reason = ""
            if ui_reason:
                # NOT a failure — the approve just can't go through YET (needs a
                # UI artifact). Keep the gate PENDING with the actionable reason
                # so the owner attaches the screenshot and re-approves WITHOUT a
                # rewind/override (owner 2026-07-19: a refused click must never
                # strand a ticket into 'failed').
                self._task_svc.update(
                    task_id, gate_state="pending", gate_reason=ui_reason,
                )
                self._task_svc.record_history(
                    task_id,
                    action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"ui-artifact=not-yet; reason={ui_reason}"),
                    actor="conductor",
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "pending",
                    "reason": ui_reason,
                }
        # SHIPPED-NESS PRE-FLIGHT (HOLE 2, task 8a737f2f): DONE means
        # SHIPPED (mx: feedback_done_means_shipped) — refuse to stamp
        # full_outcome_complete/status=done while this task's own
        # [task:<id8>] commit trailer has not reached origin/main.
        # _unshipped_gate_reason (below) fails OPEN (no objection) when no
        # workspace/origin-main is resolvable, so this only ever fires when
        # the shipped-ness question is actually answerable. PARKS pending
        # with the reason, never failed (task e0149f1f precedent) — an
        # honest owner Approve/override remains available once it ships.
        if gate_step_id == "green_gate":
            _ship_reason = self._unshipped_gate_reason(task)
            if _ship_reason:
                # APPROVE SHIPS IT (task 5b6aefc1, owner 2026-08-18). When
                # this environment opted in and a real person is approving a
                # demo/review gate, the click is no longer refused — it is
                # the TRIGGER. RECORD THE DECISION FIRST, before anything is
                # attempted: "shipping before recording the human decision (a
                # ship failure then eats the approval)" is this task's own
                # pre-declared likely_misfire, and the row written here is
                # what the ship seat later recovers and replays under this
                # same actor. The gate still parks pending — the pipeline can
                # take minutes and must never block the owner's request.
                _queue_reason = self._ship_on_approve_reason(
                    task, action, override, actor)
                if _queue_reason:
                    self._task_svc.record_history(
                        task_id, action="gate_decide",
                        details=(f"gate={gate_step_id}; action=approve; "
                                 f"human=recorded; ship=queued; "
                                 f"actor={actor}; reason={reason}"),
                        actor=str(actor or ""),
                    )
                    self._task_svc.update(task_id, gate_state="pending",
                                          gate_reason=_queue_reason,
                                          blocked_reason="")
                    self._record_agent_run(
                        task_id, gate_step_id, session_id, model=model,
                        gate_state="pending", ok=True,
                        verdict_summary=("ship-on-approve: decision recorded, "
                                         "branch queued for landing")[:200],
                    )
                    return {
                        "ok": False,
                        "task_id": task_id,
                        "gate_step": gate_step_id,
                        "gate_state": "pending",
                        "reason": _queue_reason,
                        "ship_queued": True,
                    }
                self._park_green_refusal(task_id, _ship_reason)
                self._task_svc.record_history(
                    task_id, action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"shipped-ness=not-yet; reason={_ship_reason}"),
                    actor="conductor",
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "pending",
                    "reason": _ship_reason,
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
            _kids = list(self._task_svc.list(parent_id=task_id))
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
        # HUMAN-JUDGMENT gate (owner 2026-07-19): a demo/visual/manual oracle has
        # NO machine tooth to satisfy — the person's approve IS the evidence, so
        # a plain Approve by a DISTINCT actor signs off (no override ceremony, no
        # failed-browser-receipt block). The distinct-actor + misfire teeth below
        # still apply. Objective oracles (pytest/http) keep the fresh-receipt
        # requirement. Pairs with the adjudicator guard (task eaafdf75).
        _human_judgment = False
        if gate_step_id == "green_gate" and _has_oracle:
            try:
                from prism_service.services import oracle_spec as _osp
                _pt = str(getattr(_live, "proof_type", "") or "").strip().lower()
                # A screen-claim shape (task 8a737f2f, HOLE 1) is human
                # judgment too, even when OracleSpec derives an objective
                # http_probe from the URL in the oracle text — the derived
                # adapter proves reachability, not what a person SEES on
                # that surface, so a plain distinct-actor Approve (not the
                # machine seat, which abstains separately in
                # adjudicate_green_gate) is still the correct sign-off.
                _human_judgment = _pt in ("demo", "review") or \
                    _osp.is_human_judgment(_osp.OracleSpec.from_task(_live)) or \
                    bool(_screen_claim_gate_reason(
                        getattr(_live, "tags", None), _pt,
                        getattr(_live, "oracle", "")))
            except Exception:
                _human_judgment = False
        # ROLLUP-NEVER-DECIDES-A-HUMAN-GATE (task 457b38db, mx-7e03ff):
        # child completeness alone is never machine evidence for a
        # green_gate whose proof burden is a person's own judgment of the
        # ASSEMBLED feature — three live incidents (64ba4755 x2, 88a7da0b)
        # closed a demo-proof epic the instant its children finished, with
        # no owner click, via the `elif rollup_ok:` consume site below
        # stamping "epic-rollup=pass". A plain (non-override) approve on a
        # demo/review (human-judgment) epic must PARK pending instead —
        # never failed, so an honest owner Approve/override stays available
        # — naming the unexercised oracle, with the roll-up verdict
        # demoted to informational context. An audited override=True
        # approve is a deliberate human act and is unaffected (handled by
        # the `if override:` branch below, evaluated before this).
        # Two narrowings (task dbfe3727, live incident 2026-08-17 on epic
        # 37c9207b, where this tooth refused the OWNER'S OWN CLICK with
        # advice a plain approve could never satisfy):
        # (1) an approve whose actor RESOLVES TO A REAL HUMAN (the
        #     ActorService join on the signed-in user, the identity the
        #     /api/conductor/gate route passes through since 98d38111) IS
        #     the owner's own judgment of the oracle - parking it made
        #     "Approve as the reviewing owner" unsatisfiable. Resolution
        #     failure fails CLOSED (parks), mirroring
        #     same_actor_override_reason.
        # (2) a proof_type=test epic is machine-graded (rule eaafdf75):
        #     its rollup is the legitimate machine path (epic 37c9207b
        #     suite AC-3), so only demo/review/screen-claim shapes park -
        #     an unset proof_type still parks (conservative default).
        _pt_low = str(getattr(_live, "proof_type", "") or "").strip().lower()
        _rollup_screen_claim = bool(_screen_claim_gate_reason(
            getattr(_live, "tags", None), _pt_low,
            getattr(_live, "oracle", "")))
        _rollup_human_gate = _rollup_screen_claim or (
            _human_judgment and _pt_low != "test")
        _approver_human = False
        try:
            _aid = _resolve_actor_identity(
                str(actor or session_id or "").strip())
            _approver_human = getattr(_aid, "kind", "") == "human"
        except Exception:
            _approver_human = False
        if (gate_step_id == "green_gate" and not override
                and rollup_has_children and rollup_ok
                and _rollup_human_gate and not _approver_human):
            _pt_txt = (str(getattr(_live, "proof_type", "") or "").strip()
                       or "human-judgment")
            _oracle_txt = str(getattr(_live, "oracle", "") or "").strip()
            _park_reason = (
                f"green_gate: {_pt_txt}-proof epic — child roll-up "
                f"({rollup_reason}) is informational only, not sufficient "
                f"evidence for this gate; the owner's own judgment of the "
                f"oracle (\"{_oracle_txt}\") is still required. Approve as "
                "the reviewing owner, or override=True to record a "
                "deliberate distinct-actor sign-off."
            )
            self._park_green_refusal(task_id, _park_reason)
            self._record_agent_run(
                task_id, gate_step_id, session_id, model=model,
                gate_state="pending", ok=False,
                verdict_summary=("rollup-not-human-evidence: "
                                  + _park_reason)[:200],
            )
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": "pending",
                "reason": _park_reason,
            }
        if (gate_step_id == "green_gate" and not override
                and not rollup_has_children and _has_oracle
                and not _human_judgment):
            receipt_reason, _fresh = self._oracle_receipt_refusal(
                _live, override=False, reason=reason)
            if receipt_reason:
                # REFUSAL PARKS PENDING, never `failed` (task 97d92854, epic
                # 37c9207b): "evidence not ready" is the system's verdict on
                # the RECEIPT, not on the work — writing `failed` here forced
                # the next honest approve through override=true. Mirrors
                # _park_green_refusal: pending + reason + audit rows, and the
                # refusal itself mints nothing.
                self._task_svc.update(
                    task_id, gate_state="pending", gate_reason=receipt_reason,
                )
                self._task_svc.record_history(
                    task_id, action="gate_decide",
                    details=(f"gate={gate_step_id}; action=approve; "
                             f"oracle-receipt=fail; reason={receipt_reason}"),
                    actor="conductor",
                )
                self._record_agent_run(
                    task_id, gate_step_id, session_id, model=model,
                    gate_state="pending", ok=False,
                    verdict_summary=("oracle-receipt: " + receipt_reason)[:200],
                )
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "pending",
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
                    # Same parking as the plain branch (task 97d92854): the
                    # override refusal keeps NO-OVERRIDE-SKIPS-THE-ORACLE
                    # intact (still ok:False, no receipt minted) but leaves
                    # the gate PENDING so a later honest approve — after the
                    # evidence lands — needs no override escalation.
                    self._task_svc.update(
                        task_id, gate_state="pending",
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
                        "gate_state": "pending",
                        "reason": receipt_reason,
                    }
            # Manual override path — bypass the verifier entirely but
            # tag the audit row so the override is auditable. The REAL
            # deciding actor (caller-supplied actor/session_id) is
            # persisted verbatim; 'manual-override' is only the fallback
            # literal when neither is supplied (task 1bc13307 — this used
            # to mask a real actor unconditionally).
            actor = _decided_by("manual-override")
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
            # The real deciding actor is persisted (task 1bc13307); 'conductor'
            # is only the fallback when neither actor nor session_id is given.
            # A demo/review (human-judgment) epic never reaches this branch on
            # a plain approve — task 457b38db's early park (above, before this
            # if/elif chain) already returned. It IS reached here on an
            # audited override=True (handled by the `if override:` branch
            # above, mutually exclusive with this elif) or on a non-
            # human-judgment epic, both unaffected.
            actor = _decided_by("conductor")
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
            # ORACLE-RECEIPT PRECEDENCE (68e5c699 interim), checked FIRST:
            # the trusted-runner EvidenceReceipt is the green gate's DESIGNED
            # deciding authority (inverted-flow #2). The in-daemon shell
            # verifier is known env-fragile AND can hang for tens of minutes
            # (a synchronous approve that never returns — no user can pass
            # it). A FRESH PASSING receipt (tree+spec+policy-pin matched)
            # therefore releases the gate on merit in seconds, WITHOUT
            # invoking the shell verifier; the verifier keeps its role for
            # un-receipted tasks. Judge-tamper and proof-artifact teeth
            # below still apply unchanged.
            outcome = None
            if gate_step_id == "green_gate":
                _r_refusal, _r_receipt = self._oracle_receipt_refusal(
                    task, override=False, reason=reason or "")
                if not _r_refusal and _r_receipt is not None:
                    outcome = {
                        "verified": True,
                        "reason": (
                            "released on the oracle-receipt tooth: fresh "
                            "passing EvidenceReceipt "
                            f"{str(getattr(_r_receipt, 'job_id', ''))[:8]} "
                            f"({getattr(_r_receipt, 'adapter', '')}) — a real "
                            "trusted run; shell verifier skipped (env-fragile"
                            "/hanging, 68e5c699)"),
                        "verifier": None,
                        "validation": "green_receipt",
                    }
            if outcome is None:
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
            # The real deciding actor is persisted (task 1bc13307); a machine
            # seat (ADJUDICATOR_SEAT) or a real human identity must survive
            # onto the history row, not collapse to the literal 'conductor'.
            actor = _decided_by("conductor")
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
            # (A human-judgment epic on a plain approve never reaches this
            # line — task 457b38db's early park above returns first; this
            # skip still applies to an audited override, which is correct.)
            artifact_reason = ""
        elif artifact_reason and has_captured_evidence(
                task_id, self._project_name or "default"):
            # 'Implement to evidence': a captured screenshot/video in the task's
            # evidence gallery IS the artifact (owner 2026-07-19).
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
                _children = self._task_svc.list(parent_id=task_id)
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
        # IDEMPOTENT DECISION (task b07fd46e): the verifier consult above can
        # take minutes; a timed-out client's retry may have decided this gate
        # while we were inside it. Re-fetch and require the task is STILL on
        # THIS gate with the gate in the SAME state we entered on — a lost
        # race is a RECORDED no-op naming the true step, never a second
        # 'passed' write or advance (mx-f8ed3f: red_gate overshot two steps).
        _live = self._task_svc.get(task_id)
        _live_step = getattr(_live, "workflow_step", "")
        _live_state = getattr(_live, "gate_state", "")
        if _live_step != gate_step_id or _live_state != task.gate_state:
            self._task_svc.record_history(
                task_id, action="gate_decide",
                details=(f"gate={gate_step_id}; action=approve; no-op: "
                         f"decision raced — task now at {_live_step}/"
                         f"{_live_state}"),
                actor=actor)
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": _live_state,
                "reason": (f"decision raced: this gate was already decided — "
                           f"the task is now at {_live_step} "
                           f"(gate_state={_live_state}); no second advance"),
            }
        self._task_svc.update(
            task_id,
            gate_state="passed",
            gate_reason=passed_gate_reason,
            # A prior _park() (ship_worker.py) or resume_actuator failure can
            # have stamped blocked_reason with a stage error that has since
            # been superseded by this genuine approval -- e.g. a green_gate
            # that failed once at ship's pr_create, then genuinely landed on
            # retry, kept showing "not yet reachable from origin/main" next
            # to a DONE/DELIVERED badge forever, because nothing on the
            # success path ever cleared it (owner, live: task 1291cd64,
            # 2026-08-23). This is the one place a gate legitimately passes,
            # so it is the one place stale blocked_reason must clear.
            blocked_reason="",
        )
        _delivery: Optional[dict] = None
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
            # FINISH THE PIPELINE (task cb1dc6f4): approving green_gate used
            # to stop at 'verified' and leave the task's code stranded on
            # its own branch — DONE on the board, absent from main. Land it
            # now, opt-in only (PRISM_DELIVERY_AUTOMERGE=1): deliver_task
            # re-verifies at the tree being merged, refuses red, parks on
            # conflict and never forces/resets main. Advisory — a delivery
            # failure never undoes the gate's own pass (green_gate=verified
            # and shipped=delivered are separate claims); it is recorded on
            # the task either way so the board reflects the truth.
            if _delivery_enabled():
                try:
                    _delivery = self.deliver_task(task_id, actor=actor)
                except Exception as exc:
                    _delivery = {"ok": False, "state": "error",
                                "reason": f"delivery raised {type(exc).__name__}: {exc}"}
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
        if _delivery is not None:
            response["delivery"] = _delivery
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

    def session_file_paths(self, session_id: str) -> dict:
        """The code files a session touched: {"read": [...], "modified": [...]}.

        Reads the files_read_paths / files_modified_paths JSON columns the
        transcript importer persists (task 961f273b). Guarded: a scores.db
        predating path capture (columns absent) or an unknown session yields
        empty lists — the caller just omits those session->code edges. Feeds
        the Explore mesh's session<->code neighborhood.
        """
        empty = {"read": [], "modified": []}
        if not (session_id or "").strip():
            return empty
        try:
            conn = self._scores_conn()
        except Exception:
            return empty
        try:
            cols = {r[1] for r in conn.execute(
                "PRAGMA table_info(session_outcomes)").fetchall()}
            if not ({"files_read_paths", "files_modified_paths"} & cols):
                return empty
            row = conn.execute(
                "SELECT files_read_paths, files_modified_paths "
                "FROM session_outcomes WHERE session_id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
        except Exception:
            return empty
        finally:
            conn.close()
        if row is None:
            return empty

        def _decode(raw) -> list:
            if not raw:
                return []
            try:
                val = json.loads(raw)
            except (TypeError, ValueError):
                return []
            return [str(p) for p in val if p] if isinstance(val, list) else []

        return {"read": _decode(row["files_read_paths"]),
                "modified": _decode(row["files_modified_paths"])}

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
            # cancelled is abandoned, deleted is removed. All keep their
            # workflow_step in the audit trail but must not render as
            # currently-managed tiles or feed the AWAITING REVIEW bar — a
            # deleted task parked at a pending gate is NOT awaiting review.
            if status in ("done", "cancelled", "deleted"):
                continue
            # Subtasks (parent_id set) belong under their parent's detail
            # page while IDLE — but a child the conductor is actively
            # engaged with (a live step or a gate awaiting decision) MUST
            # surface: the LIVE bar's whole promise is "who's working now +
            # what's stuck at a gate", and epic workstreams are children
            # (ui-redesign 16777a76: fix the board, never the tree).
            if getattr(t, "parent_id", "") and step == "" and gate == "none":
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
            gate = getattr(t, "gate_state", "none") or "none"
            if status in ("done", "cancelled", "deleted"):
                continue
            # Mirror managed_tasks: an ENGAGED child (live step or gate)
            # counts; an idle child stays under its parent.
            if getattr(t, "parent_id", "") and not step and gate == "none":
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
        # ONE query for every advance_task row (task-detail perf, 2026-08-07).
        # This walked list() and then history() PER TASK — 458 queries — to
        # compute a PROJECT-WIDE median that does not depend on the task being
        # rendered, on every GET /api/tasks/{id}. Same rows, same median.
        try:
            by_task = self._task_svc.advance_rows_all()
        except Exception:
            return self._TYPICAL_S_FALLBACK
        gaps: list[float] = []
        for rows in by_task.values():
            advs = [self._parse_iso(ts) for ts, _details in rows]
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
        # ONE query instead of list()-then-history()-per-task — see
        # _median_step_s. Identical rows, identical per-step medians.
        try:
            by_task = self._task_svc.advance_rows_all()
        except Exception:
            return out, counts
        buckets: dict = {}
        for rows in by_task.values():
            advs = []
            for ts_raw, details in rows:
                ts = self._parse_iso(ts_raw)
                m = _re.search(r"to=(\w+)", details or "")
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
                # parent_id-scoped (idx_tasks_parent) — same rows as the old
                # full-table load + Python filter, without the 458-row read.
                for t in self._task_svc.list(parent_id=task_id):
                    st = (getattr(t, "status", "") or "")
                    # CANCELLED children (e.g. the implement workflow's
                    # disposable ephemeral-fixture tasks) are abandoned, not
                    # pending work — counting them in the denominator dragged
                    # a green-gated parent's tile to 0% (task 7bdb5701).
                    # DELETED children are the same kind of abandoned row —
                    # excluded here to match _child_task_ids and keep the
                    # tile's N/M agreeing with epic_rollup_verdict's own
                    # active-child count (both must reach the same verdict
                    # about the same epic).
                    if st in ("cancelled", "deleted"):
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
        # Tokens inside the CURRENT step's window (see the scoping
        # note below). Initialised here so an unreadable transcript
        # yields an honest 0 rather than an undefined name.
        step_tokens = 0
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
                # AGGREGATE THE CHILDREN. A subtask is the driver's own
                # decomposition and never renders as a peer tile, but its
                # spend IS the parent's spend — an epic with three lanes
                # running underneath it must not read "0 tok/s" (owner
                # 2026-08-06). Deduped by session id, because a session
                # linked to both a child and its parent would otherwise be
                # counted twice.
                _seen_sids: set = set()
                _rows = [row
                         for _tid in ([task_id] + self._child_task_ids(task_id))
                         for row in self._task_svc.sessions_for_task(_tid)]
                for s in _rows:
                    _sid = (s.get("session_id") if isinstance(s, dict)
                            else getattr(s, "session_id", "")) or ""
                    if _sid and _sid in _seen_sids:
                        continue
                    if _sid:
                        _seen_sids.add(_sid)
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
                # SCOPE THE STEP READOUT TO THE STEP (owner 2026-08-06:
                # "this seems crazy"). `tokens` above is the task total across
                # every linked session's LIFETIME; rendering it beside the
                # current step produced "review previous notes - 7.5M tok -
                # 49s" (~153k tok/s, which no model produces) and "411M tok"
                # on a task open since July. The duration was real; the number
                # beside it described something else. Bucket the same event
                # timeline into the current step's window. The task total is
                # kept, under a name that says what it is.
                from prism_service.services.claude_transcripts import (
                    tokens_in_window as _tokens_in_window,
                )
                step_tokens = _tokens_in_window(
                    live_events, self._now_epoch() - in_step_s)
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
            # Tokens burned WITHIN the current step's window. Was the task
            # total across every linked session's lifetime, which is what
            # made a 49-second step read as 7.5M tokens.
            "tokens_since_step": step_tokens,
            # The lifetime total, named for what it actually is so nobody
            # renders it against a step again.
            "tokens_task_total": tokens,
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
    def _child_task_ids(self, task_id: str) -> list[str]:
        """Ids of tasks parented to `task_id` (one level).

        Subtasks are the driver's own decomposition and deliberately do not
        render as peers on the board — but their WORK is the parent's work.
        Without this, an epic with three lanes burning underneath it has no
        motion and no tokens of its own and reads "paused - 0 tok/s" exactly
        when the most is happening (owner 2026-08-06).
        """
        if self._task_svc is None or not task_id:
            return []
        try:
            # parent_id-scoped so this plans onto idx_tasks_parent (0.1ms)
            # instead of loading all 458 rows and filtering in Python (26ms).
            return [str(getattr(c, "id", "") or "")
                    for c in self._task_svc.list(parent_id=task_id)
                    if str(getattr(c, "status", "")) not in (
                        "cancelled", "deleted")]
        except Exception:
            return []

    def _own_transition_run_start(self, task_id: str) -> Optional[float]:
        """Timestamp of the start of the trailing run of this task's OWN
        advance_task/gate_decide rows that share the newest row's `to=`
        step -- i.e. how long the task has genuinely been sitting in its
        present step, not merely when task_history was last WRITTEN TO. A
        duplicate/re-recorded transition into the SAME step (e.g. a stale
        detector re-affirming state) must never look fresher than the
        run's true first entry."""
        try:
            rows = self._task_svc.history(task_id)
        except Exception:
            return None
        parsed = []
        for r in rows:
            if getattr(r, "action", "") not in ("advance_task", "gate_decide"):
                continue
            ts = self._parse_iso(getattr(r, "timestamp", "") or "")
            if ts is None:
                continue
            to_val = None
            for bit in (getattr(r, "details", "") or "").split(";"):
                bit = bit.strip()
                if bit.startswith("to="):
                    to_val = bit[3:]
                    break
            parsed.append((ts, to_val))
        if not parsed:
            return None
        parsed.sort(key=lambda p: p[0], reverse=True)
        newest_to = parsed[0][1]
        run_start = parsed[0][0]
        for ts, to_val in parsed:
            if to_val != newest_to:
                break
            run_start = ts
        return run_start

    def _task_motion_s(self, task) -> Optional[float]:
        """Seconds since the last CONDUCTOR TRANSITION on this task: the newest
        advance_task/gate_decide row in task_history (both written by
        advance_task/gate_decide via TaskService.record_history). Falls back to
        task.updated_at ONLY when the task has no transition history. None when
        neither resolves."""
        latest: Optional[float] = None
        tid = getattr(task, "id", "") or ""
        if self._task_svc is not None and tid:
            latest = self._own_transition_run_start(tid)
        # A transition on a CHILD is motion on the parent (see
        # _child_task_ids): the epic itself may not advance for hours while
        # its slices move constantly.
        if self._task_svc is not None and tid:
            try:
                for r in [row for cid in self._child_task_ids(tid)
                          for row in self._task_svc.history(cid)]:
                    if getattr(r, "action", "") in ("advance_task", "gate_decide"):
                        ts = self._parse_iso(getattr(r, "timestamp", "") or "")
                        if ts is not None and (latest is None or ts > latest):
                            latest = ts
            except Exception:
                pass
        # A SUB-TASK completing/moving IS the parent moving, even though the
        # parent's own step didn't transition — otherwise an epic reads "stalled"
        # for hours while its slices are actively getting done underneath it.
        if self._task_svc is not None and tid:
            try:
                # parent_id-scoped (idx_tasks_parent), same rows as before.
                for c in self._task_svc.list(parent_id=tid):
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

    def gate_waiting_s(self, task) -> Optional[float]:
        """Seconds since `task`'s CURRENT gate went pending (the /live
        graph's gate_waiting_s field, gamify data-enrichment slice). None
        when gate_state != 'pending' -- there is nothing to be waiting on.

        Reads the latest 'advance_task' history row whose details carry
        both 'gate=pending' and 'to=<current workflow_step>' -- the exact
        row advance_task itself writes the moment it parks the task on a
        gate (see advance_task's own record_history call above). Falls
        back to task.updated_at when no such row is found (e.g. seeded or
        imported data with no transition history) so the field still reads
        SOMETHING honest rather than None on a genuinely-pending gate."""
        if getattr(task, "gate_state", "") != "pending":
            return None
        tid = getattr(task, "id", "") or ""
        step = getattr(task, "workflow_step", "") or ""
        latest: Optional[float] = None
        if self._task_svc is not None and tid:
            try:
                for r in self._task_svc.history(tid):
                    if getattr(r, "action", "") != "advance_task":
                        continue
                    details = getattr(r, "details", "") or ""
                    if "gate=pending" not in details or f"to={step}" not in details:
                        continue
                    ts = self._parse_iso(getattr(r, "timestamp", "") or "")
                    if ts is not None and (latest is None or ts > latest):
                        latest = ts
            except Exception:
                latest = None
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
            # parent_id-scoped (idx_tasks_parent), same rows as before.
            return [c for c in self._task_svc.list(parent_id=tid)
                    if (getattr(c, "status", "") or "") != "cancelled"]
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
        # Third input (task e3b7ebf6): a task-attributed liveness heartbeat.
        # A real step routinely runs past both the 120s transition window
        # and the 90s session-quiet window with nothing wrong -- this is
        # the owner's "stalled must mean the owner has something to do"
        # complaint. heartbeat_age_s is scoped per task_id (never global);
        # a stale/absent heartbeat is None/over-window and changes nothing.
        beat = drive_heartbeat.latest(self._scores_db, getattr(task, "id", ""))
        heartbeat_age = beat["age_s"] if beat is not None else None
        driving = (heartbeat_age is not None
                   and heartbeat_age <= drive_heartbeat.HEARTBEAT_WINDOW_S)
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
                elif motion is not None and motion <= 120:
                    # The EPIC ITSELF just advanced. Driving a parent
                    # directly was invisible here: this branch only ever
                    # asked about children, so an epic crossing its own step
                    # boundaries still reported "paused".
                    state = "working"
                elif driving:
                    state = "driving"         # heartbeat-attributed liveness
                elif quiet is not None and quiet <= 90:
                    # A live linked session: someone IS working this, they
                    # just have not crossed a step boundary recently - and a
                    # long step produces none for most of its life. Reported
                    # as "driver active", never as nothing-happening. The
                    # owner watched real work read "paused" for hours
                    # because this signal was consulted only on the
                    # childless path (owner 2026-08-06: "it still looks like
                    # its frozen").
                    state = "adrift"
                elif done > 0:
                    state = "paused"         # progress made, idle between slices
                else:
                    state = "stalled"
            elif step.endswith("_gate") and gate in ("pending", "failed"):
                state = "awaiting_gate"      # a WAIT for review, not work
            elif motion is not None and motion <= 120:
                state = "working"            # a real recent transition on THIS task
            elif driving:
                state = "driving"            # heartbeat-attributed liveness
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
            # The driver's own progress evidence, threaded through ONLY while
            # fresh (inside HEARTBEAT_WINDOW_S) so the board can say WHAT a
            # driving step is doing (last tool, elapsed) instead of the bare
            # state word — a stale beat says nothing about now and is dropped.
            "heartbeat": (
                {
                    "step": beat.get("step"),
                    "last_tool": beat.get("last_tool"),
                    "elapsed_s": beat.get("elapsed_s"),
                    "age_s": round(heartbeat_age, 3),
                }
                if driving else None
            ),
        }
