"""Pinned control-plane for gate policy (inverted-flow soundness #3).

THE DEFECT: the candidate under test supplied its OWN judge. The branch a
gate adjudicates contains the gate policy — governance rubrics, the arc-
governance scoring code, the verifier ignore/lane rules, the oracle spec /
thresholds, and the conductor gate logic. A task could loosen its own rubric
or ignore-list to pass. This module pins the judge:

  1. GATE POLICY IS A FIXED SET of files (``POLICY_FILES``): the rubrics YAML,
     arc_governance, verifier_service, oracle_spec, conductor_service, and
     THIS loader. Policy is loaded from a PINNED control ref (``git show
     <ref>:<path>`` against the MAIN checkout), never from the candidate's
     worktree — the default ref is the task worktree's BASELINE commit (the
     code as it was BEFORE the candidate started, which it cannot have edited),
     overridable via ``PRISM_CONTROL_REF``. ``policy_hash`` is a stable content
     hash over the pinned set so every pass is attributable to a specific
     policy.
  2. CANDIDATE-CONTROLS-JUDGE REFUSAL: when a task's worktree DIFF (branch vs
     baseline) modifies ANY policy file, the gate REFUSES — the candidate is
     editing its own judge. A legitimate policy change routes through an
     explicitly-authorized path (``PRISM_POLICY_CHANGE_APPROVED`` or a task
     tagged ``policy-change`` / ``control-plane``), never silently from inside
     the graded task.

The tooth is deliberately independent of WHERE the running daemon loads its
modules from: even though the daemon scores with its own installed source
(not the worktree), a branch that edits the policy would become the judge once
merged — so it must not be able to green-gate itself first.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Optional

CONTROL_PLANE_VERSION = "control-plane/1"

# Repo-relative (POSIX) paths of the files that CONSTITUTE gate policy. A
# candidate's worktree diff touching any of these is editing its own judge.
POLICY_FILES: tuple[str, ...] = (
    "services/prism-service/prism_service/services/governance_rubrics.yaml",
    "services/prism-service/prism_service/services/arc_governance.py",
    "services/prism-service/prism_service/services/verifier_service.py",
    "services/prism-service/prism_service/services/oracle_spec.py",
    "services/prism-service/prism_service/services/conductor_service.py",
    "services/prism-service/prism_service/services/control_plane.py",
)

# Tags that mark a task as an AUTHORIZED policy change (routed through the
# control-plane, not smuggled inside a graded feature task).
_POLICY_CHANGE_TAGS = {"policy-change", "policy_change", "control-plane",
                       "control_plane"}

_TRUE = {"1", "true", "yes", "on"}


def _norm(p: str) -> str:
    """Normalise a path to POSIX + strip a leading ./ so comparisons are
    stable across git (forward slashes) and Windows worktree paths."""
    return str(p or "").replace("\\", "/").strip().lstrip("./")


def is_policy_file(path: str) -> bool:
    """True iff ``path`` is one of the enumerated gate-policy files. Matches on
    the repo-relative suffix so both bare repo-relative paths (what git diff
    emits) and longer absolute paths resolve."""
    n = _norm(path)
    if not n:
        return False
    for pf in POLICY_FILES:
        if n == pf or n.endswith("/" + pf):
            return True
    return False


def _git(cwd, *args, timeout: float = 20.0) -> Optional[str]:
    """Run git in ``cwd`` read-only; return stdout, or None on any failure
    (missing ref/path, no git, timeout). Never raises — a None result means
    'could not pin', which callers treat as an unresolved (empty) hash."""
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                           text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout


def _prism_repo_root() -> Optional[Path]:
    """The enclosing PRISM checkout (the MAIN control plane), or None. Ascends
    from this module until a directory holding ``.git`` is found."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    return None


def _resolve_repo_root(ctx: dict, ws: Optional[dict]) -> Optional[Path]:
    if ctx.get("repo_root"):
        return Path(ctx["repo_root"])
    if ws and ws.get("repo_root"):
        return Path(ws["repo_root"])
    return _prism_repo_root()


def _workspace_for(task_id: str) -> Optional[dict]:
    if not task_id:
        return None
    try:
        from prism_service.services import task_workspace
        return task_workspace.workspace_for(task_id)
    except Exception:
        return None


def resolve_control_ref(task_id: str = "",
                        ctx: Optional[dict] = None) -> tuple[str, Optional[Path]]:
    """Resolve (control_ref, repo_root) for pinning gate policy.

    Precedence for the ref: explicit ``ctx['control_ref']`` > env
    ``PRISM_CONTROL_REF`` > the task worktree's BASELINE commit (the pin the
    candidate cannot have edited) > merge-base(origin/main, HEAD) of the main
    checkout > HEAD. The repo_root is always the MAIN checkout (or ``ctx`` /
    workspace override), NEVER the candidate's worktree — so ``git show`` reads
    pinned mainline policy, not the candidate's edits."""
    ctx = dict(ctx or {})
    ws = _workspace_for(task_id)
    repo_root = _resolve_repo_root(ctx, ws)
    ref = (ctx.get("control_ref") or os.environ.get("PRISM_CONTROL_REF")
           or ctx.get("baseline") or (ws or {}).get("baseline") or "")
    ref = str(ref).strip()
    if not ref and repo_root is not None:
        mb = _git(repo_root, "merge-base", "origin/main", "HEAD")
        ref = (mb.strip().splitlines()[0] if mb and mb.strip() else "HEAD")
    return ref, repo_root


def load_pinned_text(path: str, control_ref: str,
                     repo_root: Optional[Path]) -> Optional[str]:
    """The PINNED content of a policy file at ``control_ref`` (``git show
    <ref>:<path>`` from the main checkout). None when unresolvable. IGNORES the
    candidate's worktree entirely — this is the whole point."""
    if not control_ref or repo_root is None:
        return None
    return _git(repo_root, "show", f"{control_ref}:{_norm(path)}")


def load_pinned_rubrics(task_id: str = "",
                        ctx: Optional[dict] = None) -> Optional[dict]:
    """Load governance_rubrics.yaml from the PINNED control ref (not the
    worktree). Returns the parsed rubric dict, or None when the pin cannot be
    resolved (caller falls back to the daemon's own packaged rubrics)."""
    ref, repo_root = resolve_control_ref(task_id, ctx)
    rubrics_rel = POLICY_FILES[0]
    text = load_pinned_text(rubrics_rel, ref, repo_root)
    if text is None:
        return None
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except Exception:
        return None


def policy_hash(control_ref: str, repo_root: Optional[Path]) -> str:
    """Stable content hash over the PINNED policy set. "" when nothing in the
    set resolves at ``control_ref`` (an unpinnable environment — the callers
    then skip the policy-drift tooth rather than false-refuse)."""
    if not control_ref or repo_root is None:
        return ""
    payload: dict[str, str] = {}
    for pf in POLICY_FILES:
        text = load_pinned_text(pf, control_ref, repo_root)
        if text is not None:
            payload[pf] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if not payload:
        return ""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def pinned_policy(task_id: str = "", ctx: Optional[dict] = None) -> dict:
    """The pin descriptor stamped onto evidence + gate decisions:
    {control_ref, policy_hash, policy_version, repo_root}."""
    ref, repo_root = resolve_control_ref(task_id, ctx)
    return {
        "control_ref": ref,
        "policy_hash": policy_hash(ref, repo_root),
        "policy_version": CONTROL_PLANE_VERSION,
        "repo_root": str(repo_root) if repo_root else "",
    }


def candidate_policy_edits(task_id: str) -> list[str]:
    """The gate-policy files the task's WORKTREE has modified vs its baseline
    (committed OR working-tree edits). Empty when the task has no worktree or
    touched no policy file. Reuses the verifier's diff scoping so the same
    'changed since baseline' semantics apply."""
    ws = _workspace_for(task_id)
    if not ws:
        return []
    try:
        from prism_service.services.verifier_service import _git_changed_files
        changed = _git_changed_files(Path(ws["path"]),
                                     ws.get("baseline") or None)
    except Exception:
        changed = []
    return sorted({_norm(f) for f in changed if is_policy_file(f)})


def policy_change_authorized(task: object = None) -> bool:
    """True when a policy change is EXPLICITLY authorized to route through the
    control-plane: env ``PRISM_POLICY_CHANGE_APPROVED`` truthy, or the task
    carries a ``policy-change`` / ``control-plane`` tag."""
    if os.environ.get("PRISM_POLICY_CHANGE_APPROVED", "").strip().lower() in _TRUE:
        return True
    tags = {str(t).strip().lower() for t in (getattr(task, "tags", None) or [])}
    return bool(tags & _POLICY_CHANGE_TAGS)


def candidate_controls_judge_reason(task: object) -> str:
    """REFUSAL reason when the candidate's worktree edits its own gate policy,
    else "". The core tooth: a gate cannot be cleared on a branch that modifies
    the rubric / ignore-list / oracle thresholds it is judged by. An authorized
    policy-change path (env flag / task tag) is exempt."""
    task_id = str(getattr(task, "id", "") or "")
    edits = candidate_policy_edits(task_id)
    if not edits:
        return ""
    if policy_change_authorized(task):
        return ""
    return ("candidate modified gate policy: " + ", ".join(edits)
            + "; policy must change through the control-plane, not the task "
            "under test — route an authorized policy change via "
            "PRISM_POLICY_CHANGE_APPROVED=1 or a 'policy-change'-tagged task")
