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


def task_pin(task_id: str = "") -> dict:
    """THE canonical pin for a task's gate evidence — the ONE resolution path
    shared by the MINT (oracle_spec.run_oracle's stamp fallback) and the CHECK
    (_oracle_receipt_refusal), so a receipt minted under the current pin is
    fresh by construction. Workspace-anchored: both sides resolve through
    pinned_policy with the task's OWN workspace descriptor (baseline +
    repo_root), never a caller-supplied ctx — the drift that made freshly
    minted receipts read as stale (task 68e5c699)."""
    ws = _workspace_for(task_id) or {}
    ctx = {k: ws[k] for k in ("baseline", "repo_root") if ws.get(k)}
    return pinned_policy(task_id, ctx)


def dirty_policy_files_in_daemon_checkout() -> list[str]:
    """POLICY_FILES with UNCOMMITTED changes (staged or working-tree, vs HEAD)
    in the DAEMON'S OWN checkout — the tree the running process actually
    imported its modules from (``_prism_repo_root()``), NEVER a task's
    worktree (never ``_resolve_repo_root``/ctx/ws). A dirty policy file here
    means the running judge cannot prove it is executing the code it claims
    to be pinned to (task 69233ca0: a lane once wrote uncommitted edits to
    conductor_service.py directly into this tree). Empty when the checkout is
    clean, has no git, or is unresolvable — fails OPEN so an environment
    without git does not blanket-refuse every gate."""
    root = _prism_repo_root()
    if root is None:
        return []
    out = _git(root, "status", "--porcelain", "--", *POLICY_FILES)
    if out is None:
        return []
    changed: set[str] = set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:  # rename entries: "old -> new"
            path = path.split(" -> ", 1)[1].strip()
        path = path.strip('"')
        if is_policy_file(path):
            changed.add(_norm(path))
    return sorted(changed)


def dirty_judge_reason() -> str:
    """Refusal reason when the DAEMON'S OWN checkout has uncommitted edits to
    a gate-policy file, else "". UNCONDITIONAL — no PRISM_POLICY_CHANGE_APPROVED
    or policy-change/control-plane task-tag escape, because a compromised
    RUNTIME is not remedied by authorizing a TASK. Named so both a driving
    agent and a human can act: commit or discard the edit."""
    dirty = dirty_policy_files_in_daemon_checkout()
    if not dirty:
        return ""
    return ("judge integrity unprovable: the daemon's own checkout has "
            "uncommitted changes to gate policy (" + ", ".join(dirty) + "); "
            "the running process cannot prove it is executing the pinned "
            "code — commit or discard these edits before any gate is decided")


def _first_parent_touched_files(workspace: Path, baseline: str) -> Optional[set[str]]:
    """Repo-relative paths the candidate's OWN first-parent commit lineage
    between ``baseline`` and HEAD actually altered -- as distinct from files
    that only differ because the candidate merged in an integration branch
    (e.g. ``dev``) carrying someone ELSE'S already-authorized change on the
    merge's second-parent side.

    Uses ``--first-parent`` WITH a combined-diff format (``--cc``, this git's
    only supported spelling of it -- ``--diff-merges=combined`` needs git
    >=2.36) rather than plain ``--name-only``: plain first-parent logging
    shows a merge commit's FULL diff against its first parent, which flags a
    file the candidate never touched merely because the merge changed it (a
    clean `git merge` that took the incoming side wholesale). The combined
    format instead shows a merge commit's file ONLY when its resolved content
    differs from EVERY parent -- i.e. a real conflict resolution the
    candidate authored, or content nobody upstream already had. Verified
    empirically (not just recalled) against git 2.34: a clean merge inheriting
    an unrelated policy-file change reports NO files for that merge commit;
    a candidate-authored conflict resolution still does; a same-commit direct
    edit (independent of how any later merge resolves) is still caught by its
    own non-merge commit regardless.

    None when this can't be computed (caller then falls back to the wider,
    more conservative check -- fails toward MORE refusals, never fewer).
    COMMITTED history only -- uncommitted worktree edits are not commits and
    cannot appear in `git log`; the caller handles those separately and
    UNCONDITIONALLY, since an uncommitted edit sitting in the worktree right
    now cannot have arrived via a merge someone else authored."""
    try:
        out = subprocess.run(
            ["git", "log", "--first-parent", "--cc", "--name-only",
             "--format=", f"{baseline}..HEAD"],
            cwd=str(workspace), capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if out.returncode != 0:
        return None
    return {ln.strip() for ln in out.stdout.splitlines() if ln.strip()}


def _uncommitted_files(workspace: Path) -> Optional[set[str]]:
    """Repo-relative paths that differ between the worktree's current state
    (working tree + index) and its own HEAD -- i.e. genuinely uncommitted
    edits, tracked or untracked. These can never be "merely inherited via a
    merge" (a merge only ever changes committed history), so the caller
    always flags them regardless of first-parent scoping. None on any git
    failure -- caller then treats it as "cannot rule out", not "nothing
    uncommitted"."""
    try:
        tracked = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            cwd=str(workspace), capture_output=True, text=True, timeout=15,
        )
        untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(workspace), capture_output=True, text=True, timeout=15,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if tracked.returncode != 0 or untracked.returncode != 0:
        return None
    files = tracked.stdout.splitlines() + untracked.stdout.splitlines()
    return {ln.strip() for ln in files if ln.strip()}


def candidate_policy_edits(task_id: str) -> list[str]:
    """The gate-policy files the task's WORKTREE has modified vs its baseline
    (committed OR working-tree edits). Empty when the task has no worktree or
    touched no policy file. Reuses the verifier's diff scoping so the same
    'changed since baseline' semantics apply.

    A task's branch merging the integration branch (dev) forward -- to stay
    shippable across a long-running slice, or to resolve conflicts before
    landing -- can bring in ANOTHER task's own, separately-authorized policy
    change on the merge's second-parent side. A blind `git diff baseline
    HEAD` cannot tell that apart from the candidate authoring the edit
    itself, and refuses the gate on an edit this task never made (task
    e14680ba, 2026-08-26: a merge that absorbed conductor_service.py/
    arc_governance.py changes from unrelated, already-shipped tasks tripped
    this exact false positive). Narrow the COMMITTED portion of the raw diff
    down to files the candidate's OWN first-parent lineage actually touched
    (see ``_first_parent_touched_files``) -- but a policy file with a genuine
    UNCOMMITTED edit right now stays flagged unconditionally, since that
    cannot have arrived via someone else's merge. When first-parent scoping
    can't be computed, fall back to the wider (pre-existing, more
    conservative) set so an environment where it's unresolvable never gets
    MORE permissive than before."""
    ws = _workspace_for(task_id)
    if not ws:
        return []
    baseline = ws.get("baseline") or None
    try:
        from prism_service.services.verifier_service import _git_changed_files
        changed = _git_changed_files(Path(ws["path"]), baseline)
    except Exception:
        changed = []
    flagged = {_norm(f) for f in changed if is_policy_file(f)}
    if not flagged or not baseline:
        return sorted(flagged)
    uncommitted = _uncommitted_files(Path(ws["path"]))
    if uncommitted is None:
        return sorted(flagged)
    uncommitted_norm = {_norm(f) for f in uncommitted}
    always_flagged = flagged & uncommitted_norm
    committed_only = flagged - uncommitted_norm
    if not committed_only:
        return sorted(always_flagged)
    own = _first_parent_touched_files(Path(ws["path"]), baseline)
    if own is None:
        return sorted(flagged)
    return sorted(always_flagged | (committed_only & {_norm(f) for f in own}))


def policy_change_authorized(task: object = None) -> bool:
    """True when a policy change is EXPLICITLY authorized to route through the
    control-plane: env ``PRISM_POLICY_CHANGE_APPROVED`` truthy, or the task
    carries a ``policy-change`` / ``control-plane`` tag."""
    if os.environ.get("PRISM_POLICY_CHANGE_APPROVED", "").strip().lower() in _TRUE:
        return True
    tags = {str(t).strip().lower() for t in (getattr(task, "tags", None) or [])}
    return bool(tags & _POLICY_CHANGE_TAGS)


def candidate_policy_edit_reason(task: object) -> str:
    """REFUSAL reason when the candidate's OWN worktree edits its own gate
    policy, else "". The ORIGINAL tooth (inverted-flow #3), UNCHANGED,
    byte-for-byte, since before task 69233ca0's follow-up split it off the
    combined check: a gate cannot be cleared on a branch that modifies the
    rubric/ignore-list/oracle thresholds it is judged by. An authorized
    policy-change path (env flag / task tag) is exempt. This is a real
    security boundary (a task loosening its own judge) and gate_decide's
    approve path keeps failing on it UNCONDITIONALLY, override or not — see
    conductor_service.py's candidate-controls-judge block."""
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


def candidate_controls_judge_reason(task: object) -> str:
    """REFUSAL reason when EITHER (a) the daemon's own checkout has
    uncommitted edits to a gate-policy file — the judge's independence is
    unprovable, task 69233ca0 — or (b) the candidate's worktree edits its own
    gate policy (candidate_policy_edit_reason), else "". Checked FIRST and
    unconditionally: (a) is a runtime integrity failure, not a task
    authorization question, so it has no policy-change escape.

    This COMBINED check is what every MACHINE adjudication seat in
    conductor_service.py pre-flights (adjudicate_green_gate et al.) and
    abstains (leaves the gate pending) on ANY truthy reason — correct for
    both (a) and (b), since a machine should never decide on an unprovable
    judge OR a self-editing candidate.

    gate_decide's HUMAN approve path does NOT call this combined function —
    it calls (b) alone (candidate_policy_edit_reason, unconditional hard-fail
    preserved) and treats (a) separately as a non-blocking CAVEAT a human may
    approve past, since a human who can see the dirty-judge warning is
    exactly the escape hatch the doctrine preserves (owner 2026-07-15/16:
    the human keeps visibility + override, never a required click) — see
    conductor_service.py's gate_decide, "DIRTY JUDGE CAVEAT" block."""
    dirty_judge = dirty_judge_reason()
    if dirty_judge:
        return dirty_judge
    return candidate_policy_edit_reason(task)
