"""The /live canvas draws currently-running daemon background passes as
its own pulsing chips, not a floating DOM panel.

Task fix/canvasplay (owner 2026-09-13, verbatim, with a screenshot of the
conductor canvas): "i dont want the panel on the top, the playing is
supposed to be IN the graph like in a normal game." The System Activity
panel's "running now" section (task_runner ticks, gate_adjudicator sweeps,
deploy sweeps, etc. -- services/system_activity.py's real pass_() call
sites) is retired from that DOM overlay (test_live_page_system_activity_
panel_ui.py's own test_panel_no_longer_renders_a_running_section pins the
retirement) and replaced by:

  - graphState.ts's WorkerActivity type + GraphState.workers array +
    setWorkerActivity() method -- a small, DELIBERATELY SEPARATE array
    from `nodes` (a daemon pass is not a session/task and has no
    queue_depth/gate_state/driveStartedAt of its own),
  - draw.ts's drawWorkerRow(), which paints one pulsing chip per running
    pass in SCREEN SPACE (fixed to the canvas, top-right, alongside the
    HUD/legend/gate-panel -- never a DOM element competing with the board),
  - LivePage.tsx wiring: an initial GET /api/system/activity fetch at
    mount, then refetched ONLY on a real `activity` GET /sse/changes event
    via lib/useChanges's subscribeToChangeKind -- no polling of its own.

The SPA has no JS test runner, so this pins the actual TSX source, the
convention used by tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_GRAPH_STATE = _SRC / "live" / "graphState.ts"
_DRAW = _SRC / "live" / "draw.ts"
_LIVE_PAGE = _SRC / "pages" / "LivePage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_graph_state_has_a_separate_workers_array_and_setter():
    src = _read(_GRAPH_STATE)
    assert "export type WorkerActivity = {" in src
    assert "workers: WorkerActivity[] = [];" in src
    assert "setWorkerActivity(running: Array<{" in src
    # It maps the server's `started_at`/`elapsed_ms` into ONE stored
    # instant (startedAtMs) rather than re-deriving elapsed on every poll --
    # the same "one stored instant, recomputed every frame" shape the
    # mission clock already uses.
    setter_start = src.index("setWorkerActivity(running: Array<{")
    setter = src[setter_start:setter_start + 700]
    assert "startedAtMs:" in setter


def test_draw_ts_paints_a_pulsing_worker_row_in_screen_space():
    src = _read(_DRAW)
    assert "function drawWorkerRow(" in src
    assert "drawWorkerRow(ctx, state.workers, now, width);" in src
    # Called alongside the HUD/legend/gate-panel calls, i.e. AFTER
    # ctx.restore() -- fixed to the canvas (screen space), never inside the
    # pan/zoom-transformed world-space block above it.
    call_idx = src.index("drawWorkerRow(ctx, state.workers, now, width);")
    restore_idx = src.index("ctx.restore();")
    assert restore_idx < call_idx, (
        "the worker row must be drawn in screen space, after ctx.restore() "
        "undoes the pan/zoom transform")
    # A genuine pulse (alpha modulation over time), not a static chip --
    # same visual grammar as the /workflows canvas's own behaviour-node
    # glow, so the two boards read as one system.
    assert "Math.sin((now * 2 * Math.PI) / WORKER_PULSE_PERIOD_MS)" in src


def test_no_worker_chip_draws_when_nothing_is_running():
    src = _read(_DRAW)
    fn_start = src.index("function drawWorkerRow(")
    fn = src[fn_start:fn_start + 400]
    assert "if (!workers.length) return;" in fn, (
        "an idle daemon must draw nothing here -- a chip existing is "
        "itself the claim that a pass is running")


def test_live_page_feeds_worker_activity_from_a_real_activity_event_only():
    src = _read(_LIVE_PAGE)
    assert 'import { subscribeToChangeKind } from "@/lib/useChanges";' in src
    assert 'subscribeToChangeKind("activity", load, project)' in src
    assert "stateRef.current.setWorkerActivity(snap.running)" in src
    # No polling of its own for this -- no setInterval/setTimeout driving
    # the fetch, just the initial mount call plus the event subscription.
    effect_start = src.index("// Daemon background-pass chips")
    effect_end = src.index("}, [project]);", effect_start) + len("}, [project]);")
    effect = src[effect_start:effect_end]
    assert "setInterval" not in effect
    assert "setTimeout" not in effect
