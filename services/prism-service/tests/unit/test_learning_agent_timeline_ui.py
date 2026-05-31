"""RED scaffold — LearningPage agent-timeline panel (task f4498190).

The web app has no JS test runner, so the UI-FIRST acceptance criteria
are pinned by asserting the LearningPage.tsx SOURCE (same pattern as
test_memory_page_activation_ui.py):

  * a per-task agent timeline rendering role/step/model/duration/tokens;
  * cross-run aggregates: avg duration per step, override rate, token
    cost per role;
  * the panel fetches the new /api/agent-runs surface.

FAILS today: LearningPage.tsx has no agent-runs fetch, no timeline panel,
and no cross-run aggregate rendering.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
        / "pages" / "LearningPage.tsx")


def _src() -> str:
    return _WEB.read_text(encoding="utf-8")


def test_learning_page_fetches_agent_runs():
    src = _src()
    assert "agent-runs" in src or "agent_runs" in src, (
        "LearningPage must fetch the /api/agent-runs surface to render the "
        "agent timeline — a panel with no data source is headless"
    )


def test_agent_timeline_renders_role_step_model_duration_tokens():
    src = _src()
    lower = src.lower()
    # The timeline tile must surface each per-agent dimension.
    for token in ("role", "step", "model", "duration", "tokens"):
        assert token in lower, (
            f"agent timeline panel does not render {token!r}"
        )


def test_cross_run_aggregates_present():
    src = _src()
    lower = src.lower()
    # Avg duration per step.
    assert "avg" in lower or "average" in lower, (
        "no avg-duration-per-step aggregate rendered"
    )
    # Override rate — the recurring blind-verifier override is the Tier-3
    # signal the panel surfaces.
    assert "override" in lower, "no override-rate aggregate rendered"
    # Token cost per role.
    assert "token" in lower, "no token-cost aggregate rendered"


def test_timeline_panel_is_a_distinct_section():
    src = _src()
    # A labelled panel/section so it is visible in the SPA, not buried.
    assert ("Agent" in src and ("Timeline" in src or "timeline" in src
            or "Runs" in src or "Run" in src)), (
        "LearningPage has no distinct 'Agent timeline/runs' panel heading"
    )
