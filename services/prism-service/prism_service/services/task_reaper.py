"""The conductor pipeline's terminal REAP node (task f97c196d).

WHAT IT IS. `land` is the FSM's ship step (.prism/behaviors/conductor/
land.json, executed today by ship_worker.py). `reap` is the step AFTER it:
once the work is really on origin/main, the drive's git worktree and its
`prism/ws/<task_id>` branch are dead weight, and nothing removed them. On
2026-08-30 this repo carried 256 worktrees and 474 branches, 352 of them
`prism/ws/*`; 127 of 154 branches whose task row was gone held work that
was already on main.

WHY IT IS CODIFIED. Deterministic Python plus git, zero model calls. It is
reached over `POST /api/workflows/steps/reap` -- the same http-callback
shape /steps/premise-gather and /steps/green-gate-check use. Only
/steps/reason-loop and /steps/premise-judge are agentic routes.

WHY IT IS NOT IN `WORKFLOW_STEPS`. models/workflow.py is read by
conductor_service.py, a control_plane.POLICY_FILES entry, and 14+ call
sites treat green_gate as literally the last state. The node is registered
where `land` already is: the behavior FSM (bot.json), the Workflows page
catalog (api/workflows._CONDUCTOR_LINKED_BEHAVIOR_IDS) and the canvas node
list (flow_run_recorder.CONDUCTOR_NODES). Same precedent, same reasoning.

THE FIVE SAFETY RULES, each with the line that keeps it:

  1. Never remove a worktree with uncommitted work.
     `git status --porcelain` must be EMPTY, and `git worktree remove` runs
     WITHOUT `--force` so git refuses a second time on its own.
  2. Never delete a branch whose commits exist nowhere else.
     `git cherry origin/main <branch>` (patch equivalence, so a cherry-pick
     or a rebase still counts as landed) must report no `+` lines, unless
     rule 3 already proved the work shipped.
  3. Shippedness is the TASK TRAILER on origin/main. The parent-chain
     question git can answer is the WRONG one here: 47 of 48 branches it
     called unmerged on this repo had in fact shipped under a squash sha,
     as a sibling commit. This module delegates to
     api.tasks._shipped_sha_on_main -- the gate's own reader -- so the reap
     and the gate can never disagree about whether a task landed.
  4. Never touch a worktree a drive is standing in. The shared checkout is
     refused by path, a git-locked worktree is refused, and `is_live`
     (drive heartbeat) refuses the rest.
  5. Reap only a genuinely finished task: `done` or `cancelled`.

FAILS CLOSED. Any error probing the repo keeps everything. A leaked
directory costs disk; a wrongly-reaped one costs work that has no other
copy.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from prism_service.services import task_workspace

# The canvas/FSM id of this node. flow_run_recorder re-exports it as
# REAP_NODE so the drawn node and the recorded run can never drift.
REAP_NODE = "reap"

# A task is finished when its row says so. `done` is the normal path (the
# land step flipped it); `cancelled` work is abandoned by definition.
FINISHED_STATUSES = frozenset({"done", "cancelled"})

_ONTOLOGY_KIND = "conductor.reap"


def _verdict(task_id: str, outcome: str, reason: str, **extra) -> dict:
    """One typed result object, the same shape whatever happened -- a caller
    never has to guess which keys are present (CLAUDE.md: a step validation
    result is a typed ontology object, not raw command output)."""
    out = {
        "kind": _ONTOLOGY_KIND,
        "node_id": REAP_NODE,
        "workflow_id": "conductor",
        "task_id": task_id,
        "outcome": outcome,
        "reaped": False,
        "would_reap": False,
        "worktree_removed": False,
        "branch_deleted": False,
        "path": "",
        "branch": "",
        "shipped_sha": "",
        "unique_commits": 0,
        "reason": reason,
    }
    out.update(extra)
    return out


def _shipped_sha(repo_root: str, task_id: str) -> str:
    """The gate's OWN squash-safe trailer reader, never a second copy of it
    (a duplicate would drift from the tooth that decides green_gate)."""
    from prism_service.api.tasks import _shipped_sha_on_main

    return _shipped_sha_on_main(str(repo_root), str(task_id)) or ""


def _locked_worktrees(root: Path) -> set[str]:
    """Paths git itself reports as locked -- a lock is somebody saying "I am
    using this", and the reap honours it."""
    try:
        out = task_workspace._git_out(root, "worktree", "list", "--porcelain")
    except (RuntimeError, OSError):
        return set()
    locked, current = set(), ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):].strip()
        elif line.startswith("locked"):
            locked.add(str(Path(current)))
    return locked


def _unique_commits(root: Path, branch: str, upstream: str) -> Optional[int]:
    """How many commits on `branch` have no patch-equivalent on `upstream`.

    None means the question could not be answered, which the caller treats
    as "keep everything" -- never as zero.
    """
    try:
        out = task_workspace._git_out(root, "cherry", upstream, branch)
    except (RuntimeError, OSError):
        return None
    return len([ln for ln in out.splitlines() if ln.startswith("+")])


def _upstream_ref(root: Path) -> str:
    """origin/main when the repo has it, else main -- the reap must still be
    able to answer rule 2 in a checkout with no remote configured."""
    for ref in ("origin/main", "main", "HEAD"):
        try:
            task_workspace._git_out(root, "rev-parse", "--verify", ref)
            return ref
        except (RuntimeError, OSError):
            continue
    return "HEAD"


def _branch_exists(root: Path, branch: str) -> bool:
    try:
        task_workspace._git_out(root, "rev-parse", "--verify",
                                f"refs/heads/{branch}")
        return True
    except (RuntimeError, OSError):
        return False


def _forget(task_id: str) -> None:
    """Drop the workspace index row -- ensure_workspace only reuses a record
    whose path still exists, so a stale row would block a later re-drive."""
    try:
        idx = task_workspace._load_index()
        if idx.pop(task_id, None) is not None:
            task_workspace._save_index(idx)
    except Exception:  # noqa: BLE001 - bookkeeping never fails a reap
        pass


def reap_task(
    task_id: str,
    *,
    status: str,
    repo_root: Optional[str] = None,
    mode: str = "reap",
    is_live: Optional[Callable[[str], bool]] = None,
) -> dict:
    """Remove one finished task's worktree and branch, or say why not.

    `mode="survey"` answers the same question and deletes nothing -- the
    reap.json behavior runs it as its first step so the decision is on file
    before anything is removed.
    """
    tid = str(task_id or "").strip()
    if not tid:
        return _verdict(tid, "refused", "no task id")
    if str(status or "").strip().lower() not in FINISHED_STATUSES:
        return _verdict(tid, "refused",
                        f"task is not finished (status={status!r}); only "
                        f"{sorted(FINISHED_STATUSES)} are reaped")
    if is_live is not None:
        try:
            live = bool(is_live(tid))
        except Exception:  # noqa: BLE001 - fail closed
            live = True
        if live:
            return _verdict(tid, "refused",
                            "a live drive is standing in this worktree")

    rec = task_workspace.workspace_record(tid) or {}
    branch = str(rec.get("branch") or f"prism/ws/{tid}")
    ws_path = str(rec.get("path") or "")
    try:
        root = Path(repo_root or rec.get("repo_root") or
                    task_workspace._prism_repo_root())
    except Exception as exc:  # noqa: BLE001 - fail closed
        return _verdict(tid, "refused", f"cannot resolve the repo: {exc}",
                        branch=branch, path=ws_path)
    if not (root / ".git").exists():
        return _verdict(tid, "refused", f"{root} is not a git checkout",
                        branch=branch, path=ws_path)

    ws = Path(ws_path) if ws_path else None
    if ws is not None and ws.exists():
        # Rule 4: the shared checkout is not anybody's task worktree.
        if ws.resolve() == root.resolve():
            return _verdict(tid, "refused",
                            "the workspace record points at the main checkout",
                            branch=branch, path=ws_path)
        if str(ws) in _locked_worktrees(root):
            return _verdict(tid, "refused", "the worktree is locked",
                            branch=branch, path=ws_path)
        # Rule 1: uncommitted work has no other copy anywhere.
        try:
            dirty = task_workspace._git_out(ws, "status", "--porcelain").strip()
        except (RuntimeError, OSError) as exc:
            return _verdict(tid, "refused",
                            f"could not read the worktree, keeping it: {exc}",
                            branch=branch, path=ws_path)
        if dirty:
            return _verdict(tid, "refused",
                            f"uncommitted changes in {ws} "
                            f"({len(dirty.splitlines())} path(s))",
                            branch=branch, path=ws_path)
    else:
        ws = None
        ws_path = ""

    if ws is None and not _branch_exists(root, branch):
        return _verdict(tid, "pass", "nothing left to reap", branch=branch)

    # Rule 3: the TRAILER on origin/main, never a parent-chain walk.
    shipped = _shipped_sha(str(root), tid)
    unique = 0
    if not shipped:
        # Rule 2: unshipped work survives unless every commit already has a
        # patch-equivalent upstream (a cherry-pick or a rebase counts).
        upstream = _upstream_ref(root)
        counted = _unique_commits(root, branch, upstream)
        if counted is None:
            return _verdict(tid, "refused",
                            "could not compare the branch against "
                            f"{upstream}, keeping it",
                            branch=branch, path=ws_path)
        unique = counted
        if unique:
            return _verdict(
                tid, "refused",
                f"{unique} commit(s) on {branch} exist nowhere else and no "
                f"[task:{tid[:8]}] trailer is on {upstream}",
                branch=branch, path=ws_path, unique_commits=unique)

    if mode == "survey":
        return _verdict(tid, "pass",
                        "shipped and clean; the reap step will remove this"
                        if shipped else
                        "clean and every commit is already upstream",
                        branch=branch, path=ws_path, shipped_sha=shipped,
                        would_reap=True)

    removed = _remove_worktree(root, ws)
    if ws is not None and not removed["ok"]:
        return _verdict(tid, "refused", removed["reason"],
                        branch=branch, path=ws_path, shipped_sha=shipped)
    deleted = _delete_branch(root, branch)
    _forget(tid)
    try:
        task_workspace._git_out(root, "worktree", "prune")
    except (RuntimeError, OSError):
        pass

    reaped = (ws is None or removed["ok"]) and deleted["ok"]
    return _verdict(
        tid, "pass" if reaped else "refused",
        deleted["reason"] if not deleted["ok"] else
        (f"reaped; the work is on origin/main as {shipped[:8]}" if shipped
         else "reaped; every commit was already upstream"),
        reaped=reaped, worktree_removed=bool(ws is not None and removed["ok"]),
        branch_deleted=deleted["ok"], branch=branch, path=ws_path,
        shipped_sha=shipped, unique_commits=unique)


def _remove_worktree(root: Path, ws: Optional[Path]) -> dict:
    """`git worktree remove` WITHOUT --force -- git's own dirty check is the
    second net behind rule 1's explicit `status --porcelain` gate.

    The node_modules junction is detached FIRST: on Windows a recursive
    delete follows that link back into the main checkout, which has already
    destroyed a real node_modules/.bin once (task_workspace.remove_workspace).
    """
    if ws is None:
        return {"ok": True, "reason": ""}
    try:
        task_workspace._detach_link(ws / task_workspace._NODE_MODULES)
    except OSError:
        pass
    try:
        task_workspace._git_out(root, "worktree", "remove", str(ws))
    except (RuntimeError, OSError) as exc:
        return {"ok": False, "reason": f"git refused to remove {ws}: {exc}"}
    if ws.exists():
        return {"ok": False, "reason": f"{ws} still exists after removal"}
    return {"ok": True, "reason": ""}


def _delete_branch(root: Path, branch: str) -> dict:
    """`branch -D`, reached only after rule 2 or rule 3 proved the commits
    are not the last copy. Verified by re-reading the ref."""
    if not branch:
        return {"ok": True, "reason": ""}
    if not _branch_exists(root, branch):
        return {"ok": True, "reason": ""}
    try:
        task_workspace._git_out(root, "branch", "-D", branch)
    except (RuntimeError, OSError) as exc:
        return {"ok": False, "reason": f"git refused to delete {branch}: {exc}"}
    if _branch_exists(root, branch):
        return {"ok": False, "reason": f"{branch} still exists after delete"}
    return {"ok": True, "reason": ""}
