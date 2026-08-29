"""Plan steps read LIVE task state, not a stored snapshot (task 70d14f84).

Observed live 2026-08-28: four root plans were rejected for one cause --
each drive wrote from a stale worktree HEAD, a stored task.gate_reason,
and a child list from an earlier session (9f60a849 proposed work already
merged in 814a4077; f1906073 listed six children when nine existed).

CONTRACT PINNED HERE (plan_doc, R-1..R-8): a new non-policy helper
`prism_service.services.plan_live_state` exposes
  compute(task, task_svc, readiness_fn, main_checkout, worktree_path,
          fetch_timeout_s=1.5) -> dict   and   render(live_state) -> str
and `conductor_flow._job` attaches `live_state` to the job dict for the
draft_story and verify_plan steps ONLY, appending render() to the
instructions. `_job` resolves the daemon's main checkout through
`plan_live_state.main_checkout()` and the task worktree through
`task_workspace.workspace_for(task_id)["path"]`; both are monkeypatched
here onto a temp repo so no test ever reaches the network.

ANTI-VACUITY: every job assertion runs against the RENDERED job of a REAL
task standing on draft_story / verify_plan, fetched through the same
flow_next a driver calls (test_verify_plan_step_states_its_contract.py).
Committed tests-only at the red step; every test here is RED until the
helper and the _job wiring exist.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_COMPLIANT_STORY = (
    "## Summary\nProbe.\n\n## Requirements\nNone.\n\n"
    "## Acceptance Criteria\n"
    "- AC-1: probe renders — oracle: pytest -k probe\n"
)
_MARKER = "STALE-GATE-REASON-MARKER-70d14f84"


def _git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout.strip()


def _make_repos(root: Path) -> dict:
    """bare origin; main checkout (clone) with origin/main ONE commit
    ahead of the worktree branch cut earlier."""
    env = ["-c", "user.name=t", "-c", "user.email=t@t"]
    bare = root / "origin.git"
    _git(root, "init", "--bare", "-b", "main", str(bare))
    main = root / "main"
    _git(root, "clone", "-q", str(bare), str(main))
    (main / "a.txt").write_text("a\n")
    _git(main, *env, "add", "a.txt")
    _git(main, *env, "commit", "-qm", "base")
    _git(main, "push", "-q", "origin", "HEAD:main")
    base_sha = _git(main, "rev-parse", "HEAD")
    wt = root / "wt"
    _git(main, "worktree", "add", "-q", "-b", "task-branch", str(wt), base_sha)
    (main / "b.txt").write_text("b\n")
    _git(main, *env, "add", "b.txt")
    _git(main, *env, "commit", "-qm", "ahead")
    _git(main, "push", "-q", "origin", "HEAD:main")
    ahead_sha = _git(main, "rev-parse", "HEAD")
    # local origin/main is updated by the push; drop it back so only a
    # real `git fetch` restores the ahead sha.
    _git(main, "update-ref", "refs/remotes/origin/main", base_sha)
    return {"bare": bare, "main": main, "wt": wt,
            "base_sha": base_sha, "ahead_sha": ahead_sha}


@pytest.fixture(scope="module")
def live():
    from prism_service import config
    from prism_service.api import conductor_flow as cf
    from prism_service.project_context import get_project, release_project
    from prism_service.services import task_workspace
    from prism_service.services import plan_live_state as pls

    mp = pytest.MonkeyPatch()
    tmp = tempfile.TemporaryDirectory()
    project = "plan-steps-live-state-70d14f84"
    try:
        root = Path(tmp.name)
        repos = _make_repos(root)
        mp.setattr(config, "PROJECTS_DIR", root / "projects")
        mp.setattr(pls, "main_checkout", lambda: str(repos["main"]))
        mp.setattr(task_workspace, "workspace_for",
                   lambda task_id: {"path": str(repos["wt"])})
        release_project(project)
        ctx = get_project(project)
        parent = ctx.task_svc.create(title="live state probe parent")
        kids = {}
        for st in ("done", "pending", "cancelled"):
            k = ctx.task_svc.create(title=f"child {st}", parent_id=parent.id)
            ctx.task_svc.update(k.id, status=st)
            kids[st] = k.id
        ctx.task_svc.update(kids["pending"], gate_reason=_MARKER)
        other = ctx.task_svc.create(title="other root, not a child")

        res = ctx.conductor_svc.advance_task(parent.id)
        assert res.get("to_step") == "review_previous_notes", res
        ctx.task_svc.update(parent.id, premise_notes=(
            "## Premises\n- throwaway probe - UNVERIFIED\n"))
        res = ctx.conductor_svc.advance_task(parent.id)
        assert res.get("to_step") == "draft_story", res
        draft_job = cf.flow_next(parent.id, project=project)["job"]
        assert draft_job["step"] == "draft_story", draft_job

        ctx.task_svc.update(parent.id, plan_doc=_COMPLIANT_STORY)
        res = ctx.conductor_svc.advance_task(parent.id)
        assert res.get("to_step") == "story_gate", res
        cf._autoclear_machine_gate(ctx.conductor_svc, parent.id)
        assert ctx.task_svc.get(parent.id).workflow_step == "verify_plan"
        t0 = time.perf_counter()
        plan_job = cf.flow_next(parent.id, project=project)["job"]
        plan_job_s = time.perf_counter() - t0
        assert plan_job["step"] == "verify_plan", plan_job

        # a task on a NON-plan step for AC-5
        ctx.task_svc.update(parent.id, plan_doc=_COMPLIANT_STORY)
        res = ctx.conductor_svc.advance_task(parent.id)
        assert res.get("to_step") == "plan_gate", res
        ctx.conductor_svc.gate_decide(
            parent.id, "approve", session_id="test-distinct-seat",
            model="test", reason="test seat")
        assert ctx.task_svc.get(parent.id).workflow_step == \
            "write_failing_tests"
        red_job = cf.flow_next(parent.id, project=project)["job"]

        yield {"project": project, "ctx": ctx, "task_id": parent.id,
               "kids": kids, "other": other.id, "repos": repos,
               "draft_job": draft_job, "plan_job": plan_job,
               "plan_job_s": plan_job_s, "red_job": red_job}
    finally:
        release_project(project)
        mp.undo()
        try:
            tmp.cleanup()
        except OSError:
            pass


# AC-1 -----------------------------------------------------------------

def test_origin_main_sha_is_the_fetched_origin_main_not_the_worktree_head(live):
    ls = live["plan_job"]["live_state"]
    assert ls["origin_main_sha"] == live["repos"]["ahead_sha"], ls
    assert ls["origin_main_fetched"] is True, ls
    assert ls["worktree_head"] == live["repos"]["base_sha"], ls
    assert ls["origin_main_sha"] != ls["worktree_head"]
    assert ls["worktree_behind_origin_main"] is True, ls


# AC-2 -----------------------------------------------------------------

def test_child_count_matches_task_list_parent_id_across_all_statuses(live):
    ls = live["plan_job"]["live_state"]
    expected = {t.id for t in live["ctx"].task_svc.list(
        parent_id=live["task_id"])}
    assert expected == set(live["kids"].values())
    assert ls["child_count"] == 3, ls
    assert {c["id"] for c in ls["children"]} == expected, ls
    assert {c["status"] for c in ls["children"]} == \
        {"done", "pending", "cancelled"}, ls
    assert live["other"] not in {c["id"] for c in ls["children"]}


# AC-3 -----------------------------------------------------------------

def test_readiness_is_live_not_the_stored_gate_reason(live):
    from prism_service.api import conductor as capi

    ls = live["plan_job"]["live_state"]
    kid = next(c for c in ls["children"] if c["id"] == live["kids"]["pending"])
    assert "readiness" in kid, kid
    assert _MARKER not in repr(kid["readiness"]), kid
    direct = capi.gate_readiness(kid["id"], project=live["project"])
    assert kid["readiness"] == direct, (kid["readiness"], direct)


# AC-4 -----------------------------------------------------------------

@pytest.mark.parametrize("which", ["draft_job", "plan_job"])
def test_instructions_name_live_reads_for_both_plan_steps(live, which):
    text = live[which]["instructions"]
    sha = live["repos"]["ahead_sha"]
    assert sha in text, (which, text[-600:])
    assert "children: 3" in text, (which, text[-600:])
    assert f"Cite `origin/main {sha}` and `children: 3`" in text, \
        (which, text[-600:])
    for st in ("done", "pending", "cancelled"):
        assert live["kids"][st][:8] in text, (which, st)


def test_render_is_what_the_instructions_carry(live):
    from prism_service.services import plan_live_state as pls

    ls = live["plan_job"]["live_state"]
    assert live["plan_job"]["instructions"].endswith(pls.render(ls))


# AC-5 -----------------------------------------------------------------

def test_only_plan_steps_carry_live_state_and_other_keys_are_unchanged(live):
    assert "live_state" not in live["red_job"], sorted(live["red_job"])
    prior = {"task_id", "step", "kind", "role", "role_label", "gate_state",
             "gate_reason", "instructions", "doctrine", "expected_proof",
             "contract"}
    assert set(live["draft_job"]) == prior | {"live_state"}, \
        sorted(live["draft_job"])
    assert set(live["plan_job"]) == prior | {"live_state"}


# AC-6 -----------------------------------------------------------------

def test_fetch_timeout_degrades_to_the_local_ref_and_says_so(live):
    from prism_service.services import plan_live_state as pls

    main = live["repos"]["main"]
    _git(main, "remote", "set-url", "origin",
         "file:///nonexistent/70d14f84/origin.git")
    try:
        local = _git(main, "rev-parse", "origin/main")
        task = live["ctx"].task_svc.get(live["task_id"])
        ls = pls.compute(task, live["ctx"].task_svc, lambda _id: {},
                         str(main), str(live["repos"]["wt"]),
                         fetch_timeout_s=1.5)
    finally:
        _git(main, "remote", "set-url", "origin", str(live["repos"]["bare"]))
    assert ls["origin_main_fetched"] is False, ls
    assert ls["origin_main_sha"] == local, ls
    assert "UNFETCHED" in pls.render(ls), pls.render(ls)


# AC-7 -----------------------------------------------------------------

def test_under_two_seconds_for_a_plan_step_job_with_three_children(live):
    assert live["plan_job_s"] < 2.0, live["plan_job_s"]


# AC-8 -----------------------------------------------------------------

SLICE_FILES = (
    "services/prism-service/prism_service/api/conductor_flow.py",
    "services/prism-service/prism_service/services/plan_live_state.py",
    "services/prism-service/prism_service/__version__.py",
    "services/prism-service/tests/unit/test_plan_steps_read_live_state.py",
)


def test_no_policy_file_touched():
    from prism_service.services import control_plane, plan_live_state  # noqa

    for p in SLICE_FILES:
        assert control_plane.is_policy_file(p) is False, p
    assert Path(plan_live_state.__file__).name == "plan_live_state.py"
