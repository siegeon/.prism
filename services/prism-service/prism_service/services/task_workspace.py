"""Per-task git WORKTREE for the HONEST loop (task e825e00a).

Each task that enters the flow gets its OWN git worktree of the PRISM
repo — a real branch off the task's base, an actual checkout of the
product source. The worker commits REAL work there (a real failing test
at red, a real implementation at green) and the conductor verifier runs
tier0 against THIS checkout, so a gate passes only on genuinely committed,
genuinely running tests. A fabricated trace with no committed test yields
zero tier0 claims and is refused.

This REPLACES the earlier empty-stub scratch repo (a bare pyproject+README
git init), which verified nothing real and — worse — let flow_start fall
back to the SHARED branch when it failed, cross-contaminating tasks. The
worktree model FAILS CLOSED: if a real worktree cannot be created, we
raise so flow_start refuses to start rather than sharing a branch.

Worktrees live under data_dir/task_workspaces/<task_id> and are registered
in the PRISM repo's worktree list; remove_workspace() unregisters them.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Optional

from prism_service.data_dir import resolve_data_dir

# Relative to a checkout root: where the SPA's JS deps live, and where its
# build output lands. Both are gitignored, so a fresh worktree has neither.
_WEB_DIR = Path("services") / "prism-service" / "prism_service" / "web"
_NODE_MODULES = _WEB_DIR / "node_modules"


def _root() -> Path:
    p = resolve_data_dir() / "task_workspaces"
    p.mkdir(parents=True, exist_ok=True)
    return p


def workspace_path(task_id: str) -> str:
    """This task's existing workspace checkout, or "" when it has none.

    RESOLVE ONLY -- never create. Callers are gate checks and receipt
    freshness reads (green_rewind._workspace_path, and through it every
    green_gate decision), so materialising a checkout here would hand a
    workspace to tasks that never had one.

    Task 4cbac65a: this function's ABSENCE was a silent production defect.
    `green_rewind._workspace_path` did a `getattr(task_workspace,
    "workspace_path", None)` fallback for any task row whose own
    `workspace` field was empty -- which is most of them. getattr returned
    None, the helper returned "", and `oracle_spec.current_tree_sha("")`
    returns "". Measured live across ten tasks parked at green_gate: every
    one reported an empty tree while its directory sat on disk. A PASSING
    receipt therefore could never be confirmed fresh (the gate refused it
    as unshipped), and `maybe_rewind` bailed at its `if not tree` guard, so
    the red-receipt rewind shipped as task ad92c0e9 never fired once.

    The index is consulted first because a workspace may live outside the
    default root (an explicit repo_root at creation); the conventional
    path is the fallback, and both must EXIST to be returned.
    """
    tid = str(task_id or "").strip()
    if not tid:
        return ""
    try:
        rec = _load_index().get(tid) or {}
        recorded = str(rec.get("path") or "")
        if recorded and Path(recorded).is_dir():
            return recorded
    except Exception:  # noqa: BLE001 - the conventional path below still answers
        pass
    try:
        p = _root() / tid
        return str(p) if p.is_dir() else ""
    except Exception:  # noqa: BLE001 - a resolver never raises into a gate
        return ""


def _index_path() -> Path:
    return _root() / "index.json"


def _load_index() -> dict:
    p = _index_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_index(idx: dict) -> None:
    _index_path().write_text(json.dumps(idx, indent=2))


def _git_out(cwd: Path, *args: str) -> str:
    """Run git in `cwd`, raising RuntimeError (fail closed) on failure."""
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), check=True,
                           capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"git not available: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: "
            f"{(exc.stderr or exc.stdout or '').strip()}") from exc
    return r.stdout.strip()


def _prism_repo_root() -> Path:
    """Ascend from this module until a directory holding `.git` is found —
    the enclosing PRISM checkout. Raise (fail closed) if none is found so a
    packaged/wheel install without a source tree cannot silently degrade to
    a shared branch."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    raise RuntimeError(
        "no enclosing git checkout for the PRISM repo; refusing to create a "
        "fake workspace (fail closed)")


def _create_junction(link: Path, target: Path) -> None:
    """Create a directory LINK that needs no elevated privileges: a Windows
    directory junction (a reparse point — distinct from a symlink, which
    Windows normally restricts) or, on POSIX, a plain symlink. Raises (fail
    closed) if the platform cannot make one, rather than leaving a
    worktree that looks fine and silently cannot resolve its deps.

    NOTE: a naive `mklink /J` from a bash/sh subprocess on this host has
    produced a SILENTLY BROKEN junction (a malformed double-drive-letter
    target) that LOOKS created but that npm cannot walk — that failure
    mode is why this goes through the win32 API directly instead of
    shelling out to mklink.
    """
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(str(target), str(link), target_is_directory=True)


def _link_web_node_modules(ws: Path, root: Path) -> None:
    """LINK (never copy) the main checkout's web `node_modules` into a
    task worktree. `node_modules` is gitignored, so a fresh worktree
    checkout has none, and any UI slice's real typecheck (`npm run build`;
    `tsc --noEmit` is a known false green in this repo) fails immediately
    with "'tsc' is not recognized". Copying is wrong: node_modules is
    large, worktrees are per-task, and the deps are already pinned by
    package-lock.json at the same commit — a link is exact and O(1).

    Only `node_modules` is linked, never `web_dist` (the SPA build
    output): they are siblings under the same web dir, and leaving
    `web_dist` alone means a worktree's `npm run build` always writes its
    OWN build output, never the main checkout's — a task build can never
    mutate the bundle the running dev daemon serves.

    Skips quietly if the main checkout has no `node_modules` yet (a fresh
    clone before its first `npm install`): that is a setup gap unrelated
    to worktree creation, not a reason to fail the task's workspace. Also
    skips (no-op) if the worktree already has a `node_modules` — idempotent
    for the "worktree already exists" fast path in ensure_workspace.
    """
    src = root / _NODE_MODULES
    if not src.exists():
        return
    dest = ws / _NODE_MODULES
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    _create_junction(dest, src)


_NODE_MODULES_LINK_NORM = str(_NODE_MODULES).replace("\\", "/")


def is_injected_node_modules_link(path: str) -> bool:
    """True when `path` (workspace-root-relative) IS the node_modules link
    `_link_web_node_modules` injects into every task worktree — never a
    change the worker made. Used by the worker-contract check (task
    93cd2cf9) so PRISM's own injected dependency link is never blamed on
    the worker: a `.gitignore` pattern with a trailing slash matches only
    directories, never a symlink (git-scm.com/docs/gitignore), so on a
    Linux runner `git status --porcelain` reports this link itself as
    untracked even after the trailing-slash fix in .gitignore/web/.gitignore
    — this is the structural, git-independent backstop for that gap."""
    return str(path or "").replace("\\", "/").strip("/") == _NODE_MODULES_LINK_NORM


def _is_link(path: Path) -> bool:
    """True if `path` is a directory LINK (a Windows junction/symlink
    reparse point, or a POSIX symlink) rather than a real directory.

    Deliberately does NOT rely on Path.is_symlink()/os.path.islink() alone
    on Windows: verified empirically (task 0384b04d) that a junction made
    by `_create_junction` (IO_REPARSE_TAG_MOUNT_POINT) reports
    is_symlink() == False on this Python/Windows combo — that check only
    recognizes IO_REPARSE_TAG_SYMLINK. Checking the FILE_ATTRIBUTE_REPARSE_POINT
    bit directly is what actually catches it.
    """
    try:
        if sys.platform == "win32":
            st = os.stat(str(path), follow_symlinks=False)
            return bool(st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return path.is_symlink()
    except OSError:
        return False


def _detach_link(path: Path) -> None:
    """Remove a directory LINK without touching what it points to. This is
    a safety precondition, not optional cleanup: a naive recursive delete
    of a worktree containing our node_modules junction (`git worktree
    remove --force` is one; so is a bare shutil.rmtree/rm -rf) can FOLLOW
    the reparse point on Windows and delete the TARGET's contents instead
    of just the link — confirmed empirically, it destroyed the main
    checkout's real node_modules/.bin (96 shims) during verification of
    this module's fix (task 0384b04d). `os.rmdir()` on a reparse point
    removes only the reparse point itself; the target directory and its
    contents are left untouched — also confirmed empirically.
    """
    if not _is_link(path):
        return
    if sys.platform == "win32":
        os.rmdir(str(path))
    else:
        os.unlink(str(path))


def ensure_workspace(task_id: str, repo_root: Optional[str] = None,
                     base_ref: Optional[str] = None) -> dict:
    """Create (idempotently) a real git worktree of the PRISM repo for this
    task and record {path, baseline, branch, repo_root} in the index.

    The worktree is a checkout of the PRODUCT source on a fresh branch off
    `base_ref` (default: the repo's current HEAD). `baseline` is that base
    commit, so the worker's later commits are the scoped work tier0 diffs
    against.

    FAIL CLOSED: any git failure (no repo, worktree add error) raises —
    callers must NOT fall back to a shared branch.
    """
    idx = _load_index()
    rec = idx.get(task_id)
    if rec and Path(rec["path"]).exists():
        # Self-heal: a worktree created before this fix, or before the main
        # checkout's first `npm install`, may still be missing its deps
        # link. Idempotent no-op once the link exists.
        _link_web_node_modules(Path(rec["path"]),
                                Path(rec.get("repo_root") or _prism_repo_root()))
        # Fail-soft: advance to the lane's own branch tip if it moved
        # elsewhere (mx-399f3e / mx-2aff62). Never strand this fast path
        # on a sync hiccup.
        try:
            sync_workspace(task_id)
        except Exception:
            pass
        return rec

    root = Path(repo_root) if repo_root else _prism_repo_root()
    if not (root / ".git").exists():
        raise RuntimeError(f"{root} is not a git checkout; refusing to create "
                           "a fake workspace (fail closed)")
    base = base_ref or _git_out(root, "rev-parse", "HEAD")

    ws = _root() / task_id
    branch = f"prism/ws/{task_id}"
    # Reap any orphaned registration from a prior partial run so a fresh id
    # is not blocked; harmless when there is nothing to prune.
    try:
        _git_out(root, "worktree", "prune")
    except RuntimeError:
        pass
    # git creates `ws`; adding -b makes the task's branch off `base`. A
    # failure here raises straight out of _git_out (fail closed).
    _git_out(root, "worktree", "add", "-b", branch, str(ws), base)
    _git_out(ws, "config", "user.email", "worker@prism")
    _git_out(ws, "config", "user.name", "prism-worker")
    # JS deps are gitignored, so the fresh checkout has none; link them in
    # so a UI slice's real typecheck (`npm run build`) works unaided.
    _link_web_node_modules(ws, root)

    rec = {"task_id": task_id, "path": str(ws), "baseline": _git_out(
        ws, "rev-parse", "HEAD"), "branch": branch, "repo_root": str(root)}
    idx[task_id] = rec
    _save_index(idx)
    return rec


def remove_workspace(task_id: str) -> dict:
    """Unregister and delete a task's worktree + its branch (best effort).
    Used on task teardown and by tests so the PRISM repo's worktree list
    stays clean.

    SAFETY: detaches our node_modules link BEFORE any recursive delete of
    the worktree tree. `git worktree remove --force` deletes the worktree
    directory recursively, and on Windows that recursion FOLLOWS our
    node_modules junction into the main checkout rather than stopping at
    the link — see _detach_link. Skipping this step is not merely a
    regression risk, it already destroyed a real node_modules/.bin once.
    """
    idx = _load_index()
    rec = idx.pop(task_id, None)
    if rec is None:
        return {"ok": False, "reason": "no workspace for task"}
    ws_path = Path(rec.get("path") or "")
    try:
        _detach_link(ws_path / _NODE_MODULES)
    except OSError:
        pass
    root = Path(rec.get("repo_root") or "")
    if root.exists():
        for args in (("worktree", "remove", "--force", rec["path"]),
                     ("branch", "-D", rec.get("branch", "")),
                     ("worktree", "prune")):
            if not args[-1]:
                continue
            try:
                _git_out(root, *args)
            except RuntimeError:
                pass
    _save_index(idx)
    return {"ok": True, "removed": rec}


def _unmerged_commits(ws: Path, base: str) -> list[str]:
    """Commits on the worktree's HEAD with NO equivalent on `base`.

    `git cherry` compares by PATCH, not by sha, so a change that reached the
    base via cherry-pick or squash counts as landed and does not block a
    reap. Lines starting '+' are the ones that exist nowhere upstream.
    """
    out = _git_out(ws, "cherry", base, "HEAD")
    return [ln for ln in out.splitlines() if ln.startswith("+")]


def reap_if_settled(task_id: str, base: Optional[str] = None) -> dict:
    """Delete a finished task's worktree, but ONLY when nothing is lost.

    remove_workspace() runs `git branch -D`, so reaping a worktree that
    still holds unique commits DESTROYS them - and the task is already
    marked done, so nobody would look again. Measured 2026-08-02: of 91
    worktrees left behind by finished tasks, 16 held commits absent from
    origin/main and 3 held uncommitted changes. This guard is the reason
    wiring the reaper is safe at all.

    FAILS CLOSED: any error probing the worktree keeps it. A leaked
    directory costs disk; a wrongly-reaped one costs work.
    """
    rec = workspace_for(task_id)
    if rec is None:
        return {"reaped": False, "reason": "no workspace on file for task"}
    ws = Path(rec.get("path") or "")
    try:
        if base is None:
            # Resolve the base in the MAIN checkout, never in the worktree.
            # A literal "HEAD" would resolve to the worktree's OWN tip, so
            # `git cherry HEAD HEAD` is always empty and every worktree
            # would look settled - the guard would pass on exactly the
            # unmerged work it exists to protect.
            base = _git_out(Path(rec.get("repo_root") or ""),
                            "rev-parse", "HEAD")
        if _git_out(ws, "status", "--porcelain").strip():
            return {"reaped": False,
                    "reason": "uncommitted changes in the worktree",
                    "path": str(ws)}
        ahead = _unmerged_commits(ws, base)
        if ahead:
            return {"reaped": False,
                    "reason": (f"{len(ahead)} commit(s) not on {base} - "
                               "reaping would delete the branch carrying them"),
                    "path": str(ws)}
    except (RuntimeError, OSError) as exc:
        return {"reaped": False,
                "reason": f"could not verify the worktree, keeping it: {exc}",
                "path": str(ws)}
    res = remove_workspace(task_id)
    return {"reaped": bool(res.get("ok")), "reason": res.get("reason", ""),
            "path": str(ws)}


def _sync_ws(ws: Path, branch: str) -> dict:
    """The actual probe/advance, isolated so `sync_workspace` can wrap it
    in one fail-closed try/except. Raises RuntimeError/OSError on any git
    failure - the caller catches those."""
    status = _git_out(ws, "status", "--porcelain")
    if status.strip():
        changed = status.strip().splitlines()[:5]
        return {"state": "dirty",
                "reason": f"uncommitted changes in {ws}: {changed}"}
    head = _git_out(ws, "rev-parse", "HEAD")
    tip = _git_out(ws, "rev-parse", branch)
    if head == tip:
        return {"state": "already_current", "head": head}
    base = _git_out(ws, "merge-base", head, tip)
    if base == tip:
        # HEAD already contains the branch tip (worktree is ahead) -
        # nothing to sync, and NOT a divergence.
        return {"state": "already_current", "head": head}
    if base != head:
        return {"state": "diverged",
                "reason": f"HEAD {head} and branch {branch} tip {tip} "
                          "have each diverged from the other"}
    # merge_base == head: a pure fast-forward along the task's OWN branch.
    _git_out(ws, "checkout", branch)
    return {"state": "advanced", "head": _git_out(ws, "rev-parse", "HEAD")}


def sync_workspace(task_id: str) -> dict:
    """Advance a task's worktree HEAD to its OWN branch tip, fail-soft.

    Follows ONLY the task's recorded `prism/ws/<task_id>` branch - never
    main, never another task's branch - so a receipt minted against this
    worktree measures the lane's own committed work, not whatever tree
    happened to be checked out at creation time (mx-399f3e / mx-2aff62,
    task 5a6837a0).

    Returns {"state": ..., "reason"?: ..., "head"?: ...}:
      no_record        - nothing registered for this task_id.
      dirty            - uncommitted changes; nothing touched, reason
                          names them.
      already_current  - HEAD already at (or already ahead of) the tip.
      advanced         - HEAD fast-forwarded to the branch tip.
      diverged         - HEAD and the branch tip each carry commits the
                          other lacks; refused, reason names both shas.
      error            - a git call failed; reason carries the message.

    Never raises: any git/OS failure is caught and reported as
    state="error" so callers (workspace_for, ensure_workspace) stay
    fail-soft and never strand on a sync hiccup.
    """
    rec = _load_index().get(task_id)
    if rec is None or not Path(rec["path"]).exists():
        return {"state": "no_record"}
    ws = Path(rec["path"])
    branch = rec.get("branch") or f"prism/ws/{task_id}"
    try:
        return _sync_ws(ws, branch)
    except (RuntimeError, OSError) as exc:
        return {"state": "error", "reason": str(exc)}


def workspace_for(task_id: str) -> Optional[dict]:
    rec = _load_index().get(task_id)
    if rec and Path(rec["path"]).exists():
        try:
            sync_workspace(task_id)
        except Exception:
            pass
        return rec
    return None


def workspace_record(task_id: str) -> Optional[dict]:
    """The stored workspace record WITHOUT the sync side effect.

    workspace_for() fast-forwards the worktree on every call (v7.10.33) —
    correct for callers about to read the tree, ruinous for read-only sweeps
    over many tasks (the Dashboard stranded scan calls this once per done
    task; 193 syncs measured ~60s on Windows, 2026-08-13)."""
    rec = _load_index().get(task_id)
    if rec and Path(rec["path"]).exists():
        return rec
    return None
