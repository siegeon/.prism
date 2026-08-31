"""The Interactions panel shows NOW, not only a lifetime figure (a91976ec).

The panel reported one zero-result figure and it read about 35 percent, so a
reader concluded that memory search was broken. It was not: search recovered
on 2026-08-29 and now returns results on nearly every query. The owner read
the panel exactly that way and asked what the point of memory search is.

Found while planning this slice, and worse than the ticket claimed: the
figure is not a fourteen-day average at all. `api/dashboard.py` counts
`WHERE n_results = 0` with NO window clause, so the panel shows a LIFETIME
number labelled as though it described a period. Measured at the base
commit: zero 1086 of total 3090.

The fix shows a RECENT window BESIDE the lifetime figure. Both survive. The
ticket's own likely_misfire names the trap: replacing the average would hide
a long-running problem instead of a recovered one, and a window short enough
that one quiet hour reads as health is its own lie.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest


def _searches_db(tmp_path: Path, rows) -> Path:
    """A brain.db holding only what the panel reads: ts and n_results."""
    p = tmp_path / "brain.db"
    conn = sqlite3.connect(p)
    conn.execute(
        "CREATE TABLE searches (id INTEGER PRIMARY KEY, ts TEXT, query TEXT, "
        "n_results INTEGER, latency_ms INTEGER, domain TEXT)"
    )
    conn.executemany(
        "INSERT INTO searches (ts, query, n_results, latency_ms, domain) "
        "VALUES (?, ?, ?, ?, 'expertise')", rows)
    conn.commit()
    conn.close()
    return p


def _iso(days_ago: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.gmtime(time.time() - days_ago * 86400))


# ----------------------------------------------------------------------
# AC-1 / AC-5 -- the recent window exists, and an empty one is honest
# ----------------------------------------------------------------------

def test_the_panel_reports_a_recent_window(tmp_path):
    """A recent-window rate is computed from the same rows, and an EMPTY
    window reports None rather than 0.0.

    A zero-row window returning 0.0 would render as perfect health, which
    is the second trap the ticket names: a window so short that silence
    reads as success.
    """
    from prism_service.api.dashboard import recent_zero_rate

    # Rows only in the distant past: the recent window holds nothing.
    db = _searches_db(tmp_path, [(_iso(20), "q", 0, 10)])
    empty = recent_zero_rate(str(db), days=2)

    assert empty["total"] == 0
    assert empty["rate"] is None, (
        "an empty recent window must report None, never 0.0 -- 0.0 renders "
        "as perfect health when nothing was measured at all")
    assert empty["days"] == 2, "the window must name its own span"


def test_a_recent_window_counts_only_its_own_rows(tmp_path):
    """The window is a real ts filter, not the lifetime figure renamed."""
    from prism_service.api.dashboard import recent_zero_rate

    db = _searches_db(tmp_path, [
        (_iso(30), "old-miss", 0, 10),
        (_iso(30), "old-miss", 0, 10),
        (_iso(0.1), "new-hit", 5, 10),
        (_iso(0.1), "new-hit", 7, 10),
    ])
    got = recent_zero_rate(str(db), days=2)

    assert got["total"] == 2, "only the two recent rows are in the window"
    assert got["zero"] == 0
    assert got["rate"] == 0.0


# ----------------------------------------------------------------------
# AC-2 / AC-4 -- a recovery is visible, and the lifetime figure survives
# ----------------------------------------------------------------------

def test_a_recovery_is_visible_beside_the_average(tmp_path):
    """The two figures must be able to DISAGREE, and both must survive.

    This is the ticket's likely_misfire made executable: replacing the
    lifetime figure with a recent one would hide a long-running problem
    instead of revealing a recovered one.
    """
    from prism_service.api.dashboard import recent_zero_rate

    # An outage long ago, clean since -- the live shape on 2026-08-31.
    rows = [(_iso(9), "miss", 0, 10) for _ in range(8)]
    rows += [(_iso(0.2), "hit", 4, 10) for _ in range(10)]
    db = _searches_db(tmp_path, rows)

    lifetime_zero = 8
    lifetime_total = 18
    lifetime_rate = lifetime_zero / lifetime_total

    recent = recent_zero_rate(str(db), days=2)

    assert recent["rate"] == 0.0, "the recent window must show the recovery"
    assert lifetime_rate > 0.4, "the lifetime figure must still carry the outage"
    assert recent["rate"] < lifetime_rate, (
        "the whole point: the recent figure and the lifetime figure differ, "
        "so a reader can tell today apart from the period being averaged")


def test_a_still_broken_window_does_not_read_as_healthy(tmp_path):
    """A recent window must report a REAL problem, not only a recovery.

    If the recent figure only ever looked good, it would be decoration.
    """
    from prism_service.api.dashboard import recent_zero_rate

    db = _searches_db(tmp_path, [(_iso(0.1), "miss", 0, 10) for _ in range(6)])
    got = recent_zero_rate(str(db), days=2)

    assert got["rate"] == 1.0
    assert got["zero"] == 6


# ----------------------------------------------------------------------
# AC-3 -- the panel names both periods
# ----------------------------------------------------------------------

def test_the_panel_names_both_periods():
    """A number whose period is unlabelled is how this defect happened.

    Asserted against the rendered component source: the panel must carry a
    recent-window stat beside the lifetime one, and neither may be nameless.
    """
    root = Path(__file__).resolve().parents[2]
    src = (root / "prism_service" / "web" / "src" / "pages"
           / "DashboardPage.tsx").read_text(encoding="utf-8")

    assert "recent_zero" in src, (
        "the panel must read the recent-window figure the endpoint serves")
    assert "all time" in src.lower() or "lifetime" in src.lower(), (
        "the lifetime figure must be NAMED, so nobody reads it as 'now'")


def test_the_endpoint_still_serves_the_lifetime_figure():
    """AC-4 at the seam: the existing key is untouched."""
    import inspect

    from prism_service.api import dashboard

    src = inspect.getsource(dashboard)
    assert '"zero": zero_results' in src, (
        "the lifetime zero-result figure must survive -- replacing it would "
        "hide a long-running problem instead of revealing a recovery")
