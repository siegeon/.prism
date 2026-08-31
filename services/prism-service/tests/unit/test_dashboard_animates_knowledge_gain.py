r"""Red tests for task 0ee4dc98 -- "The Dashboard animates the knowledge gain".

TRACE. Each test names the acceptance criterion(s) it pins, the measurement
that is RED at the base commit 1bece91b, and the file the fix lands in.
The task's `verify` pins exactly the two test functions in this file.

  AC-1 a finished play records a coverage sample, even below the floor
  AC-2 GET /api/brain/health returns a `history` series, oldest-first
       RED AT BASE: `grep -n "brain_coverage_samples"
       prism_service/services/brain_health.py` -> no match; no table, no
       write call, no reader exists. `grep -n "history"
       prism_service/api/brain.py` -> no match.
       FIX LANDS IN: prism_service/services/brain_health.py,
       prism_service/api/brain.py
  AC-3 the Dashboard renders coverage.history as a real animated element
  AC-4 a new sample mounts fresh and existing points tween (keyed, not
       remounted)
       RED AT BASE: `grep -n "motion/react\|<motion\."
       prism_service/web/src/pages/DashboardPage.tsx` -> no match; the page
       imports no `motion` symbol and renders no chart element at all.
       FIX LANDS IN: prism_service/web/src/pages/DashboardPage.tsx
  AC-5 a fall renders through the identical code path as a rise -- no
       direction-keyed color/opacity/visibility branch, and a falling
       sample is never dropped or clamped away
       RED AT BASE: same absence as AC-3/AC-4 -- the property under test
       (one code path for both directions) cannot hold because there is no
       code path yet.
       FIX LANDS IN: prism_service/web/src/pages/DashboardPage.tsx,
       prism_service/services/brain_health.py
  AC-6 no new charting dependency; no reindex introduced -- checked inline
       in both tests below (a guard against the diff, not a base RED).

Backend fixtures mirror tests/unit/test_flow_keeps_the_brain_healthy.py's
own convention exactly: a disposable tmp_path project built from REAL
MemoryService + BrainService (never the live /home/siegeon/.prism store),
a `_StubCtx(data_dir, memory_svc, brain_svc)` monkeypatched onto
`brain_health.get_project` (and, for the route test, onto
`prism_service.api.brain.get_project`), and every new symbol reached
LAZILY inside each test body so a pre-fix run is a genuine `rc==1` red
(real FAILUREs / AttributeErrors), not a collection ERROR.

Frontend assertions source-read the ACTUAL TSX (no JS test runner in this
repo -- tests/unit/test_dashboard_unshipped_card.py:337-339's documented
convention), matching the RENDERED TAG and real prop expressions, never a
comment describing intent.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "prism_service" / "web" / "src"
_DASH = _SRC / "pages" / "DashboardPage.tsx"
_PKG = _ROOT / "prism_service" / "web" / "package.json"
_BRAIN_HEALTH = _ROOT / "prism_service" / "services" / "brain_health.py"


# ---------------------------------------------------------------------------
# Shared backend fixture -- two real plays against one disposable store.
# Play 1 writes+indexes its own single memory (ratio 1.0, a RISE from
# nothing). Play 2 writes nothing new but the store gains three more
# UN-indexed memories in between, so coverage FALLS to 1/4 (0.25), below
# the default 0.5 floor -- CoverageBelowFloor must raise, and the fall must
# still land in the samples table (never swallowed).
# ---------------------------------------------------------------------------

# MemoryService.store dedups on >85% description similarity
# (memory_service.py:225) -- each body must share almost no text with the
# others or the store collapses to fewer active entries than expected.
_BODIES = [
    "The conductor pipeline lands a branch on origin main and then reaps.",
    "Sigma renders the graph viewer with WebGL on a dark canvas surface.",
    "Simplified Technical English keeps every hedge in an instruction.",
    "A gate is decided by an actor that did not produce the evidence.",
]


class _StubCtx:
    """What get_project(project) hands the node/route: two real services and
    the data dir holding scores.db -- the same three attributes the real
    ProjectContext exposes (project_context.py:37,84 and `_data_dir`)."""

    def __init__(self, data_dir: Path, memory_svc, brain_svc) -> None:
        self._data_dir = data_dir
        self.memory_svc = memory_svc
        self.brain_svc = brain_svc


def _stamp_play(scores_db: Path, task_id: str, session_id: str,
                memory_ids: list[str]) -> None:
    conn = sqlite3.connect(str(scores_db), timeout=5.0)
    conn.execute("CREATE TABLE IF NOT EXISTS task_sessions ("
                 "task_id TEXT NOT NULL, session_id TEXT NOT NULL, "
                 "started_at TEXT, ended_at TEXT, "
                 "PRIMARY KEY (task_id, session_id))")
    conn.execute("CREATE TABLE IF NOT EXISTS memory_meta ("
                 "memory_id TEXT PRIMARY KEY, session_id TEXT, status TEXT)")
    conn.execute("INSERT OR REPLACE INTO task_sessions (task_id, session_id) "
                 "VALUES (?, ?)", (task_id, session_id))
    for mid in memory_ids:
        conn.execute("INSERT OR REPLACE INTO memory_meta "
                     "(memory_id, session_id, status) VALUES (?, ?, ?)",
                     (mid, session_id, "active"))
    conn.commit()
    conn.close()


def _two_plays(tmp_path: Path, monkeypatch):
    """Runs play 1 (rise to 1.0) then play 2 (fall to 0.25, below floor).

    Returns (brain_health module, ctx, scores_db path, verdict1,
    caught_exc2) so each test picks the assertions it needs.
    """
    from prism_service.services import brain_health
    from prism_service.services.brain_service import BrainService
    from prism_service.services.memory_service import MemoryService

    data_dir = tmp_path / "proj"
    data_dir.mkdir(parents=True, exist_ok=True)
    memory_svc = MemoryService(mulch_dir=str(data_dir / "mulch"))
    brain_svc = BrainService(
        brain_db=str(data_dir / "brain.db"),
        graph_db=str(data_dir / "graph.db"),
        scores_db=str(data_dir / "scores.db"),
    )
    ctx = _StubCtx(data_dir, memory_svc, brain_svc)
    scores_db = data_dir / "scores.db"
    monkeypatch.setattr(brain_health, "get_project", lambda project: ctx)

    # -- Play 1: one memory, written and indexed by this play. entries=1,
    # indexed=1, ratio=1.0 -- a rise from a store that starts at nothing.
    e1 = memory_svc.store(domain="decision", name="play-one-memory",
                          description=_BODIES[0], type="pattern",
                          classification="architecture")
    _stamp_play(scores_db, "t1", "sess-1", [e1.id])
    verdict1 = brain_health.index_finished_play("t1", "proj", floor=0.5)

    # -- Between plays: three more memories land in the store but are never
    # indexed by anyone (the silent-decay scenario the ticket describes).
    memory_svc.store(domain="decision", name="unindexed-two",
                     description=_BODIES[1], type="pattern",
                     classification="architecture")
    memory_svc.store(domain="decision", name="unindexed-three",
                     description=_BODIES[2], type="pattern",
                     classification="architecture")
    memory_svc.store(domain="decision", name="unindexed-four",
                     description=_BODIES[3], type="pattern",
                     classification="architecture")

    # -- Play 2: writes/indexes nothing new. entries=4, indexed=1,
    # ratio=0.25 -- a real FALL, and it is below the default 0.5 floor.
    _stamp_play(scores_db, "t2", "sess-2", [])
    caught2 = None
    try:
        brain_health.index_finished_play("t2", "proj")
    except brain_health.CoverageBelowFloor as exc:
        caught2 = exc
    return brain_health, ctx, scores_db, verdict1, caught2


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _func_block(src: str, name_pattern: str) -> str:
    """Slice out one top-level function's source, from its `function <name>`
    (or `const <name> = (` ) declaration to the next top-level
    `function `/`export default`/EOF -- mirrors the enclosing-JSX-branch
    discipline from the project's own test-writing lessons: never a fixed
    character window that can spill past the real element."""
    m = re.search(name_pattern, src)
    assert m, f"could not find a definition matching {name_pattern!r} in DashboardPage.tsx"
    start = m.start()
    rest = src[start + 1:]
    nxt = re.search(r"\nfunction [A-Za-z]|\nexport default function", rest)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


# ---------------------------------------------------------------------------
# AC-1 / AC-2 / AC-3 / AC-4 / AC-6
# ---------------------------------------------------------------------------

def test_a_new_sample_moves_the_chart(tmp_path, monkeypatch):
    """A rising sample is recorded, served in `history`, and rendered by a
    real animated, keyed SVG element driven by that history -- so a new
    sample landing (array gains an element) actually moves the chart."""
    brain_health, ctx, scores_db, verdict1, caught2 = _two_plays(tmp_path, monkeypatch)

    # -- AC-1: play 1's rise (entries=1, indexed=1, ratio=1.0) is a real
    # pass, and the node's own verdict says so.
    assert verdict1["outcome"] == "pass"
    assert verdict1["ratio"] == 1.0

    # -- AC-2: the samples table (read directly, the same technique
    # test_flow_keeps_the_brain_healthy.py uses for `docs`) holds at least
    # the first sample, and `coverage_history` (the new reader this task
    # adds) returns it oldest-first with the real numbers, never fabricated.
    history = brain_health.coverage_history(str(scores_db))
    assert len(history) >= 1, (
        "no coverage sample was recorded for the finished play -- there is "
        "no series to draw, which is the task's own stop_if")
    first = history[0]
    assert first["ratio"] == 1.0 and first["entries"] == 1 and first["indexed"] == 1
    assert first["measured_at"], "a sample must carry a real measured_at timestamp"

    # -- route-level: GET /api/brain/health exposes the same series under
    # `history`, via the real FastAPI route, not a hand-shaped dict.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import prism_service.api.brain as brain_api
    monkeypatch.setattr(brain_api, "get_project", lambda project: ctx)
    app = FastAPI()
    app.include_router(brain_api.router, prefix="/api/brain")
    client = TestClient(app)
    body = client.get("/api/brain/health", params={"project": "proj"}).json()
    assert "history" in body, "the /health response must carry a history series"
    assert isinstance(body["history"], list) and len(body["history"]) >= 1
    assert body["history"][0]["ratio"] == 1.0

    # -- AC-3 / AC-4: the Dashboard renders that history as a real animated
    # element, keyed so React reuses nodes across a length change (a new
    # sample landing), with a genuine entrance animation on mount.
    src = _read(_DASH)
    assert "history" in src, "DashboardPage must read the history field"
    assert re.search(r"<motion\.(circle|line)\b", src), (
        "expected a real rendered <motion.circle> or <motion.line> element "
        "(the actual tag, not a comment describing one)")
    chart = _func_block(src, r"function CoverageTrend\b")
    assert re.search(r"\.map\(", chart), (
        "the chart must map over the history series to render one element "
        "per sample")
    assert re.search(r"key=\{`(pt|seg)-\$\{i\}`\}|key=\{i\}", chart), (
        "chart elements must be keyed by their stable index so React reuses "
        "the same node across a history-length change (a new sample), "
        "instead of remounting every element on every render")
    assert "initial={{" in chart or "initial={" in chart, (
        "a genuine `initial` prop is required so the chart plays an "
        "entrance animation on first load, not a pre-rendered final state")
    assert re.search(r"animate=\{\{[^}]*(cx|cy|x1|y1|x2|y2)", chart, re.S), (
        "the animated props must include a real position (cx/cy/x1/y1/x2/y2) "
        "computed from each sample, not a static decoration")
    assert re.search(r"\.ratio\b", chart), (
        "the position must be derived from each sample's own ratio value")
    assert "transition={{" in chart, (
        "a transition (duration/ease) must drive the tween between polls")

    # -- AC-6: no new charting dependency landed for this slice.
    pkg = _read(_PKG)
    assert "@observablehq/plot" in pkg  # unchanged, pre-existing
    assert pkg.count('"motion"') <= 1, "motion must not be re-declared/duplicated"
    new_chart_libs = ("chart.js", "recharts", "victory", "nivo", "highcharts",
                      "apexcharts", "d3-shape", "visx")
    for lib in new_chart_libs:
        assert lib not in pkg.lower(), f"a new charting dependency was added: {lib}"


# ---------------------------------------------------------------------------
# AC-1 / AC-5
# ---------------------------------------------------------------------------

def test_a_fall_renders_as_clearly_as_a_rise(tmp_path, monkeypatch):
    """A falling sample (below the coverage floor) is recorded and served
    exactly like a rising one -- never swallowed by the raise, never
    filtered out of history -- and the chart's own styling code contains no
    branch keyed on direction, so a dip is exactly as visible as a climb."""
    brain_health, ctx, scores_db, verdict1, caught2 = _two_plays(tmp_path, monkeypatch)

    # -- AC-1: the fall (1/4 = 0.25) is below the default 0.5 floor and the
    # node RAISES -- it must never only log.
    assert caught2 is not None, (
        "coverage of 1/4 is under the 0.5 floor and the node returned "
        "quietly -- a number nobody acts on is the failure this task "
        "exists to stop")
    assert caught2.entries == 4 and caught2.indexed == 1 and caught2.ratio == 0.25

    # -- AC-1 / AC-5: the raise must NOT have swallowed the sample -- it is
    # still on file, in order, right after the rise, so the fall is exactly
    # as visible in the series as the rise that preceded it.
    history = brain_health.coverage_history(str(scores_db))
    assert len(history) >= 2, (
        "the falling sample was dropped -- a fall must land in the series "
        "exactly like a rise does, never swallowed by the floor raise")
    ratios = [round(h["ratio"], 4) for h in history]
    assert ratios[0] == 1.0, f"the rise must still be first: {ratios}"
    assert 0.25 in ratios, (
        f"the fall (0.25) must be present in the series, unfiltered: {ratios}")
    fall_idx = ratios.index(0.25)
    assert ratios[fall_idx] < ratios[fall_idx - 1], (
        "the fall must be recorded as a genuine decrease, not clamped up "
        f"to look like a rise: {ratios}")

    # -- route-level: the API must not filter the fall out of `history`.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import prism_service.api.brain as brain_api
    monkeypatch.setattr(brain_api, "get_project", lambda project: ctx)
    app = FastAPI()
    app.include_router(brain_api.router, prefix="/api/brain")
    client = TestClient(app)
    body = client.get("/api/brain/health", params={"project": "proj"}).json()
    route_ratios = [round(h["ratio"], 4) for h in body["history"]]
    assert 0.25 in route_ratios, (
        f"the route dropped the falling sample: {route_ratios}")

    # -- AC-5: the chart's own render code never conditions color/opacity/
    # visibility on a comparison between two DIFFERENT samples' ratios --
    # only an index ROLE (e.g. "is this the newest point") may branch, never
    # a direction (rising vs falling) computed from neighboring values.
    src = _read(_DASH)
    chart = _func_block(src, r"function CoverageTrend\b")

    banned_words = ("rising", "falling", "rise", "fall", "decreas", "increas",
                    "direction", "uptrend", "downtrend")
    lowered = chart.lower()
    hits = [w for w in banned_words if w in lowered]
    assert not hits, (
        f"the chart names a direction-based concept ({hits}) -- a fall must "
        f"render through the SAME code path as a rise, never a special case")

    # No fill/stroke assignment may itself contain a comparison operator --
    # any such comparison would be exactly the "only rises read well" bug
    # the task's own likely_misfire names.
    style_lines = [ln for ln in chart.splitlines()
                  if re.search(r"\b(fill|stroke)\s*=", ln)]
    assert style_lines, "expected fill/stroke assignments in the chart"
    for ln in style_lines:
        assert "<" not in ln and ">" not in ln, (
            f"a fill/stroke line contains a comparison operator, which is "
            f"exactly a direction-keyed color branch: {ln!r}")

    # Exactly one literal stroke color is used for the trend line -- not a
    # rising-color / falling-color pair.
    stroke_literals = set(re.findall(r'stroke="([^"]+)"', chart))
    assert len(stroke_literals) <= 1, (
        f"more than one literal stroke color exists ({stroke_literals}) -- "
        f"a fall must not be painted a different base color than a rise")

    # No history entry is ever dropped by a value-based filter before
    # rendering (e.g. `.filter(h => h.ratio >= ...)`), which would hide a
    # fall from the chart entirely.
    assert not re.search(r"history[\w.]*\.filter\(", src), (
        "the history series must not be filtered by value before rendering "
        "-- that would hide a fall from the chart")
