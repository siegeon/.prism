"""RED scaffold — STRAND B: phase-merge orchestration + dep-base guard
(task 56458db1).

Two acceptance criteria, two halves:

B1 (RED today) — the multi-phase driver (prototype / phase-router) must
   gain a PHASE-MERGE GATE so phase N is merged to main BEFORE phase N+1
   launches. Root cause (memory project_implement_branches_off_main): the
   implement workflow cuts every task branch off main, so a stacked epic's
   phase N+1 depends on un-merged phase N substrate and hard-halts. The
   fix lands the merge-before-next-phase rule in the phase router
   (prototype.js — the engine the /prototype phase router runs). FAILS
   today: prototype.js registers a single planning task and never speaks
   of merging phase N to main before launching phase N+1.

B2 (PASSES today — REGRESSION GUARD, must STAY green) — PR #130's
   dependency-aware base in implement.js Locate path (b) is the substrate
   safety net. This task KEEPS it (must not be removed). These assertions
   pin the path-(b) wiring so a later edit can't silently drop it.

Source-structure asserts: the workflow JS the harness runs is the
user-facing seam (mirrors test_implement_dependency_aware_branch_base.py).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WORKFLOWS = _SERVICE_ROOT.parent.parent / ".claude" / "workflows"


def _prototype_src() -> str:
    return (_WORKFLOWS / "prototype.js").read_text(encoding="utf-8")


def _implement_src() -> str:
    return (_WORKFLOWS / "implement.js").read_text(encoding="utf-8")


def _locate_block(src: str) -> str:
    start = src.index("phase('Locate')")
    # SUPERSEDED ANCHOR (2026-08-01): the drive used to end its Locate phase at a
    # "Per-step handler prompts" section that built one closure per step. The
    # conductor_work rewrite deleted that section (the server hands back each
    # job), so the Locate phase now ends where the Graph phase begins. The base
    # SAFETY invariant these tests pin is unchanged and still required.
    end = src.index("-- Phase: Graph", start)
    return src[start:end]


# ── B1: phase-merge gate in the multi-phase driver (RED today) ───────────

def test_phase_router_speaks_of_merging_phase_before_next():
    """The phase router must encode 'merge phase N to main before launching
    phase N+1' — the rule that prevents stacked-epic stranding."""
    src = _prototype_src().lower()
    assert "merge" in src, (
        "prototype.js (the phase router) never mentions merging — it cannot "
        "enforce phase N merged-to-main before phase N+1 launches, so a "
        "stacked epic's next phase cuts off un-merged substrate"
    )
    # The merge must be tied to main and to the phase-sequencing concept,
    # not an incidental word.
    has_main = "main" in src
    has_phase_seq = ("next phase" in src or "phase n+1" in src
                     or "before" in src and "phase" in src)
    assert has_main and has_phase_seq, (
        "prototype.js does not gate the next phase on the prior phase being "
        "merged to main — the phase-merge gate (B1) is absent"
    )


def test_phase_router_has_phase_merge_gate_marker():
    """A discoverable marker for the phase-merge gate so the orchestration
    is explicit (not buried). Accept any of the natural spellings."""
    src = _prototype_src().lower()
    assert ("phase-merge" in src or "phase merge" in src
            or "merge gate" in src or "merge-gate" in src
            or ("merged to main" in src and "phase" in src)), (
        "prototype.js has no explicit phase-merge gate marker — the "
        "merge-before-next-phase rule is not a first-class step in the "
        "phase router"
    )


def test_phase_router_orders_phases_with_dependency_chain():
    """Stacked phases must be ordered so each phase depends on the prior —
    the driver must register phases with a dependency/sequence linkage
    (depends_on / dependencies / sequential) so phase N+1 cannot launch
    until phase N is done+merged."""
    src = _prototype_src().lower()
    assert ("depends_on" in src or "dependencies" in src
            or "sequential" in src or "in order" in src
            or "phase n" in src), (
        "prototype.js does not chain phases by dependency/sequence — "
        "without an ordering linkage there is no point at which 'phase N "
        "merged' can gate 'phase N+1 launch'"
    )


# ── B2: dependency-aware base in implement.js Locate path (b) — GUARD ────
# These PASS today (PR #130 landed the feature). They must STAY green: the
# task preserves the substrate safety net rather than removing it.

def test_guard_locate_still_reads_depends_on():
    """REGRESSION GUARD: Locate must still read depends_on before choosing
    the base. Removing this re-opens the ~15-min hard-halt (mx-a56419)."""
    locate = _locate_block(_implement_src())
    assert "depends_on" in locate, (
        "REGRESSION: implement.js Locate no longer reads depends_on — the "
        "PR #130 dependency-aware base safety net was removed"
    )


def test_guard_locate_still_finds_containing_branch_path_b():
    """REGRESSION GUARD: path (b) — for a done-but-unmerged dep, the base is
    the branch CONTAINING that dep's [conductor:<dep8>] commit, found via
    `git log --all` + `branch --contains`."""
    locate = _locate_block(_implement_src())
    assert ("log --all" in locate or "branch -a --contains" in locate
            or "branch --contains" in locate), (
        "REGRESSION: implement.js Locate path (b) no longer searches ALL "
        "branches for the dep's containing branch — substrate safety net "
        "removed"
    )
    assert "conductor:" in locate and "grep" in locate, (
        "REGRESSION: Locate no longer greps for the [conductor:<dep8>] "
        "commit marker to resolve the containing branch (path b)"
    )


def test_guard_locate_still_rejects_base_behind_main():
    """REGRESSION GUARD: the chosen base must never be behind origin/main."""
    locate = _locate_block(_implement_src())
    assert "behind" in locate.lower() and "origin/main" in locate, (
        "REGRESSION: implement.js Locate dropped the 'base must not be "
        "behind origin/main' guard from the dependency-aware base"
    )
