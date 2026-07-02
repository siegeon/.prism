"""Source acceptance test — PI drive-loop know-how + telemetry POST (fc08da8d).

The web app ships no JS test runner, so (as the other *_ui.py tests do) the
acceptance criteria are pinned by asserting over the TS/MJS SOURCE:

  HALF A — web/pi-expert.mjs's EXPERT_SYSTEM_PROMPT must teach an explicit
    ordered DRIVE loop (not just gate names): the section header + the ordered
    action verbs mirroring implement.js (author plan_doc -> advance to
    story_gate -> approve with rubric evidence -> ... -> green_gate).

  HALF B — web/src/lib/piAgent.ts must POST the exchange's usage to the new
    /api/agent/run endpoint on agent_end, attributing it to the task detected
    from the conductor/task tool calls.

FAIL today: neither the DRIVE section nor the telemetry POST exists.
"""

from __future__ import annotations

from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web")
_EXPERT = _WEB / "pi-expert.mjs"
_PIAGENT = _WEB / "src" / "lib" / "piAgent.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


def test_pi_expert_has_drive_loop_section():
    src = _read(_EXPERT)
    up = src.upper()
    assert "DRIVING A TASK" in up, "pi-expert must add a DRIVE-loop section"
    # It teaches the ORDERED action loop, not just step names.
    assert "plan_doc" in src and "plan_diagram" in src
    assert "story_gate" in src and "plan_gate" in src
    assert "red_gate" in src and "green_gate" in src
    # rubric-evidence discipline: approve carries the FR-n/AC-n oracle evidence.
    assert "oracle:" in src
    # Ordered verbs — the loop is actionable (author -> advance -> approve).
    assert "conductor_advance" in src and "conductor_gate" in src


def test_piagent_posts_task_attributed_telemetry_on_agent_end():
    src = _read(_PIAGENT)
    # Posts to the new endpoint.
    assert "/api/agent/run" in src, "piAgent must POST exchange telemetry"
    # Attribution is captured from conductor/task tool calls.
    assert "captureTaskAttribution" in src
    assert "conductor_advance" in src and "task_update" in src
    # Fired from the terminal agent_end path (best-effort, never blocks).
    assert "postTelemetry" in src
    ae = src.index('case "agent_end"')
    # postTelemetry is invoked within the agent_end handler.
    assert "postTelemetry" in src[ae:ae + 2000], \
        "postTelemetry must fire on agent_end"
