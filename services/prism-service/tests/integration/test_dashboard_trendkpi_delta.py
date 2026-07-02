"""RED scaffold — Dashboard TrendKpi delta/arrow semantics (task 855ef8aa).

STRESS-LOOP FINDING: the Dashboard "DOCS INDEXED" trend KPI renders
today's ABSOLUTE count next to a ▲/▼ arrow whose direction is the
day-over-day rate (today vs prev). A day that ADDED docs (9 today after
57 yesterday) renders "▼ 9 today", reading as a loss — the arrow encodes
rate direction while the number encodes an absolute count (mismatched
semantics).

Fix (option b in the finding): render the SIGNED day-over-day delta
(today - prev) next to the arrow so the arrow sign and the number share
one semantic; relabel "today" -> "vs prev day". Then a KPI whose total
rose never shows the bare absolute count behind a red down-arrow.

Source-structure test — scans the real TrendKpi render path on disk.
FAILS today: TrendKpi renders `{nf(today)}` behind the arrow with label
"today" and never computes `today - prev`.
"""
from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
_DASH = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "DashboardPage.tsx"


def _trendkpi_body() -> str:
    src = _DASH.read_text(encoding="utf-8")
    start = src.index("function TrendKpi")
    end = src.index("function Stat", start)
    return src[start:end]


def test_trendkpi_computes_signed_delta():
    """The arrow direction is derived from a signed day-over-day delta."""
    body = _trendkpi_body()
    assert "today - prev" in body, (
        "TrendKpi must compute a signed delta (today - prev) so the arrow "
        "and the shown number share one semantic"
    )


def test_trendkpi_does_not_show_bare_absolute_count_behind_arrow():
    """The value beside the arrow is the signed delta, never the bare
    absolute today count that reads as a loss under a red down-arrow."""
    body = _trendkpi_body()
    # The buggy render was: {up ? "▲" : "▼"} {nf(today)}</span> today
    assert "nf(today)" not in body, (
        "TrendKpi must not render the absolute today count (nf(today)) "
        "behind the ▲/▼ arrow — show the signed delta instead"
    )
    assert not re.search(r">\s*today\s*<", body) and "} today" not in body, (
        "the 'today' label must become an explicit day-over-day label "
        "(e.g. 'vs prev day') so an absolute count is never implied"
    )
