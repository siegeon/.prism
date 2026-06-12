"""RED scaffold — Reinforce SDLC gates (real proof, no self-override) +
source-of-truth write-back doctrine (task 3826dac3).

These tests pin the USER-FACING seams the conductor/verifier must grow:

  * VerifierService.run() must run in the AGENT CHECKOUT, and
    conductor_service._verify_gate must pass that concrete workspace
    through (today it passes none -> daemon cwd -> status=error 'no diff').
  * The run() contract must surface a non-empty SCOPE so a captured run
    proves real diff scope (today there is no scope key).
  * Tier0 must run the FULL suite at the gates, not only changed test
    files (today _run_python_tools scopes pytest to changed test files).
  * A gate cannot be cleared by the SAME actor that produced the work:
    a same-actor override is rejected (today override has no actor guard).
  * Proof-carrying artifacts generalized to ALL gates: red_gate needs a
    committed failing-test trace, green_gate a captured full-suite-green;
    override CANNOT bypass the artifact requirement.

They FAIL today and pass once the feature lands.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class CapturingVerifier:
    """Records every run() call (kwargs) and returns a scripted result so
    tests can assert WHAT workspace/scope conductor passed in."""

    def __init__(self, result: Optional[dict] = None) -> None:
        self.calls: list[dict] = []
        self.next_result: Optional[dict] = result

    def run(self, **kwargs: object) -> dict:
        self.calls.append(dict(kwargs))
        assert self.next_result is not None, "no result queued"
        return self.next_result


def _services(tmp_path, verifier=None, project_root: Optional[str] = None):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    kw = {}
    if project_root is not None:
        kw["project_root"] = project_root
    cond = ConductorService(
        str(tmp_path / "scores.db"), enable_engine=False,
        task_svc=task_svc, verifier_svc=verifier, **kw,
    )
    return task_svc, cond


def _workflow():
    from prism_service.models.workflow import WORKFLOW_STEPS
    return WORKFLOW_STEPS


def _gate_id(gid: str) -> str:
    return next(s["id"] for s in _workflow()
               if s["type"] == "gate" and s["id"] == gid)


def _walk_to_gate(cond, task_id: str, gate_id: str) -> None:
    target_idx = next(i for i, s in enumerate(_workflow()) if s["id"] == gate_id)
    guard = (target_idx + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if snap.workflow_step == gate_id and snap.gate_state == "pending":
            return
        if snap.gate_state == "pending":
            # Intermediate gates are cleared with an override that carries a
            # real proof-trace + a DISTINCT actor per clear (task 8579d49e:
            # story_gate + plan_gate precede red_gate now, and a reused walk
            # actor would be a same-actor self-override on the second gate).
            # The actor guard is exercised on the gate-under-test calls below.
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason=("walk intermediate; independent re-run: "
                        "pytest -q -> 2 failed / 0 passed"),
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}",
            )
            continue
        cond.advance_task(task_id)


# ======================================================================
# 1. VERIFIER RUNS IN THE AGENT CHECKOUT — _verify_gate must pass a
#    concrete workspace into verifier.run(), not the daemon cwd.
# ======================================================================


def test_verify_gate_passes_concrete_workspace_into_verifier_run(tmp_path):
    """conductor_service._verify_gate must hand verifier.run() the agent
    checkout (project_root), so Tier0 sees the real diff. Today run() is
    called with NO workspace kwarg -> daemon cwd -> 'no diff in scope'."""
    checkout = tmp_path / "agent_checkout"
    checkout.mkdir()
    verifier = CapturingVerifier({
        "status": "fail", "tier0": "fail", "tier1": "not-run",
        "tier2": "skipped", "summary": "1 fail", "scope": ["x.py"],
    })
    task_svc, cond = _services(tmp_path, verifier, project_root=str(checkout))
    t = task_svc.create(title="ws threading")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    cond.gate_decide(t.id, action="approve", reason="red landed")

    assert verifier.calls, "verifier must be consulted"
    call = verifier.calls[-1]
    assert call.get("workspace"), (
        "verify_gate passed no workspace -> verifier scopes the daemon cwd"
    )
    assert str(checkout) in str(call["workspace"])


def test_verifier_run_result_surfaces_nonempty_scope(tmp_path):
    """A captured real verifier run must report a non-empty scope (the
    changed files it actually checked), so a gate decision can prove it
    saw a real diff rather than status=error 'nothing to verify'."""
    from prism_service.services.verifier_service import VerifierService
    import subprocess

    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "add", "-A"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "base"], cwd=ws, check=True)
    (ws / "changed.py").write_text("x = 1\n")

    svc = VerifierService(scores_db=str(tmp_path / "s.db"),
                          workspace=str(ws))
    out = svc.run(workspace=str(ws))
    assert "scope" in out, "run() result must surface the changed-file scope"
    assert out["scope"], "scope must be non-empty when a file changed"
    assert any("changed.py" in str(p) for p in out["scope"])


# ======================================================================
# 2. FULL SUITE AT THE GATES — Tier0 must run the whole pytest suite,
#    not only changed test files / changed-file lint.
# ======================================================================


def test_tier0_runs_full_suite_not_only_changed_test_files(tmp_path):
    """When a NON-test source file changes, the gate verifier must still
    run the full pytest suite (so a regression in an untouched test is
    caught). Today _run_python_tools only runs pytest when a *test* file
    changed, so a non-test change yields NO pytest claim at all."""
    from prism_service.services import verifier_service as vs

    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")

    captured: dict = {}

    def fake_run_tool(cmd, workspace, timeout_s=60.0):
        # Tier0 invokes pytest as `[sys.executable, "-m", "pytest", ...]`
        # (cd44b22 — venv-only installs expose `python -m pytest`, not a
        # bare `pytest` on PATH), so detect it by membership, not cmd[0].
        if cmd and "pytest" in cmd:
            captured["pytest_cmd"] = list(cmd)
        return (0, "1 passed", "")

    monkey_changed = ["src/feature.py"]   # a NON-test source file only
    orig_changed = vs._git_changed_files
    orig_tool = vs._run_tool
    vs._git_changed_files = lambda w, b=None: monkey_changed
    vs._run_tool = fake_run_tool
    try:
        claims = vs.run_tier0(ws)
    finally:
        vs._git_changed_files = orig_changed
        vs._run_tool = orig_tool

    kinds = {c.kind for c in claims}
    assert "tooling.pytest" in kinds, (
        "full suite must run at the gate even when only non-test files "
        "changed — Tier0 skipped pytest because no test file changed"
    )
    # And it must be the FULL suite invocation, not a scoped file list.
    assert captured.get("pytest_cmd"), "pytest was never invoked"
    assert "src/feature.py" not in captured["pytest_cmd"], (
        "pytest must run the whole suite, not just the changed file"
    )


# ======================================================================
# 3. NO SELF-OVERRIDE — a gate cannot be cleared by the SAME actor that
#    produced the work; override demoted to a distinct-actor exception.
# ======================================================================


def test_same_actor_override_is_rejected(tmp_path):
    """The driver overriding its own gate is forbidden. The actor who
    produced the work (recorded on the gate / session) cannot be the same
    actor that overrides it. Today override has NO actor guard."""
    verifier = CapturingVerifier({
        "status": "pass", "tier0": "pass", "tier1": "pass",
        "tier2": "skipped", "summary": "green",
    })
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="self override forbidden")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    # Record driver-A as the actor that produced the work on this task.
    link = getattr(task_svc, "link_session", None)
    if callable(link):
        link(t.id, "driver-A")

    # The same actor ("driver-A") that drove the work tries to override.
    result = cond.gate_decide(
        t.id, action="approve", reason="trust me", override=True,
        actor="driver-A", session_id="driver-A",
    )
    assert result["ok"] is False, (
        "same-actor self-override must be rejected"
    )
    assert "actor" in (result.get("reason") or "").lower()


def test_independent_actor_override_is_allowed(tmp_path):
    """An override BY A DISTINCT actor (independent verifier sub-agent)
    is still permitted and logged as a separate exception."""
    verifier = CapturingVerifier({
        "status": "pass", "tier0": "pass", "tier1": "pass",
        "tier2": "skipped", "summary": "green",
    })
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="independent override ok")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    result = cond.gate_decide(
        t.id, action="approve",
        reason="independent verifier re-ran cmd: pytest -q -> 2 failed",
        override=True, actor="verifier-bot", session_id="verifier-bot",
    )
    assert result["ok"] is True
    assert result["gate_state"] == "passed"


# ======================================================================
# 4. PROOF-CARRYING ARTIFACTS GENERALIZED — red_gate needs a committed
#    failing-test trace; green_gate needs captured full-suite-green.
#    The artifact SHAPE is machine-validated and override CANNOT bypass.
# ======================================================================


def test_red_gate_requires_committed_failing_test_trace(tmp_path):
    """red_gate must reject a close that carries no failing-test trace
    artifact on the task row (proof_type/completion_proof), even when the
    verifier would pass. Generalizes the ui-artifact tooth to red_gate."""
    verifier = CapturingVerifier({
        "status": "fail", "tier0": "fail", "tier1": "not-run",
        "tier2": "skipped", "summary": "red", "scope": ["t.py"],
    })
    task_svc, cond = _services(tmp_path, verifier)
    # No proof_type / completion_proof set -> malformed artifact.
    t = task_svc.create(title="red needs trace")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    result = cond.gate_decide(t.id, action="approve",
                              reason="red landed (but no artifact)")
    assert result["ok"] is False, (
        "red_gate must require a committed failing-test trace artifact"
    )
    assert "artifact" in (result.get("reason") or "").lower() \
        or "trace" in (result.get("reason") or "").lower()


def test_override_cannot_bypass_missing_red_artifact(tmp_path):
    """Override is demoted: it can skip the VERIFIER but NOT the
    proof-carrying artifact requirement. A red_gate override with no
    failing-test trace is still rejected."""
    verifier = CapturingVerifier({
        "status": "fail", "tier0": "fail", "tier1": "not-run",
        "tier2": "skipped", "summary": "red",
    })
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="override no artifact")
    _walk_to_gate(cond, t.id, _gate_id("red_gate"))

    result = cond.gate_decide(
        t.id, action="approve", reason="override, trust me",
        override=True, actor="independent-bot", session_id="independent-bot",
    )
    assert result["ok"] is False, (
        "override must NOT bypass the proof-carrying artifact requirement"
    )


def test_green_gate_requires_full_suite_green_artifact(tmp_path):
    """green_gate must require a captured full-suite-green artifact
    (machine-validated shape: exit code + test counts), not a self-
    attested string. A close with no such artifact is rejected."""
    verifier = CapturingVerifier({
        "status": "pass", "tier0": "pass", "tier1": "pass",
        "tier2": "skipped", "summary": "green",
    })
    task_svc, cond = _services(tmp_path, verifier)
    t = task_svc.create(title="green needs suite artifact")
    _walk_to_gate(cond, t.id, _gate_id("green_gate"))

    result = cond.gate_decide(
        t.id, action="approve",
        reason="looks green to me",   # self-attested, no captured artifact
    )
    assert result["ok"] is False, (
        "green_gate must require a captured full-suite-green artifact"
    )
