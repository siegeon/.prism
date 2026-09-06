"""The Background activity page shows activity, not the whole job archive.

Owner, 2026-09-05, looking at /settings/activity: 52 completed analyzer
runs back to May rendered under "Background activity" as though they were
running now -- "we don't scare people into thinking we are using all of
their tokens". The default view keeps every pending / in-progress job and
only the done ones from the last 24h; the explicit completed/failed
filters stay unbounded so history remains reachable. The SPA has no JS
test runner, so the ACTUAL TSX source is pinned.
"""
from __future__ import annotations

import re
from pathlib import Path

TSX = (Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src"
       / "pages" / "SettingsPage.tsx")


def _src() -> str:
    return re.sub(r"\{/\*.*?\*/\}|//[^\n]*", "", TSX.read_text(encoding="utf-8"), flags=re.S)


def test_the_default_view_hides_done_jobs_older_than_a_day():
    src = _src()
    assert "RECENT_DONE_MS = 24 * 60 * 60 * 1000" in src
    assert 'filter === "all"' in src and "allJobs.filter(doneRecently)" in src
    assert "now - j.completed_at * 1000 <= RECENT_DONE_MS" in src


def test_in_flight_jobs_are_never_hidden_by_age():
    src = _src()
    m = re.search(r"const doneRecently = \(j: Job\) =>\s*(!isDone\(j\) \|\|)", src)
    assert m, "an unfinished job must pass the recency check unconditionally"


def test_explicit_history_filters_stay_unbounded():
    src = _src()
    assert re.search(r'filter === "all"\s*\?\s*allJobs\.filter\(doneRecently\)\s*:\s*allJobs\.filter\(\(j\) => j\.state === filter\)', src)


def test_the_hidden_count_is_stated_not_silent():
    src = _src()
    assert "older finished hidden" in src
