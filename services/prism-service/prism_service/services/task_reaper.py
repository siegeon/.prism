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

THE SWEEP (task ab: reap-node-non-task-worktrees, ops incident 2026-09-12).
`reap_task` above only ever runs for the ONE task_id a land just finished
(`ship_worker._reap_after_land`), keyed off the workspace index. An agent
worktree (`.claude/worktrees/agent-*`), a QA/fixer worktree
(`/home/siegeon/wt-*`, a job's own scratch worktree) or a `prism/ws/*`
branch whose task row was later deleted has no matching task_id, so it is
INVISIBLE to that hook forever, however clean and however long dead.
Measured live: 173 registered git worktrees, disk at 98%, 93 of them
already landed on origin/main and clean.

`sweep_worktrees` is the task-agnostic net: `git worktree list
--porcelain` is the ground truth, never the workspace index, so it finds
every worktree regardless of who created it or whether any task row still
names it. Reuses rules 1 (dirty) and 4 (main checkout, locked, in-use) from
above unchanged, adds an age grace window (a worktree with no dirty files
YET, between two commands, is not proof of abandonment), and answers rule 3
differently: there is no task row here to read a `[task:<id>]` trailer
for, so this is the one place in this module that asks git directly
whether a branch is an ancestor of origin/main -- the is-ancestor
prohibition on `reap_task` is about misattributing SHIPPEDNESS to a
specific task when the trailer disagrees with the graph; with no task to
attribute to, the graph question is the only one there is to ask, and a
worktree that fails it is kept, never removed on a guess.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time as _time
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


# ---------------------------------------------------------------------------
# The sweep: every registered worktree, no task row required.
# ---------------------------------------------------------------------------

SWEEP_NODE = "reap_sweep"

#: PRISM_REAP_SWEEP_MIN_AGE_H overrides the age grace window (hours).
SWEEP_MIN_AGE_ENV = "PRISM_REAP_SWEEP_MIN_AGE_H"
DEFAULT_SWEEP_MIN_AGE_H = 24.0


def _sweep_min_age_h() -> float:
    raw = os.environ.get(SWEEP_MIN_AGE_ENV, "")
    try:
        return float(raw) if raw.strip() else DEFAULT_SWEEP_MIN_AGE_H
    except ValueError:
        return DEFAULT_SWEEP_MIN_AGE_H


def _worktree_list(root: Path) -> list[dict]:
    """Parse `git worktree list --porcelain` -- the ground truth for what
    is actually registered, independent of task_workspace's own index."""
    try:
        out = task_workspace._git_out(root, "worktree", "list", "--porcelain")
    except (RuntimeError, OSError):
        return []
    entries: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                entries.append(cur)
            cur = {"path": line[len("worktree "):].strip()}
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith(
                "refs/heads/") else ref
        elif line == "bare":
            cur["bare"] = True
        elif line == "detached":
            cur["detached"] = True
        elif line.startswith("locked"):
            cur["locked"] = True
    if cur:
        entries.append(cur)
    return entries


def _is_ancestor(root: Path, ref: str, upstream: str) -> Optional[bool]:
    """True/False from `merge-base --is-ancestor`'s own exit code (0/1),
    None on anything else (a bad ref, a timeout) -- the caller treats None
    as "cannot tell", i.e. keep it. Raw subprocess, never `_git_out`:
    `_git_out` raises on a non-zero exit, which cannot distinguish "not an
    ancestor" (rc 1, a real, common answer) from a genuine error."""
    try:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ref, upstream],
            cwd=str(root), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode == 0:
        return True
    if r.returncode == 1:
        return False
    return None


def _older_than(path: Path, hours: float) -> bool:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return False
    return (_time.time() - mtime) >= hours * 3600.0


def _proc_cwd_is_under(path: Path) -> bool:
    """True if any process on this host has its cwd inside `path` --
    /proc/*/cwd, Linux-only (this daemon runs on WSL2/Linux). Missing
    /proc (a non-Linux host) answers False, not an error: the dirty/age/
    ancestor checks are the primary safety net here, this is a best-effort
    extra layer on top of them, not the only one."""
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    try:
        target = path.resolve()
    except OSError:
        return False
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return False
    for entry in pids:
        try:
            cwd = (entry / "cwd").resolve()
        except OSError:
            continue
        try:
            cwd.relative_to(target)
        except ValueError:
            continue
        return True
    return False


_TASK_BRANCH_PREFIX = "prism/ws/"

# A task in any of these states is finished; every other status (in_progress,
# blocked, pending, or anything future) means the task is still active and
# its worktree must survive the sweep regardless of age or cleanliness.
_ACTIVE_STATUSES_ARE_EVERYTHING_ELSE = FINISHED_STATUSES


def _task_id_from_branch(branch: str) -> Optional[str]:
    """The task id a `prism/ws/<task_id>` branch names, or None for any
    other branch shape (an agent/QA/fixer worktree, which never has a task
    row to protect it here)."""
    b = str(branch or "")
    if not b.startswith(_TASK_BRANCH_PREFIX):
        return None
    tid = b[len(_TASK_BRANCH_PREFIX):].strip()
    return tid or None


def _live_task_guard_reason(branch: str) -> Optional[str]:
    """None when this worktree is free to be judged on age/cleanliness/
    ancestry as before; a reason string when a REAL task row says it is
    still active and must survive regardless of any of that.

    Ops incident 2026-09-13, task a65c66e5: that task was `in_progress` at
    `verify_plan`, its worktree was clean, and its branch was a pure
    ancestor of origin/main (zero unique commits -- the ordinary shape of a
    task that has not committed past its baseline yet). The sweep reaped it
    on age alone. This guard is checked BEFORE dirty/age/ancestor so none of
    those can ever override it.

    No task row for the id (an orphaned branch, or a task_id belonging to
    no project this process knows about) returns None -- the OLD
    task-agnostic behavior, unchanged (see
    test_the_sweep_reaps_a_task_worktree_too_when_orphaned).
    """
    task_id = _task_id_from_branch(branch)
    if not task_id:
        return None
    try:
        from prism_service.project_context import get_all_projects, get_project
    except Exception:  # noqa: BLE001 - the guard degrades to "no opinion"
        return None
    try:
        project_ids = get_all_projects()
    except Exception:  # noqa: BLE001
        return None
    for pid in project_ids:
        try:
            task = get_project(pid).task_svc.get(task_id)
        except Exception:  # noqa: BLE001 - one bad project never blocks the rest
            continue
        if task is None:
            continue
        status = str(getattr(task, "status", "") or "").strip().lower()
        if status not in _ACTIVE_STATUSES_ARE_EVERYTHING_ELSE:
            return (f"task {task_id[:8]} is still {status or 'active'} in "
                    f"project {pid!r}; the sweep never reaps a live task's "
                    "own worktree")
        return None  # found the task row and it is finished; no restriction
    return None


def _forget_paths(gone: set[str]) -> None:
    """Drop any workspace index row pointing at a path the sweep just
    removed -- the same bookkeeping `_forget` does for `reap_task`, just
    keyed by path instead of task_id since a sweep target may have no
    task_id at all."""
    if not gone:
        return
    try:
        idx = task_workspace._load_index()
        changed = False
        for tid, rec in list(idx.items()):
            if str(rec.get("path") or "") in gone:
                idx.pop(tid, None)
                changed = True
        if changed:
            task_workspace._save_index(idx)
    except Exception:  # noqa: BLE001 - bookkeeping never fails a sweep
        pass


def sweep_worktrees(
    repo_root: Optional[str] = None,
    *,
    mode: str = "reap",
    min_age_h: Optional[float] = None,
    is_path_live: Optional[Callable[[Path], bool]] = None,
) -> dict:
    """Reap every registered worktree that is clean, old enough, unused,
    and already landed -- whether or not any task row ever pointed at it.

    A worktree survives (is kept, never removed) unless ALL of:
      - it is not the main checkout and not a bare repo entry
      - it is not git-locked
      - `git status --porcelain` is empty (ignored files do not count --
        that flag already excludes them)
      - its directory mtime is at least `min_age_h` old (default
        `DEFAULT_SWEEP_MIN_AGE_H`, overridable via
        `PRISM_REAP_SWEEP_MIN_AGE_H`)
      - no process on the host has its cwd inside it (`is_path_live`, or
        `_proc_cwd_is_under` by default)
      - its branch (or, detached, its HEAD) is an ancestor of origin/main

    `mode="survey"` computes every verdict and removes nothing. A missing
    worktree directory is pruned either way (`git worktree prune`), and a
    branch is deleted only once its own worktree has actually been
    removed (or was already gone) AND it is confirmed merged.

    Fails closed exactly like `reap_task`: any error reading a candidate
    keeps it. Returns a summary dict with a `items` list, one entry per
    registered worktree, so a caller (or a test) can inspect why any one
    of them was kept or reaped.
    """
    root = Path(repo_root) if repo_root else task_workspace._prism_repo_root()
    age_h = DEFAULT_SWEEP_MIN_AGE_H if min_age_h is None else float(min_age_h)
    live_check = is_path_live or _proc_cwd_is_under

    try:
        task_workspace._git_out(root, "fetch", "origin")
    except (RuntimeError, OSError):
        pass  # best-effort refresh; a stale origin/main just keeps more

    upstream = _upstream_ref(root)
    try:
        root_resolved = root.resolve()
    except OSError:
        root_resolved = root

    items: list[dict] = []
    reaped_paths: set[str] = set()

    for entry in _worktree_list(root):
        path_str = str(entry.get("path") or "")
        item = {"path": path_str, "branch": str(entry.get("branch") or ""),
                "outcome": "kept", "reason": "", "would_reap": False,
                "reaped": False, "worktree_removed": False,
                "branch_deleted": False}
        if not path_str:
            continue
        path = Path(path_str)

        if entry.get("bare"):
            item["reason"] = "bare repository entry, not a task worktree"
            items.append(item)
            continue
        try:
            if path.resolve() == root_resolved:
                item["reason"] = "the main checkout"
                items.append(item)
                continue
        except OSError:
            item["reason"] = "could not resolve the path, keeping it"
            items.append(item)
            continue
        if entry.get("locked"):
            item["reason"] = "the worktree is locked"
            items.append(item)
            continue
        if not path.exists():
            item["reason"] = "directory already gone (pruned)"
            items.append(item)
            continue

        live_task_reason = _live_task_guard_reason(item["branch"])
        if live_task_reason is not None:
            item["reason"] = live_task_reason
            items.append(item)
            continue

        try:
            dirty = task_workspace._git_out(path, "status",
                                            "--porcelain").strip()
        except (RuntimeError, OSError) as exc:
            item["reason"] = f"could not read status, keeping it: {exc}"
            items.append(item)
            continue
        if dirty:
            item["reason"] = (f"uncommitted changes "
                              f"({len(dirty.splitlines())} path(s))")
            items.append(item)
            continue

        if not _older_than(path, age_h):
            item["reason"] = f"younger than {age_h}h, keeping it"
            items.append(item)
            continue

        try:
            if live_check(path):
                item["reason"] = "a process is using this worktree right now"
                items.append(item)
                continue
        except Exception:  # noqa: BLE001 - fail closed
            item["reason"] = "could not check for live use, keeping it"
            items.append(item)
            continue

        ref = item["branch"] or str(entry.get("head") or "")
        if not ref:
            item["reason"] = "no branch or HEAD to check ancestry, keeping it"
            items.append(item)
            continue
        ancestor = _is_ancestor(root, ref, upstream)
        if ancestor is None:
            item["reason"] = (f"could not compare {ref} against {upstream}, "
                              "keeping it")
            items.append(item)
            continue
        if not ancestor:
            item["reason"] = f"not yet merged into {upstream}"
            items.append(item)
            continue

        item["would_reap"] = True
        if mode == "survey":
            item["outcome"] = "pass"
            item["reason"] = "clean, old, merged; the sweep will remove this"
            items.append(item)
            continue

        removed = _remove_worktree(root, path)
        if not removed["ok"]:
            item["reason"] = removed["reason"]
            items.append(item)
            continue
        item["worktree_removed"] = True
        reaped_paths.add(path_str)

        branch_deleted = True
        if item["branch"]:
            deleted = _delete_branch(root, item["branch"])
            branch_deleted = deleted["ok"]
            if not deleted["ok"]:
                item["reason"] = f"worktree removed; {deleted['reason']}"
        item["branch_deleted"] = branch_deleted
        item["reaped"] = True
        item["outcome"] = "pass"
        if not item["reason"]:
            item["reason"] = "reaped: clean, merged, past the age window"
        items.append(item)

    _forget_paths(reaped_paths)
    try:
        task_workspace._git_out(root, "worktree", "prune")
    except (RuntimeError, OSError):
        pass

    return {
        "kind": "conductor.reap_sweep",
        "node_id": SWEEP_NODE,
        "workflow_id": "conductor",
        "outcome": "pass",
        "mode": mode,
        "considered": len(items),
        "reaped": sum(1 for i in items if i["reaped"]),
        "would_reap": sum(1 for i in items if i["would_reap"]),
        "items": items,
    }


# ---------------------------------------------------------------------------
# The periodic pass -- catches what accumulates BETWEEN lands.
# ---------------------------------------------------------------------------
#
# `ship_worker._sweep_after_land` runs `sweep_worktrees` on every successful
# land, which is enough to stop the backlog from growing again -- but the
# 173-worktree incident this was built for had gaps of hours between lands,
# during which agent/QA/fixer worktrees still landed and went stale with
# nobody's land event to piggyback on. This thread is that gap's own timer.
# Default OFF, same posture as dispatch_guard/resume_actuator/deploy_worker:
# PRISM_WORKTREE_SWEEP_INTERVAL=<seconds> opts an environment in.

SWEEP_INTERVAL_ENV = "PRISM_WORKTREE_SWEEP_INTERVAL"


def _log(msg: str) -> None:
    print(f"[worktree-sweep] {msg}", file=sys.stderr, flush=True)


def _sweep_interval_s() -> int:
    raw = os.environ.get(SWEEP_INTERVAL_ENV, "")
    try:
        return int(raw) if raw.strip() else 0
    except ValueError:
        return 0


def sweep_worktrees_once(repo_root: Optional[str] = None) -> dict:
    """One periodic pass, logged -- the daemon-wide counterpart to the
    per-land call, using the SAME `sweep_worktrees`. Deliberately allowed
    to fall back to the bare-checkout default (unlike
    `ship_worker._sweep_after_land`, which never does): this runs from a
    live daemon thread, not a test, so "the repo this process actually
    serves" is exactly the right default."""
    result = sweep_worktrees(repo_root=repo_root)
    reaped = [i for i in result.get("items", []) if i.get("reaped")]
    if reaped:
        _log(f"reaped {len(reaped)}/{result.get('considered', 0)}: " +
            ", ".join(i["path"] for i in reaped))
    return result


def _sweep_loop(interval_s: int,
                stop_event: Optional[threading.Event] = None) -> None:
    _log(f"started; interval={interval_s}s")
    while stop_event is None or not stop_event.is_set():
        try:
            sweep_worktrees_once()
        except Exception as exc:  # noqa: BLE001 - never kill the thread
            _log(f"sweep error: {exc}")
        if stop_event is not None:
            if stop_event.wait(interval_s):
                break
        else:
            _time.sleep(interval_s)


def start_worktree_sweep_worker() -> Optional[threading.Thread]:
    """Spawn the periodic sweep thread, unless disabled via
    PRISM_WORKTREE_SWEEP_INTERVAL<=0/unset (the default). Mirrors
    dispatch_guard.start_dispatch_reaper / deploy_worker.start_deploy_worker."""
    interval = _sweep_interval_s()
    if interval <= 0:
        _log(f"disabled (default OFF; set {SWEEP_INTERVAL_ENV}=<seconds> to "
            "opt this environment in)")
        return None
    t = threading.Thread(target=_sweep_loop, args=(interval,),
                         name="prism-worktree-sweep", daemon=True)
    t.start()
    return t
