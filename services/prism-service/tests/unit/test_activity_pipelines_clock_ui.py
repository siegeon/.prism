"""RED scaffold (UI contract) — Phase 5 (epic 4fd1e6b4, task 034fbd6c):
SettingsPage BackgroundWorkersPanel must collapse the memory concern into
~2 pipeline rows + 1 maintenance-clock row.

The web app has no JS test runner, so the UI-FIRST acceptance criterion is
pinned by asserting SettingsPage.tsx SOURCE. Today BackgroundWorkersPanel
(:1513-1574) maps EVERY worker dict to one <WorkerRow> (:1574); it has no
notion of a "pipeline" vs "clock" row and no per-pass consolidation receipt
with progressive disclosure.

Pinned here (FAIL today):
  - The panel branches on the row `kind` ("pipeline" / "clock") and renders
    dedicated markup for each — not a single WorkerRow per item.
  - The event-pipeline render surfaces the four readouts: events/min,
    queue depth, last event, in-flight.
  - The clock render surfaces interval / last sweep / next sweep.
  - The per-pass consolidation receipt (N extracted, M deduped, K superseded,
    J retired) renders as a one-line summary with click-to-expand detail
    (progressive disclosure) — never a <pre>/raw JSON wall.
  - The section description no longer enumerates the now-collapsed discrete
    memory workers (reflection worker / memory summary) as separate rows.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
        / "pages" / "SettingsPage.tsx")


def _src() -> str:
    return _WEB.read_text(encoding="utf-8")


def test_panel_branches_on_row_kind():
    src = _src()
    # The Worker type / panel must understand a row `kind` so pipeline and
    # clock rows render differently from a plain worker row.
    assert 'kind' in src and ('"pipeline"' in src or "'pipeline'" in src), (
        "BackgroundWorkersPanel must branch on a row kind=='pipeline'; the "
        "memory concern is no longer one-WorkerRow-per-worker"
    )
    assert '"clock"' in src or "'clock'" in src, (
        "panel must render a dedicated clock row (kind=='clock')"
    )


def test_pipeline_row_surfaces_throughput_readouts():
    src = _src()
    # The four event-pipeline readouts the oracle requires.
    assert "events_per_min" in src, "pipeline row must show events/min"
    assert "queue_depth" in src, "pipeline row must show queue depth"
    assert "last_event_ts" in src or "last event" in src.lower(), (
        "pipeline row must show last-event timestamp"
    )
    assert "in_flight" in src, "pipeline row must show in-flight count"


def test_clock_row_surfaces_sweep_readouts():
    src = _src()
    assert "next_sweep" in src, "clock row must show next sweep"
    assert "last_sweep" in src, "clock row must show last sweep"
    assert "interval_s" in src or "interval" in src.lower(), (
        "clock row must show the heartbeat interval"
    )


def test_consolidation_receipt_progressive_disclosure():
    src = _src()
    # A per-pass receipt rendered as one-line summary + click-to-expand.
    # The receipt fields the oracle names — extracted / deduped / superseded /
    # retired — must be referenced, not dumped as raw JSON.
    receipt_terms = ("extracted", "deduped", "superseded", "retired")
    assert any(t in src for t in receipt_terms), (
        "the consolidation receipt (extracted/deduped/superseded/retired) "
        "must render in the panel"
    )
    # Progressive disclosure: a collapsible/expand affordance, like the
    # existing WorkerRow drilldown (open/setOpen) — NOT a wall of text.
    assert ("setOpen" in src or "summary" in src.lower()
            or "expand" in src.lower()), (
        "the receipt must be a one-line summary with click-to-expand detail "
        "(progressive disclosure)"
    )


def test_no_raw_json_dump_for_receipt():
    src = _src()
    # Render structured via Hermes primitives — never <pre>JSON.stringify(...).
    assert "JSON.stringify" not in src, (
        "no JSON.stringify wall in the activity panel — render structured"
    )
    assert "<pre>" not in src, "no <pre> raw dump in the activity panel"


def test_section_description_drops_collapsed_memory_workers():
    src = _src()
    # The activity section blurb still enumerates the now-collapsed discrete
    # memory workers as separate rows; once collapsed it should describe the
    # pipeline + clock model instead of "opt-in reflection worker".
    assert "opt-in reflection worker" not in src, (
        "section description must stop enumerating the discrete reflection "
        "worker — it is folded into the event pipeline + clock rows"
    )
