"""Work in flight is visible the moment it starts (task 2b363d8e).

Owner 2026-07-29, watching a ticket being driven: "is this a task we are
working? i dont see it active in the application" — a task read/researched
by a worker before its first conductor_work() call shows status='pending',
workflow_step='', zero worktree on disk: indistinguishable from untouched.

This is NOT a new state machine. conductor_service.py is a control_plane
POLICY_FILE (control_plane.POLICY_FILES) and this task carries no
policy-change/control-plane tag, so it stays untouched — every rendering
primitive below (managed_tasks intake lane, activity_for adrift/stalled
wording, TasksPage's step-badge gating) ALREADY exists and is pinned here
read-only, as a regression guard.

The one real gap, and the one assertion in this file that is RED on the
tree before this task's doctrine edit and GREEN after: nothing teaches a
worker to CLAIM a task — task_update(id, status="in_progress"), which is
free (no worktree, no conductor_service.py involvement) — as the FIRST
action, before reading/researching it. See test_claim_doctrine_* below.

Style matches test_conductor_page_animated_cleanup_ui.py / test_work_search
_filter_ui.py: the PRISM SPA ships no JS test runner, so TSX acceptance
criteria are pinned by asserting the ACTUAL SOURCE (the real element/branch,
never a comment, never a fixed character window around a match).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_PKG = _SERVICE_ROOT / "prism_service"
_TOOLS = _PKG / "mcp" / "tools.py"
_INSTRUCTIONS = _PKG / "mcp" / "instructions.py"
_TASK_SERVICE = _PKG / "services" / "task_service.py"
_CONDUCTOR_SERVICE = _PKG / "services" / "conductor_service.py"
_TASKS_PAGE = _PKG / "web" / "src" / "pages" / "TasksPage.tsx"
_SDLC_PROGRESS = _PKG / "web" / "src" / "components" / "conductor" / "SdlcProgress.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _slice(src: str, start_marker: str, end_marker: str, *, from_end_of_start: bool = False) -> str:
    start = src.index(start_marker)
    if from_end_of_start:
        start += len(start_marker)
    end = src.index(end_marker, start)
    return src[start:end]


# ---------------------------------------------------------------------------
# AC-1 — claiming via task_update(status="in_progress") is CHEAP: no
# worktree, no conductor_service.py involvement on that path.
# ---------------------------------------------------------------------------


def test_task_update_in_progress_branch_never_creates_a_worktree():
    src = _read(_TOOLS)
    handler = _slice(
        src, 'if name == "task_update":', 'if name == "task_link_session":')
    for banned in ("flow_start", "ensure_worktree", "worktree", "conductor_flow"):
        assert banned not in handler, (
            f"task_update's dispatch must stay a cheap claim path — found "
            f"{banned!r}, which means claiming now pays worktree/flow cost "
            f"(the exact misfire this ticket forbids)"
        )
    # The session gate DOES run on this path (auto-links a REAL session,
    # never the phantom request handle) — that machinery must stay wired.
    assert '_resolve_real_session_id' in handler
    assert 'arguments.get("status") == "in_progress"' in handler


def test_task_service_update_is_a_pure_field_write_no_worktree_calls():
    src = _read(_TASK_SERVICE)
    body = _slice(
        src,
        "def update(self, task_id: str, **kwargs: object) -> Optional[Task]:",
        "\n    def next_task(",
    )
    for banned in ("flow_start", "ensure_worktree", "worktree", "git.", "subprocess"):
        assert banned not in body, (
            f"task_service.update() must remain a plain DB write — found "
            f"{banned!r}, which would make a claim as expensive as entering "
            f"the SDLC"
        )


# ---------------------------------------------------------------------------
# AC-2 — a claimed-not-stepped task is already surfaced: managed_tasks()'s
# synthetic INTAKE_STEP lane, and task_list(status="in_progress") as the
# "equivalent claimed state" filter named by the oracle.
# ---------------------------------------------------------------------------


def test_managed_tasks_intake_lane_covers_a_bare_claim():
    src = _read(_CONDUCTOR_SERVICE)
    body = _slice(
        src, "def managed_tasks(self) -> list[dict]:",
        "def step_buckets(self) -> dict[str, int]:")
    assert 'if step == "" and gate == "none":' in body
    assert 'if status == "in_progress":' in body
    assert "step = self.INTAKE_STEP" in body, (
        "a task claimed (in_progress) but not yet stepped must still enter "
        "the synthetic intake lane, or it stays invisible on /conductor"
    )
    # And it must NOT be gated behind an existing workflow_step or gate —
    # this branch only runs when both are still empty/none, i.e. exactly the
    # claimed-before-first-report window this ticket is about.
    assert 'if getattr(t, "parent_id", "") and step == "" and gate == "none":' in body


def test_task_list_status_filter_is_the_equivalent_claimed_state_query():
    src = _read(_TOOLS)
    handler = _slice(src, 'if name == "task_list":', 'if name == "task_next":')
    assert "status=_pass_status" in handler, (
        "task_list must pass an explicit status through to task_svc.list() "
        "— this IS the 'GET /api/tasks?status=in_progress (or an equivalent "
        "claimed state)' filter named by the oracle"
    )


# ---------------------------------------------------------------------------
# AC-3 — the Work board never confuses claimed with executing a step: the
# step Lozenge is conditional on a non-empty workflow_step; the status cell
# always shows the raw status.
# ---------------------------------------------------------------------------


def test_work_row_never_renders_a_step_badge_for_an_unstepped_claim():
    src = _read(_TASKS_PAGE)
    start = src.index("function WorkRow(")
    body = src[start:]  # WorkRow is the last top-level function in the file
    assert "{step && <Lozenge" in body, (
        "the step badge must be conditioned on a truthy workflow_step — a "
        "claimed-but-unstepped task (step='') must render NO step lozenge, "
        "never a false 'executing a step' claim"
    )
    assert "{item.status}" in body, (
        "the status cell must always show the raw status text for native "
        "rows, so a claimed task reads 'in_progress' distinctly from a "
        "pending one"
    )


# ---------------------------------------------------------------------------
# AC-4 — never idle/stalled for a fresh claim; a genuinely dead claim DOES
# surface as 'stalled . needs you' (the release/expiry signal).
# ---------------------------------------------------------------------------


def test_activity_for_reads_a_fresh_claim_as_adrift_not_stalled():
    src = _read(_CONDUCTOR_SERVICE)
    body = _slice(
        src, "def activity_for(self, task, phase_progress: dict) -> dict:",
        "\n        return {")
    # The leaf-task precedence block, contiguous and in order: a real recent
    # transition wins ("working"); else a live-but-busy-elsewhere session
    # reads "adrift" (NOT stalled) while quiet <= 90s; only past that does a
    # claim fall to "stalled" — the release/expiry signal for a dead worker.
    assert (
        'elif motion is not None and motion <= 120:\n'
        '                state = "working"            # a real recent transition on THIS task\n'
        '            elif quiet is not None and quiet <= 90:\n'
        '                state = "adrift"             # session alive but busy elsewhere\n'
        '            else:\n'
        '                state = "stalled"            # nothing is driving it'
    ) in body, (
        "a fresh claim (no step reported yet, session live) must resolve "
        "via the adrift branch before ever falling through to stalled"
    )


def test_sdlc_progress_activity_copy_never_says_idle_for_adrift():
    src = _read(_SDLC_PROGRESS)
    meta = _slice(
        src, "export const ACTIVITY_META", "\n};", from_end_of_start=True)
    assert 'adrift: { label: "driver active · between step reports", tone: "teal" }' in meta
    assert 'stalled: { label: "stalled · needs you", tone: "rose" }' in meta
    assert "idle" not in meta.lower(), (
        "the honest-activity label map must never use the alarm word "
        "'idle' for a task that is simply claimed and being read"
    )


# ---------------------------------------------------------------------------
# AC-5 — the claim-early convention must be DOCTRINE, not tribal knowledge.
# This is the assertion that is RED before the doctrine text lands and
# GREEN after.
# ---------------------------------------------------------------------------


def test_task_update_tool_description_teaches_claim_before_reading():
    src = _read(_TOOLS)
    desc = _slice(src, 'name="task_update"', "inputSchema=")
    lowered = desc.lower()
    assert "in_progress" in lowered
    assert "before" in lowered and ("reading" in lowered or "research" in lowered), (
        "task_update's tool description must teach: claim a task via "
        "status='in_progress' BEFORE reading/researching it — otherwise "
        "the cheap claim primitive stays undiscoverable"
    )
    assert "worktree" in lowered, (
        "the description should say the claim is free (no worktree), "
        "distinguishing it from conductor_work"
    )


def test_prism_guide_doctrine_teaches_claim_before_reading():
    src = _read(_INSTRUCTIONS)
    lowered = src.lower()
    assert "in_progress" in lowered
    assert "before" in lowered and ("reading" in lowered or "research" in lowered), (
        "the prism_guide preamble (every session's onboarding doctrine) "
        "must teach the claim-before-reading convention, not just the "
        "task_update tool description"
    )
    assert "conductor_work" in src, (
        "the doctrine must distinguish claiming (free, task_update) from "
        "entering the graded SDLC (conductor_work, which creates a worktree)"
    )
