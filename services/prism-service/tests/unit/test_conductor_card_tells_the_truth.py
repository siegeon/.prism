"""UI contract — the conductor card's numbers must match the server, not an
animation. The PRISM SPA has NO JS test runner, so these ACs are pinned by
asserting the ACTUAL web source (same pattern as
test_conductor_page_animated_cleanup_ui.py).

Defect 1 — the printed percentage was DERIVED FROM ELAPSED WALL-CLOCK TIME
(SdlcProgress's rAF loop wrote `liveLabel` from `liveFraction(phase,
elapsedS)`, which creeps toward its own asymptote regardless of real
progress). Epic 0784729f's server pct=0.538 rendered as 77.62%, then climbed
78->86->95% while the epic made zero progress. Fix: the printed percentage is
a pure function of the server's phase.pct + curIdx (`overallSeed`), never a
time-animated state variable. The segment FILL may still tween toward the
live estimate — only the printed number is pinned here.

Defect 3 — the per-step token rows in StepRail summed each history row's
`turn_tokens` bucketed by step id, which let a step's own row absorb
whatever idle gap preceded it ("review previous notes 7.5M tok · 49s").
Fix: StepRail reads a server-scoped `stepTokens` map (api.tasks.
_step_token_totals, tested in test_step_tokens_scoped_to_step_window.py)
keyed by step id, threaded TaskDetailPage -> PlanView -> StepRail.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_PROGRESS = _SRC / "components" / "conductor" / "SdlcProgress.tsx"
_RAIL = _SRC / "components" / "conductor" / "StepRail.tsx"
_PLAN_VIEW = _SRC / "components" / "plan" / "PlanView.tsx"
_DETAIL_PAGE = _SRC / "pages" / "TaskDetailPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Defect 1 — printed percentage is not an elapsed-time estimate
# ---------------------------------------------------------------------------

def test_displayed_percentage_is_not_derived_from_elapsed_time():
    src = _read(_PROGRESS)
    assert "setLiveLabel" not in src, \
        "the printed percentage must not be written from the rAF loop — " \
        "that is what let it drift with elapsed wall-clock time instead of " \
        "real server-reported progress"
    caption = src[src.index("showCaption && curIdx"):]
    assert "overallSeed" in caption[:400], \
        "the printed percentage must read the pure overallSeed (curIdx + " \
        "server pct) / steps.length — never a time-animated state variable"


# ---------------------------------------------------------------------------
# Defect 3 — per-step token rows read a server-scoped map, not a local sum
# ---------------------------------------------------------------------------

def test_step_meta_tokens_come_from_the_server_scoped_map():
    src = _read(_RAIL)
    assert "stepTokens" in src, \
        "StepRail must accept a stepTokens prop carrying the server-scoped " \
        "per-step token totals (api.tasks._step_token_totals)"
    # The old bug: summing turn_tokens across a step's rows let the row that
    # advances INTO a step absorb the whole preceding idle gap.
    assert "tokens={stepTokens(stepTurns)}" not in src, \
        "the current-step row must no longer feed StepMeta from the local " \
        "per-row turn_tokens sum"
    assert "tokens={stepTokens(turns ?? [])}" not in src, \
        "the gate row must no longer feed StepMeta from the local per-row " \
        "turn_tokens sum"
    assert "stepTokens?.[s.id]" in src or "stepTokens[s.id]" in src, \
        "each row's token figure must be looked up by THIS step's id in " \
        "the server-scoped map"


def test_step_tokens_threaded_from_task_detail_through_plan_view():
    detail = _read(_DETAIL_PAGE)
    assert "task.step_tokens" in detail, \
        "TaskDetailPage must read step_tokens off the task-detail payload"
    plan = _read(_PLAN_VIEW)
    assert "stepTokens" in plan, \
        "PlanView's ConductorInfo must carry stepTokens through to <StepRail/>"
