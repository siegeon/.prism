"""RED scaffold — "A test-proof ticket cannot claim a screen" (task
8a737f2f).

TWO independent honesty-gate holes in conductor_service.py:

HOLE 1 — FR-4's proof_type opt-out (ui_artifact_gate_reason, :349-351)
laundering: ANY declared non-demo proof_type (typically 'test') short-
circuits the WHOLE demonstrable-UI tooth, with no check of what the
oracle TEXT actually claims. A ui-tagged, proof_type='test' task whose
oracle names a user-reachable surface (a :8888 URL, a route, a nav
entry, a page a person lands on) can therefore machine-close green_gate
on a pytest receipt alone — a suite passing proves nothing about what
renders on that surface. The fix narrows FR-4 (never deletes it): when
the oracle TEXT names a surface, treat this as a HUMAN sign-off (the
adjudicator seat abstains and parks pending with an actionable reason;
a genuine distinct human's plain Approve still succeeds, because their
own look at the screen IS the evidence FR-4 was trying to approximate
with a pytest receipt).

HOLE 2 — no shipped-ness check before full_outcome_complete/status=done.
gate_decide stamps a green_gate pass as the task being DONE regardless
of whether its `[task:<id8>]` commit trailer has ever reached
origin/main. DONE means SHIPPED (mx: feedback_done_means_shipped) — the
fix adds a pre-flight, resolved via `git log origin/main --grep` (never
merge-base --is-ancestor, which task 499ba9c9 documents as false-
negative on squash merges; never the daemon's own checkout HEAD, which
sits on whatever branch this SHARED repo happens to have checked out),
that parks the WHOLE approve pending — never gate_state=failed — when
the trailer is not yet reachable.

Both teeth are cheap, objective, ABSTAIN-ONLY pre-flights: they PARK
pending with a recorded reason (mirrors `_park_green_refusal`,
task e0149f1f's "compute a reason and don't drop it" precedent), never
flip a gate to failed, and when BOTH fire on the same task (task
b22576bb's exact closing state, replayed below) the parked reason names
BOTH counts, not just whichever tooth happened to run first.

These drive the REAL dispatchers — ConductorService.gate_decide and
ConductorService.adjudicate_green_gate, the seams MCP conductor_gate and
the machine seat actually call — never a new isolated helper in
isolation only. A test that merely imported a new is_screen_claim()
function would pass even if neither dispatcher ever consulted it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# git fixture helpers (mirrors test_delivery_detects_squash_merge.py's /
# test_gate_adjudicator_seat.py's style — a REAL bare origin, never a
# stubbed git layer, never the real E:\.prism checkout).
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


def _repo_with_bare_origin(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    bare = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _commit(repo: Path, path: str, body: str, subject: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD")


# ---------------------------------------------------------------------------
# gate_decide harness (mirrors test_green_gate_ui_artifact.py's style)
# ---------------------------------------------------------------------------


def _gated_task(tmp_path, *, tags=None, oracle="", proof_type="",
                completion_proof="", verify=None):
    """A task parked PENDING at green_gate with the given shape — no walk
    through earlier gates required (mirrors test_gate_adjudicator_seat.py's
    `_gated_task`)."""
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="screen-claim fixture", tags=tags or [],
                        oracle=oracle, proof_type=proof_type,
                        completion_proof=completion_proof)
    task_svc.update(t.id, verify=verify or [], workflow_step="green_gate",
                    gate_state="pending")
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc, verifier=None):
    from prism_service.services.conductor_service import ConductorService

    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=verifier)
    cond._project_name = "testproj"
    return cond


# ── Group A — screen_claim_gate_reason: pure, text-driven tooth ──────────


def test_A1_ui_test_task_naming_a_screen_is_flagged():
    from prism_service.services.conductor_service import (
        screen_claim_gate_reason,
    )

    reason = screen_claim_gate_reason(
        ["ui"], "test",
        "load http://127.0.0.1:8888/tasks/8a737f2f and see the card render",
    )
    assert reason, (
        "a ui task declaring proof_type='test' whose oracle names a "
        "user-reachable surface (a :8888 URL) must be flagged — a pytest "
        "receipt proves a suite ran, not what renders on that surface"
    )
    assert "8888" in reason or "surface" in reason.lower() or \
        "screen" in reason.lower()


def test_A2_ui_test_task_with_no_surface_in_oracle_is_unaffected():
    from prism_service.services.conductor_service import (
        screen_claim_gate_reason,
    )

    # FR-4's narrowing target: a ui task that declares proof_type='test'
    # AND whose oracle names a metric/build-count (no screen) must keep the
    # existing opt-out untouched — the tooth narrows, it must not
    # blanket-block every non-demo ui task.
    reason = screen_claim_gate_reason(
        ["ui"], "test", "build-count: default MCP tool surface 41 -> 31",
    )
    assert reason == "", (
        "a ui/test task whose oracle names a metric with NO user-reachable "
        f"surface must be untouched by this tooth (got {reason!r})"
    )


def test_A3_demo_proof_type_is_governed_by_the_existing_ui_artifact_tooth():
    from prism_service.services.conductor_service import (
        screen_claim_gate_reason,
    )

    # proof_type='demo' is STRAND C's own territory (ui_artifact_gate_reason)
    # — this tooth must not double-fire on it.
    reason = screen_claim_gate_reason(
        ["ui"], "demo", "see it render at http://127.0.0.1:8888/tasks/x",
    )
    assert reason == ""


def test_A4_non_ui_task_is_never_this_tooths_business():
    from prism_service.services.conductor_service import (
        screen_claim_gate_reason,
    )

    # A ui TAG is not what decides this — but the absence of the tag still
    # must not trip it (stop_if: never key off the tag alone in the OTHER
    # direction either — a non-ui task citing a URL is simply out of scope).
    reason = screen_claim_gate_reason(
        ["backend"], "test",
        "hit http://127.0.0.1:8888/api/x and confirm the response",
    )
    assert reason == ""


def test_A5_unset_proof_type_is_governed_by_the_existing_ui_artifact_tooth():
    from prism_service.services.conductor_service import (
        screen_claim_gate_reason,
    )

    reason = screen_claim_gate_reason(
        ["ui"], "", "see it render at http://127.0.0.1:8888/tasks/x",
    )
    assert reason == ""


# ── Group B — adjudicate_green_gate: the machine seat abstains ───────────


_SCREEN_ORACLE = (
    "open http://127.0.0.1:8888/tasks/8a737f2f and confirm the parked "
    "reason string is present on the loaded page"
)


def test_B1_adjudicator_abstains_and_parks_an_actionable_reason(tmp_path):
    task_svc, task = _gated_task(
        tmp_path, tags=["ui"], oracle=_SCREEN_ORACLE, proof_type="test",
        completion_proof="tests/unit/test_thing.py::test_x PASSED",
    )
    cond = _conductor(tmp_path, task_svc)

    res = cond.adjudicate_green_gate(task.id)

    assert res is None, "the machine seat must NEVER close a screen-claim"
    after = task_svc.get(task.id)
    assert after.gate_state == "pending", (
        "a refused machine approve must PARK pending, never flip to failed"
    )
    assert after.workflow_step == "green_gate"
    assert after.gate_reason, (
        "the refusal reason must be RECORDED (task e0149f1f) — an empty "
        "gate_reason leaves the driver unable to self-diagnose"
    )
    assert "8888" in after.gate_reason or "surface" in \
        after.gate_reason.lower() or "screen" in after.gate_reason.lower()


def test_B2_captured_evidence_does_NOT_hand_this_to_the_machine(tmp_path):
    """Regression guard for requirement #2: STRAND C's 'a captured
    screenshot satisfies the demo requirement' shortcut (has_captured_
    evidence) must NOT be reused to let the machine close a screen-claim —
    the oracle names something SEEN, not something MEASURED, so even a
    real captured artifact keeps this a human sign-off."""
    from prism_service.data_dir import evidence_dir

    task_svc, task = _gated_task(
        tmp_path, tags=["ui"], oracle=_SCREEN_ORACLE, proof_type="test",
        completion_proof="tests/unit/test_thing.py::test_x PASSED",
    )
    d = evidence_dir(task.id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "capture.png").write_bytes(b"\x89PNG\r\n\x1a\n0000")

    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)

    assert res is None, (
        "a captured screenshot must NOT flip a screen-claim task to "
        "machine-approvable — it is still a HUMAN sign-off"
    )
    after = task_svc.get(task.id)
    assert after.gate_state == "pending"
    assert after.gate_reason


def test_B3_wiring_the_machine_seat_actually_consults_both_teeth():
    """AC-5-equivalent (mirrors test_adjudicator_calls_BOTH_teeth_before_
    approving's precedent): pin that adjudicate_green_gate's OWN source
    calls the new screen-claim tooth AND the shipped-ness tooth, not merely
    that they exist somewhere in the module."""
    import inspect

    from prism_service.services.conductor_service import ConductorService

    src = inspect.getsource(ConductorService.adjudicate_green_gate)
    assert "screen_claim_gate_reason" in src
    assert "_unshipped_gate_reason" in src


# ── Group C — shipped-ness (Hole 2), through the real gate_decide path ───


_BACKEND_PROOF = "tests/unit/test_backend.py::test_x PASSED"


def test_C1_shipped_work_still_closes_normally(tmp_path, monkeypatch):
    """Regression guard: a task whose [task:<id8>] trailer IS reachable
    from origin/main must close exactly as before — the new tooth narrows,
    it must not become a blanket block on every green_gate."""
    from prism_service.services import task_workspace

    task_svc, task = _gated_task(
        tmp_path, tags=["backend"], oracle="", proof_type="test",
        completion_proof=_BACKEND_PROOF,
    )
    repo = _repo_with_bare_origin(tmp_path)
    _commit(repo, "feature.txt", "work\n",
           f"add feature [task:{task.id[:8]}]")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})

    cond = _conductor(tmp_path, task_svc)
    result = cond.gate_decide(task.id, action="approve",
                              reason="test: shipped work")

    assert result["ok"] is True, (
        f"a SHIPPED task was rejected at green_gate (reason="
        f"{result.get('reason')!r}) — the shipped-ness tooth must not "
        "block work that already reached origin/main"
    )
    assert result["gate_state"] == "passed"
    after = task_svc.get(task.id)
    assert after.status == "done"


def test_C2_unshipped_work_parks_pending_not_done(tmp_path, monkeypatch):
    """AC-4: a task whose commits exist ONLY on its own unmerged branch must
    leave the gate PENDING (never failed, never passed/done) with a reason
    naming the unshipped branch — full_outcome_complete must never fire on
    work the machine cannot see landed on origin/main."""
    from prism_service.services import task_workspace

    task_svc, task = _gated_task(
        tmp_path, tags=["backend"], oracle="", proof_type="test",
        completion_proof=_BACKEND_PROOF,
    )
    repo = _repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task.id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n",
           f"add feature [task:{task.id[:8]}]")
    _git(repo, "checkout", "-q", "main")  # main/origin never sees the branch
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})

    cond = _conductor(tmp_path, task_svc)
    result = cond.gate_decide(task.id, action="approve",
                              reason="test: unshipped work")

    assert result["ok"] is False, (
        "an UNSHIPPED task machine-approved at green_gate — DONE means "
        "SHIPPED (mx: feedback_done_means_shipped)"
    )
    assert result["gate_state"] == "pending", (
        "an unshipped close must PARK pending, never flip to failed"
    )
    after = task_svc.get(task.id)
    assert after.status != "done"
    assert after.full_outcome_complete is not True
    assert "origin/main" in (result.get("reason") or "") or \
        "shipped" in (result.get("reason") or "").lower()


def test_C3_b22576bb_replay_parks_with_a_reason_covering_both_counts(
    tmp_path, monkeypatch,
):
    """AC-6 / requirement #5: replaying task b22576bb's exact closing state
    (ui tag, proof_type=test, oracle citing http://127.0.0.1:8888/
    ?project=prism, empty evidence dir, unpushed branch) against the new
    decision path must leave the gate PENDING with a reason covering BOTH
    the surface-claim and the shipped-ness counts — never a silent
    machine-approve on just the first tooth that happened to run."""
    from prism_service.services import task_workspace

    task_svc, task = _gated_task(
        tmp_path, tags=["ui"],
        oracle="http://127.0.0.1:8888/?project=prism",
        proof_type="test",
        completion_proof="tests/unit/test_thing.py::test_x PASSED",
    )
    repo = _repo_with_bare_origin(tmp_path)
    branch = f"prism/ws/{task.id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "feature.txt", "work\n",
           f"add feature [task:{task.id[:8]}]")
    _git(repo, "checkout", "-q", "main")  # unpushed: main never sees it
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})

    cond = _conductor(tmp_path, task_svc)
    res = cond.adjudicate_green_gate(task.id)

    assert res is None, "the replay must NOT machine-approve"
    after = task_svc.get(task.id)
    assert after.gate_state == "pending"
    reason = (after.gate_reason or "").lower()
    names_surface = "8888" in reason or "surface" in reason or \
        "screen" in reason
    names_shipped = "origin/main" in reason or "shipped" in reason or \
        "unpushed" in reason or "unmerged" in reason
    assert names_surface, (
        f"the replay's parked reason must name the surface-claim count: "
        f"{after.gate_reason!r}"
    )
    assert names_shipped, (
        f"the replay's parked reason must ALSO name the shipped-ness "
        f"count: {after.gate_reason!r}"
    )


# ── Group D — a genuine human sign-off still works ────────────────────────


class _AlwaysPassVerifier:
    def run(self, **kwargs: object) -> dict:
        return {"status": "pass", "tier0": "pass", "tier1": "pass",
                "tier2": "skipped", "summary": "all green"}


def test_D1_a_distinct_humans_plain_approve_still_closes_the_gate(
    tmp_path,
):
    """Requirement #2: a screen-claim task with NO shipped-ness problem is
    still a HUMAN sign-off, not a machine one — a genuine distinct human's
    plain Approve (never the adjudicator seat) must succeed, because their
    own look at the surface IS the evidence FR-4 approximated with a pytest
    receipt. (No git workspace is wired here, so the shipped-ness tooth is
    fail-open/not-this-tooth's-business and isolates this assertion to the
    screen-claim half alone.)"""
    task_svc, task = _gated_task(
        tmp_path, tags=["ui"], oracle=_SCREEN_ORACLE, proof_type="test",
        completion_proof="tests/unit/test_thing.py::test_x PASSED",
    )
    cond = _conductor(tmp_path, task_svc, verifier=_AlwaysPassVerifier())

    result = cond.gate_decide(
        task.id, action="approve",
        reason="human: I opened the page and saw the card render",
    )

    assert result["ok"] is True, (
        f"a genuine human's plain approve on a screen-claim task was "
        f"rejected (reason={result.get('reason')!r}) — the oracle names "
        "something a PERSON must see; their own Approve is the sign-off, "
        "not a pytest receipt"
    )
    assert result["gate_state"] == "passed"
