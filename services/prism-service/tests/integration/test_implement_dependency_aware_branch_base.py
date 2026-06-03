"""RED scaffold — implement.js dependency-aware branch base (task d47c5f11).

The conductor SDLC workflow (implement.js) currently cuts every feature
branch off bare origin/main with ZERO awareness of the task's depends_on.
When a task depends on done-but-UNMERGED work (the dep's
[conductor:<dep8>] commit lives only on some feature branch, never an
ancestor of origin/main), the working tree lacks that substrate, so the
run hard-halts ~15 min later at write_failing_tests (qa correctly
refuses to write false-red tests against a missing import — see memory
mx-a56419: feat/event-bus-handlers-phase3 cut off bare main while
event_pool.py lived only on an unmerged branch).

This task makes the branch base dependency-aware:
- Locate reads depends_on BEFORE creating the branch.
- Dep already reachable from origin/main -> base stays origin/main.
- Dep DONE but unmerged yet present on some branch (git log --all hit)
  -> new branch is based off that dep's CONTAINING branch.
- The chosen base is never a branch that is BEHIND origin/main.
- Dep NOT done -> the task is BLOCKED, the workflow does not drive.
- No deps / all deps on origin/main -> fresh off origin/main (unchanged).
- Pre-flight gains a dependency-PRESENCE assertion that halts in ~5s
  with one actionable line if a done dep's commit is unreachable from
  the chosen base AND no branch carries it ('merge it first').

Source-structure asserts (mirror test_implement_agent_run_emitter.py and
test_task_session_association.py): read the workflow file and assert the
dependency-aware base-resolution wiring substrings are present. The JS the
harness runs is the USER-FACING seam — a backend that "knows" about deps
but a workflow that still cuts off bare main is a false-green (the exact
~15-min hard-halt the task removes).

ALL FAIL today: implement.js never references depends_on, never inspects
`git log --all` for a containing branch, and the pre-flight guard asserts
only ok/sane_branch/datenow_clean/daemon_ok — no dep-presence check.

Out of scope (do NOT assert against): the worktree copy under
.claude/worktrees/.../implement.js — only the canonical workflow is edited.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WORKFLOWS = _SERVICE_ROOT.parent.parent / ".claude" / "workflows"


def _implement_src() -> str:
    return (_WORKFLOWS / "implement.js").read_text(encoding="utf-8")


def _locate_block(src: str) -> str:
    """The Locate phase: from `phase('Locate')` to the per-step handlers
    section. This is the seam that must read depends_on and choose the base
    BEFORE creating the feature branch."""
    start = src.index("phase('Locate')")
    end = src.index("Per-step handler prompts", start)
    return src[start:end]


def _preflight_block(src: str) -> str:
    """The Pre-flight phase: from `phase('Pre-flight')` up to the Locate
    phase. This is where the dependency-presence assertion belongs."""
    start = src.index("phase('Pre-flight')")
    end = src.index("phase('Locate')", start)
    return src[start:end]


# ── AC: Locate reads depends_on before creating the branch ───────────────

def test_locate_reads_depends_on_before_branching():
    """The Locate step must read the task's depends_on and use it to choose
    the branch base — not the bare 'create a feature branch off main'."""
    src = _implement_src()
    assert "depends_on" in src, (
        "implement.js never references depends_on — the branch base is "
        "cut off bare main with zero dependency awareness (task d47c5f11)"
    )
    locate = _locate_block(src)
    assert "depends_on" in locate, (
        "depends_on is not consulted inside the Locate phase, so the base "
        "is chosen before the dependency substrate is considered"
    )


def test_locate_inspects_all_branches_for_dep_commit():
    """For a done-but-unmerged dep, the base is the branch CONTAINING that
    dep's [conductor:<dep8>] commit — found via `git ... log --all` /
    branch --contains across ALL refs (not just ancestors of main)."""
    src = _implement_src()
    locate = _locate_block(src)
    assert ("log --all" in locate
            or "branch --all" in locate
            or "branch -a --contains" in locate
            or "branch --contains" in locate
            or "for-each-ref" in locate), (
        "Locate never searches ALL branches for a dep's containing branch — "
        "a done-but-unmerged dep yields a substrate-less base off main"
    )


def test_locate_keys_on_conductor_commit_marker():
    """Base resolution must key on the dep's [conductor:<dep8>] commit
    marker (the tag commitInstr stamps) to find the containing branch."""
    src = _implement_src()
    locate = _locate_block(src)
    # The grep marker the base-resolution relies on is `[conductor:<dep8>]`.
    assert "conductor:" in locate and ("--grep" in locate or "grep" in locate), (
        "Locate does not grep for the [conductor:<dep8>] commit marker to "
        "locate the dep's containing branch"
    )


def test_locate_preserves_fresh_off_main_when_dep_on_main():
    """When a dep's commit is already reachable from origin/main (or there
    are no deps), the branch is still cut fresh off origin/main — current
    behavior preserved, explicitly stated in the Locate instruction."""
    src = _implement_src()
    locate = _locate_block(src)
    assert "origin/main" in locate, (
        "Locate no longer anchors the default base on origin/main — the "
        "no-deps / dep-already-merged path must remain fresh off origin/main"
    )


def test_locate_rejects_base_behind_origin_main():
    """The chosen base must NEVER be a branch that is BEHIND origin/main —
    the Locate instruction must guard against picking a stale containing
    branch (e.g. rev-list HEAD..origin/main / behind check on the base)."""
    src = _implement_src()
    locate = _locate_block(src)
    assert "behind" in locate.lower(), (
        "Locate does not guard the chosen base against being behind "
        "origin/main — a stale containing branch could be selected"
    )


def test_locate_blocks_when_dep_not_done():
    """A task with a dep that is NOT done must be treated as BLOCKED — the
    workflow does not drive (the Locate instruction must say so). The
    blocking wording must be tied to dependency state, not the unrelated
    'unblocked task' phrase in the task_next pick text."""
    src = _implement_src()
    locate = _locate_block(src)
    # Find a blocked/not-done assertion that sits NEAR depends_on handling,
    # not the pre-existing 'highest-priority unblocked task' pick text.
    lowered = locate.lower()
    found = False
    for kw in ("not done", "not yet done", "is blocked", "treat as blocked",
               "halt", "do not drive"):
        idx = lowered.find(kw)
        while idx != -1:
            window = lowered[max(0, idx - 200): idx + 200]
            if "depends_on" in window or "dep " in window or "dependency" in window:
                found = True
                break
            idx = lowered.find(kw, idx + 1)
        if found:
            break
    assert found, (
        "Locate does not treat a not-done DEPENDENCY as blocked — the "
        "workflow would drive against an incomplete dependency (the only "
        "'blocked' wording today is the unrelated task_next pick text)"
    )


# ── AC: Pre-flight gains a dependency-presence assertion ─────────────────

def test_preflight_schema_has_dep_presence_field():
    """PREFLIGHT_SCHEMA must gain a dependency-presence boolean alongside
    ok/sane_branch/datenow_clean/daemon_ok so the guard can fail fast."""
    src = _implement_src()
    # The schema block that lists the pre-flight required fields.
    schema_start = src.index("PREFLIGHT_SCHEMA")
    schema_block = src[schema_start:schema_start + 700]
    assert ("deps_present" in schema_block
            or "dep_present" in schema_block
            or "deps_satisfied" in schema_block
            or "deps_reachable" in schema_block), (
        "PREFLIGHT_SCHEMA has no dependency-presence field — the guard "
        "cannot assert the dep substrate before the drive"
    )


def test_preflight_asserts_dependency_presence():
    """The pre-flight agent prompt must check that every done depends_on
    task's [conductor:<dep8>] commit is reachable from the chosen base OR
    carried by some branch — i.e. it consults depends_on."""
    src = _implement_src()
    preflight = _preflight_block(src)
    assert "depends_on" in preflight, (
        "the pre-flight guard never inspects depends_on — a missing-substrate "
        "dep still surfaces late at write_failing_tests, not in ~5s here"
    )


def test_preflight_emits_actionable_merge_first_halt():
    """When a done dep's commit is unreachable from the base AND no branch
    carries it, the halt line must be actionable — tell the user to merge
    the dep first ('done but not merged ... merge it first')."""
    src = _implement_src()
    preflight = _preflight_block(src)
    assert "merge" in preflight.lower(), (
        "the pre-flight dep-presence halt is not actionable — it must tell "
        "the user to merge the unmerged dependency first"
    )


# ── AC: dry-run trace proves the dep-aware base (no false-green) ─────────

def test_dry_run_reports_dep_aware_base():
    """A dry-run trace of Locate on a task whose dep is done-but-unmerged
    must report the dep's CONTAINING branch as the base it WOULD use — the
    DRY branch of the Locate instruction must be dependency-aware too, not
    just the live path. (Provability: the dry-run is how the fix is shown.)"""
    src = _implement_src()
    locate = _locate_block(src)
    # The DRY ternary inside the BRANCH instruction must also speak of the
    # dependency-derived base, not merely 'the branch name you WOULD create'.
    assert "DRY" in locate, "Locate has no dry-run branch shape"
    # depends_on must appear in a region that the dry-run path also reaches:
    # the single BRANCH instruction string carries both the DRY and live
    # arms, so depends_on co-locating with the DRY ternary proves the
    # dry-run trace is dependency-aware (the only way to PROVE the fix).
    branch_idx = locate.index("BRANCH:")
    branch_instr = locate[branch_idx:branch_idx + 1200]
    assert "depends_on" in branch_instr or "dep" in branch_instr.lower(), (
        "the BRANCH instruction (covering both DRY and live arms) is not "
        "dependency-aware — a dry-run trace cannot prove the dep-aware base"
    )


# ── AC: only the canonical workflow is edited (worktree untouched) ───────

def test_only_canonical_workflow_targeted():
    """Guard: this suite asserts against the canonical workflow path, never
    a worktree copy — the worktree under .claude/worktrees syncs separately
    and is explicitly out of scope (plan_doc)."""
    assert _WORKFLOWS.name == "workflows"
    assert "worktrees" not in str(_WORKFLOWS), (
        "test is pointed at a worktree copy — only the canonical "
        ".claude/workflows/implement.js is in scope"
    )
    assert (_WORKFLOWS / "implement.js").exists(), (
        "canonical .claude/workflows/implement.js not found"
    )
