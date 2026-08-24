"""blocked_reason must self-heal once the condition that set it clears
(discovered during task 3baadd19's own QA pass, 2026-08-24) -- a general
adjudicate_green_gate fix, not something that pins task 3baadd19's own
oracle.

LIVE REGRESSION this pins: _park_green_refusal (conductor_service.py) writes
BOTH gate_reason and blocked_reason when adjudicate_green_gate's shipped-ness
pre-flight objects ("this task's [task:<id8>] commit trailer is not yet
reachable from origin/main"). gate_adjudicator's periodic sweep re-computes
and re-writes gate_reason every pass via _write_pending_reason, so that field
self-heals -- but nothing symmetric ever touched blocked_reason once the
objection stopped firing, so a task whose branch got landed AFTER the tooth
first parked it kept showing "commit trailer is not yet reachable" forever,
with no code path short of the owner clicking Approve (the ship-on-approve
queue path) to clear it. Observed live: task 3baadd19's own AC-1 branch was
merged to origin/main by hand, yet task.blocked_reason still read the stale
refusal text on every subsequent read.

Fix: adjudicate_green_gate now clears a stale blocked_reason on the same
sweep that finds neither the screen-claim nor the shipped-ness tooth
objecting, scoped to status != "blocked" so it never touches
resume_actuator's unrelated dependency-retry park (which always pairs
status="blocked" with its own blocked_reason, unlike this tooth)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


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


def _gated_task(tmp_path, *, tags=None, oracle="", proof_type="",
                completion_proof="", verify=None):
    from prism_service.services.task_service import TaskService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="stale blocked_reason fixture", tags=tags or [],
                        oracle=oracle, proof_type=proof_type,
                        completion_proof=completion_proof)
    task_svc.update(t.id, verify=verify or [], workflow_step="green_gate",
                    gate_state="pending")
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc):
    from prism_service.services.conductor_service import ConductorService

    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    cond._project_name = "testproj"
    return cond


def test_blocked_reason_is_set_while_unshipped_then_clears_once_landed(
    tmp_path, monkeypatch,
):
    from prism_service.services import task_workspace

    task_svc, task = _gated_task(
        tmp_path, tags=["backend"],
        oracle="the drive worker claims one task and completes it",
        proof_type="demo",
        completion_proof="film/screenshots in the evidence store",
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

    # Pass 1: genuinely unshipped -- the machine seat must park pending with
    # blocked_reason naming the unreachable trailer.
    res1 = cond.adjudicate_green_gate(task.id)
    assert res1 is None
    after1 = task_svc.get(task.id)
    assert after1.gate_state == "pending"
    assert "origin/main" in (after1.blocked_reason or "").lower() or \
        "reachable" in (after1.blocked_reason or "").lower(), (
        f"expected the shipped-ness refusal on blocked_reason, "
        f"got {after1.blocked_reason!r}")

    # Land the branch for real.
    _git(repo, "merge", "-q", "--no-ff", "-m", f"merge {branch}", branch)
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "fetch", "-q", "origin")

    # Pass 2: now genuinely shipped. proof_type="demo" still keeps the
    # machine seat abstaining (human-only per eaafdf75), so res2 stays None
    # -- but blocked_reason must no longer carry the now-false claim that
    # the commit is unreachable.
    res2 = cond.adjudicate_green_gate(task.id)
    assert res2 is None, (
        "a demo-type gate must stay human-only even once shipped-ness "
        "clears -- this test isolates the blocked_reason healing, not a "
        "machine auto-approve")
    after2 = task_svc.get(task.id)
    assert after2.gate_state == "pending"
    assert (after2.blocked_reason or "") == "", (
        f"blocked_reason must clear once the branch is genuinely shipped, "
        f"but still reads {after2.blocked_reason!r} -- this is the exact "
        f"live staleness task 3baadd19 hit")


def test_blocked_reason_from_an_unrelated_dependency_block_is_left_alone(
    tmp_path, monkeypatch,
):
    """The clear must be scoped to THIS tooth's own park -- a task blocked
    for an unrelated reason (status='blocked', e.g. resume_actuator's retry-
    budget park) must never have its blocked_reason silently wiped just
    because it happens to also sit at a clean green_gate pre-flight."""
    from prism_service.services import task_workspace

    task_svc, task = _gated_task(
        tmp_path, tags=["backend"], oracle="GET /health returns 200",
        proof_type="demo", completion_proof="screenshot in evidence store",
    )
    repo = _repo_with_bare_origin(tmp_path)
    # No commit under this task's own trailer at all -- shipped-ness is not
    # this tooth's business (fail-open), isolating this test to the
    # unrelated-block scoping guard alone.
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})

    unrelated_reason = ("resume-actuator: retry budget spent (3/3) — "
                        "parked for a human")
    task_svc.update(task.id, status="blocked", blocked_reason=unrelated_reason)

    cond = _conductor(tmp_path, task_svc)
    cond.adjudicate_green_gate(task.id)

    after = task_svc.get(task.id)
    assert after.blocked_reason == unrelated_reason, (
        "an unrelated status='blocked' park must never be cleared by this "
        f"tooth's un-park -- got {after.blocked_reason!r}")


def test_adjudicate_green_gate_source_scopes_the_unpark_to_status_not_blocked():
    """Pin the actual guard, not just its effect -- a driving agent reading
    this source should see the status check right next to the clear."""
    import inspect

    from prism_service.services.conductor_service import ConductorService

    src = inspect.getsource(ConductorService.adjudicate_green_gate)
    assert 'blocked_reason=""' in src
    assert '!= "blocked"' in src
