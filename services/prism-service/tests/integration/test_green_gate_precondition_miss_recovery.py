"""GH#218 mirror — a green_gate verifier MISS is not sticky-fatal (task
1414a5e2).

THE MECHANISM (read live in this task's own worktree, cited by file:line):

  A first Approve at green_gate on a proof_type=test, no-oracle task whose
  fix lives OUTSIDE the workspace the gate measures (the shared checkout,
  never the task's own worktree — the standing project lesson) gets scoped
  by ``VerifierService._git_changed_files`` (verifier_service.py:145-199)
  against the task's own baseline via ``_verify_gate``
  (conductor_service.py:2782-2792: ``workspace=_tw["path"]``,
  ``baseline_rev=_tw["baseline"]``). No diff -> ``run_tier0`` returns ``[]``
  (verifier_service.py:447-449) -> ``VerifierService._summarize`` emits the
  literal string ``"no claims to verify (no diff in scope)"``
  (verifier_service.py:1198-1199) -> ``_verify_gate`` reports
  ``verified=False`` against the ``green_full`` rule -> ``gate_decide``
  writes ``gate_state="failed"`` (conductor_service.py:3564-3591).

  THE RECOVERY PATH UNDER TEST (conductor_service.py:3100-3131): once the
  fix is committed INTO the task's own worktree, a second
  ``action="approve"`` call with ``override`` OMITTED ENTIRELY (never
  ``override=False``) re-runs ``_oracle_receipt_refusal`` then
  ``_verify_gate`` against the task's CURRENT evidence
  (conductor_service.py:3100-3115); a ``verified is True`` recheck "fall[s]
  through to the normal approve path" (comment at conductor_service.py:3131)
  with no override signature at all. This self-heal already exists but had
  NEVER been demonstrated end-to-end against the real green_gate / tier0
  verifier path — the only prior recheck test
  (tests/unit/test_gate_autoclear_and_recheck.py::
  test_failed_gate_recovers_on_evidence_recheck_without_override) drives
  the story_gate RUBRIC, never the verifier.

NOTHING IN THE DECISION PATH IS STUBBED (likely_misfire (1)): every task
below runs against a REAL ``ConductorService`` (via
``project_context.get_project(...).conductor_svc``, which wires a REAL
``VerifierService``) over a REAL git repository. ``task_workspace.
workspace_for`` is monkeypatched only at the WORKSPACE-LOOKUP layer (never
the verifier itself) — ``_verify_gate`` imports the module inside the
function (conductor_service.py:2782-2786), so the attribute patch takes and
the real ``VerifierService.run -> run_tier0 -> _git_changed_files ->
_summarize`` chain executes for real.

Nothing here edits a control_plane.POLICY_FILES entry (AC-7): the only new
path is this test file itself.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def _head(repo: Path) -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True)
    return out.stdout.strip()


def _baseline_repo(tmp_path: Path, name: str) -> tuple[Path, str]:
    """A tiny real python repo (so ``_detect_python_project`` fires and
    Tier0 will run pytest on a real diff) with one baseline commit."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    (repo / "pyproject.toml").write_text(
        "[project]\nname = \"probe\"\nversion = \"0\"\n", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: empty probe project")
    return repo, _head(repo)


def _gated_task(project: str, oracle: str = "", proof_type: str = "test"):
    """A REAL ConductorService (wired to a REAL VerifierService via
    project_context) plus a task parked PENDING at green_gate, matching the
    established idiom (tests/integration/test_gate_adjudicator_seat.py:
    ``_gated_task``). No walk through story_gate/plan_gate/red_gate — this
    isolates the green_gate decision, which is what section 4 of this
    task's own plan_doc scopes the suite to."""
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task_svc = ctx.task_svc
    cond = ctx.conductor_svc
    task = task_svc.create(title="gh218 probe", oracle=oracle,
                           proof_type=proof_type)
    task_svc.update(task.id, workflow_step="green_gate", gate_state="pending")
    return task_svc, cond, task.id


def test_precondition_miss_self_heals_without_override_ac2_ac3_ac5(
        tmp_path, monkeypatch):
    """The full GH#218 sequence, strict order, as ONE test that fails if any
    step is skipped:

      (1) the fix exists only OUTSIDE the task's own worktree -> Approve
      (2) -> refusal, gate_state == 'failed', reason names the unmet
          precondition (no diff in scope) — NOT a measured verdict
      (3) commit the fix INTO the task's own worktree
      (4) -> Approve again, `override` OMITTED from the call entirely
      (5) -> gate clears: ok is True, gate_state == 'passed'

    AC-5 pin: the post-recovery gate_state assertion below is the
    already-implemented self-heal at conductor_service.py:3100-3131 under
    live test — not left as prose.
    """
    import prism_service.services.task_workspace as tw

    project = "ggp-" + uuid.uuid4().hex[:8]
    repo, baseline = _baseline_repo(tmp_path, "repo_a")

    ws = {"path": str(repo), "baseline": baseline, "repo_root": str(repo)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws)

    task_svc, cond, task_id = _gated_task(project)

    # --- Phase 1: the fix lives OUTSIDE this task's own worktree. The
    # repo the gate measures is untouched (clean vs baseline) — a "fix"
    # was written to an unrelated scratch dir the gate never sees.
    elsewhere = tmp_path / "shared_checkout_not_the_task_worktree"
    elsewhere.mkdir()
    (elsewhere / "fix.py").write_text("x = 1\n", encoding="utf-8")

    call1 = cond.gate_decide(
        task_id, action="approve",
        reason="fix landed outside this task's own workspace",
        actor="reviewer-1", session_id="reviewer-1")

    assert call1["ok"] is False, call1
    assert call1["gate_state"] == "failed", call1
    miss_reason = call1["reason"]
    assert "no diff in scope" in miss_reason, (
        "the refusal must name the unmet precondition (no diff was in "
        f"scope), got: {miss_reason!r}"
    )
    snap1 = task_svc.get(task_id)
    assert snap1.gate_state == "failed", (
        "readback after call 1 must match the call's own response"
    )

    # --- Phase 2: commit the SAME fix INTO the task's own worktree.
    (repo / "tests" / "test_probe.py").write_text(
        "def test_it():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"test(probe): land the fix [task:{task_id[:8]}]")

    # --- Phase 3: recover with `override` OMITTED ENTIRELY from the call
    # signature — never override=False (AC-3). A DISTINCT reviewer session
    # decides (no self-review), matching the gate's own no-self-review tooth.
    call2 = cond.gate_decide(
        task_id, action="approve",
        reason="fix committed into the task's own worktree; pytest now green",
        actor="reviewer-2", session_id="reviewer-2")

    assert call2["ok"] is True, call2
    assert call2["gate_state"] == "passed", (
        "the self-heal at conductor_service.py:3100-3131 must clear the "
        f"gate on a verified recheck with no override; got {call2!r}"
    )
    snap2 = task_svc.get(task_id)
    assert snap2.gate_state == "passed", (
        "readback after the recovery approve must match the call's own "
        "response — AC-5's pinned post-recovery assertion"
    )
    # green_gate is the LAST WORKFLOW_STEPS entry (task ae63e375,
    # conductor_service.py:3769-3778): "auto-advance past the gate" for the
    # TERMINAL gate means the task becomes done in place — advance_task has
    # no next step to move to and workflow_step correctly stays "green_gate"
    # (mirrors conductor_work's own `done` predicate: final step + gate_state
    # == passed). status flipping to "done" IS the advance signal here.
    assert snap2.status == "done", (
        "a passed TERMINAL green_gate must flip the task to done in place "
        f"(workflow_step legitimately stays 'green_gate'); got status="
        f"{snap2.status!r}, workflow_step={snap2.workflow_step!r}"
    )
    assert snap2.workflow_step == "green_gate", (
        "the terminal step has no successor to advance to; workflow_step "
        f"must remain 'green_gate', got {snap2.workflow_step!r}"
    )


def test_precondition_miss_reason_distinct_from_genuine_measured_failure_ac4(
        tmp_path, monkeypatch):
    """AC-4 contrast case: a sibling task whose committed change makes tier0
    measure a REAL failing test. The two refusal strings must differ, and
    the precondition-miss reason must name the unmet precondition (no diff)
    rather than carry a measured pass/fail tally the way a genuine verdict
    does."""
    import prism_service.services.task_workspace as tw

    # --- Sibling A: the precondition miss (identical shape to the main
    # sequence's Phase 1, reproduced standalone so this test is independent).
    project_a = "ggp-" + uuid.uuid4().hex[:8]
    repo_a, baseline_a = _baseline_repo(tmp_path, "repo_miss")
    ws_a = {"path": str(repo_a), "baseline": baseline_a, "repo_root": str(repo_a)}

    project_b = "ggp-" + uuid.uuid4().hex[:8]
    repo_b, baseline_b = _baseline_repo(tmp_path, "repo_fail")
    # A REAL committed FAILING change AFTER the baseline, so it is a real
    # diff in scope and tier0 measures an actual verdict.
    (repo_b / "tests" / "test_probe.py").write_text(
        "def test_it():\n    assert False\n", encoding="utf-8")
    _git(repo_b, "add", "-A")
    _git(repo_b, "commit", "-qm", "test(probe): a real failing change")
    ws_b = {"path": str(repo_b), "baseline": baseline_b, "repo_root": str(repo_b)}

    task_svc_a, cond_a, task_id_a = _gated_task(project_a)
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws_a)
    call_miss = cond_a.gate_decide(
        task_id_a, action="approve",
        reason="fix landed outside this task's own workspace",
        actor="reviewer-3", session_id="reviewer-3")
    assert call_miss["ok"] is False, call_miss
    miss_reason = call_miss["reason"]

    task_svc_b, cond_b, task_id_b = _gated_task(project_b)
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws_b)
    call_fail = cond_b.gate_decide(
        task_id_b, action="approve",
        reason="independent re-run: pytest -q -> 1 failed",
        actor="reviewer-4", session_id="reviewer-4")
    assert call_fail["ok"] is False, call_fail
    fail_reason = call_fail["reason"]

    assert miss_reason != fail_reason, (
        "a precondition miss and a genuine measured failure must produce "
        f"distinct reason strings; both were: {miss_reason!r}"
    )
    assert "no diff in scope" in miss_reason
    assert "no diff in scope" not in fail_reason
    # The miss NAMES its unmet precondition; it does not carry a measured
    # pass/fail tally the way a genuine verdict does.
    import re
    assert re.search(r"\d+/\d+ pass", miss_reason) is None, (
        f"a precondition miss must not read as a measured tally: {miss_reason!r}"
    )
    assert re.search(r"\d+/\d+ pass", fail_reason) is not None, (
        f"a genuine measured failure must carry a pass/fail tally: {fail_reason!r}"
    )
