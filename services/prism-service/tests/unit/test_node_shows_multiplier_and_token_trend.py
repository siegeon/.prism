"""Task 112dbb72: a node on /workflows shows its measured multiplier and a
named moving average of its token cost.

Owner: "each programmatic node is a token multiplier" / "make sure the ui
shows the multiplication value of the node and the moving average of token
cost." Codifying a node is the progression move in this game; the
multiplier is the score, and it must be honest.

Drives the real node_token_trend() data-access function
(services/agent_runs_data.py) against a real sqlite scores.db on disk --
the same table the HTTP ingest route and the conductor's own in-process
writer both funnel through -- then source-checks that get_workflows()
(api/workflows.py) and the canvas (web/src/live/workflowGraph.ts) actually
surface what it computed, per the task's stop_if lines:

  - the multiplier must come from a node's own MEASURED runs, never from
    its declared kind/type/agent -- and must change when what a node
    actually does changes, not when a label changes
  - a node with too few runs must read as an honest INDETERMINATE, never
    a fabricated 0 or 1.0
  - the trend's window must be NAMED on screen
  - a pre-fix (corrupt) agent_runs row must never be aggregated without
    the same per-model ceiling filter task fc471aed applies at write time
  - no kind is detected by matching an endpoint url
  - the slice must not touch any control_plane.POLICY_FILES entry
"""
from __future__ import annotations

import re
import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service/web/src"
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))



def _data_module():
    """Imported LAZILY, inside each test, on purpose.

    A module-level `from ... import node_token_trend` turns the red step
    into a pytest COLLECTION error (rc=2): one ImportError stands in for
    every assertion, and the run proves only that a name is absent -- it
    never demonstrates that a single claim in this file is unmet. Resolved
    per-test, each acceptance criterion fails on its own message and the
    red run reports rc=1 with one failure per unmet claim, which is the
    trace the red seat anchors to.
    """
    from prism_service.services import agent_runs_data

    return agent_runs_data


def node_token_trend(*args, **kwargs):
    return _data_module().node_token_trend(*args, **kwargs)


def _row(**kw) -> dict:
    base = dict(
        run_id="run-1", workflow_name="conductor", task_id="T-1",
        session_id="S-1", agent_id="agent-1", parent_agent_id=None,
        role="sm", step="draft_story", model="claude-sonnet-5",
        started_at="2026-08-30T00:00:00+00:00",
        ended_at="2026-08-30T00:01:00+00:00", duration_ms=60000,
        tokens=1000, tool_uses=1, ok=True, gate_state="none",
        verdict_summary=None, evidence_ref=None, cost_usd=0.01,
    )
    base.update(kw)
    return base


def _seed(db: str, rows: list[dict]) -> None:
    """Write rows DIRECTLY (bypassing upsert_agent_run's write-time ceiling
    guard, task fc471aed) -- this is exactly how a real pre-fix corrupt row
    got onto scores.db: written before the guard existed, never rewritten
    since (fc471aed's own stop_if). A test proving the READ-time filter
    must be able to construct that exact shape."""
    conn = sqlite3.connect(db)
    try:
        conn.executescript(_data_module().AGENT_RUNS_SCHEMA)
        cols = (
            "run_id", "workflow_name", "task_id", "session_id", "agent_id",
            "parent_agent_id", "role", "step", "model", "started_at",
            "ended_at", "duration_ms", "tokens", "tool_uses", "ok",
            "gate_state", "verdict_summary", "evidence_ref", "cost_usd",
        )
        for i, row in enumerate(rows):
            row = dict(row)
            row.setdefault("run_id", f"seed-{i}")
            # recorded_at is OMITTED from the insert unless a test supplies
            # one explicitly -- the schema's own `DEFAULT (datetime('now'))`
            # only fires when the column is absent from the statement, so a
            # test that needs to control write-order (e.g. proving the
            # window sorts by recorded_at, not the mixed-format started_at)
            # can set it per row without breaking every other seed call.
            use_cols = cols + ("recorded_at",) if "recorded_at" in row else cols
            vals = [row.get(c) for c in use_cols]
            placeholders = ", ".join("?" for _ in use_cols)
            conn.execute(
                f"INSERT INTO agent_runs ({', '.join(use_cols)}) VALUES ({placeholders})",
                vals,
            )
        conn.commit()
    finally:
        conn.close()


# ----------------------------------------------------------------------
# AC-1: the multiplier comes from MEASURED runs, and changes when what a
# node actually does changes -- never from a declared kind/type/label.
# ----------------------------------------------------------------------


def test_a_low_cost_node_reads_a_large_multiplier_against_the_baseline(tmp_path):
    db = str(tmp_path / "scores.db")
    # "agentic_step" costs real tokens every run -- this IS the baseline.
    agentic_rows = [
        _row(run_id=f"a-{i}", step="agentic_step", tokens=10_000)
        for i in range(5)
    ]
    # "codified_step" makes no model call: near-zero measured tokens.
    codified_rows = [
        _row(run_id=f"c-{i}", step="codified_step", tokens=10, model=None,
             cost_usd=None)
        for i in range(5)
    ]
    _seed(db, agentic_rows + codified_rows)

    out = node_token_trend(db, ["agentic_step", "codified_step"])
    agentic, codified = out["agentic_step"], out["codified_step"]

    assert not agentic["indeterminate"] and not codified["indeterminate"], out
    # Hand check against the raw rows: baseline = mean of ALL good rows =
    # (5*10000 + 5*10) / 10 = 5005.
    assert agentic["avg_tokens"] == 10_000, agentic
    assert codified["avg_tokens"] == 10, codified
    baseline = (5 * 10_000 + 5 * 10) / 10
    assert agentic["multiplier"] == baseline / 10_000, agentic
    assert codified["multiplier"] == baseline / 10, codified

    # The oracle's own framing: an agentic node reads NEAR 1, a codified
    # one reads LARGE.
    assert 0.4 < agentic["multiplier"] < 2.5, agentic
    assert codified["multiplier"] > agentic["multiplier"] * 10, (
        "a node that measurably costs 1000x fewer tokens must read a "
        f"correspondingly larger multiplier: {out}")


def test_the_same_step_id_changes_multiplier_when_its_measured_cost_changes(tmp_path):
    """There is no 'kind' input anywhere in node_token_trend -- the ONLY
    way this test can move the number is by changing what was measured,
    which is exactly the stop_if: 'swapping a node between kinds changes
    the number because the number comes from runs and not the declared
    kind.' Same step id both times; only the recorded tokens differ."""
    db = str(tmp_path / "scores.db")
    _seed(db, [_row(run_id=f"low-{i}", step="convert_me", tokens=5,
                     model=None, cost_usd=None) for i in range(5)]
          + [_row(run_id=f"peer-{i}", step="peer", tokens=8_000) for i in range(5)])
    before = node_token_trend(db, ["convert_me", "peer"])["convert_me"]

    db2 = str(tmp_path / "scores2.db")
    _seed(db2, [_row(run_id=f"high-{i}", step="convert_me", tokens=8_000)
                for i in range(5)]
          + [_row(run_id=f"peer-{i}", step="peer", tokens=8_000) for i in range(5)])
    after = node_token_trend(db2, ["convert_me", "peer"])["convert_me"]

    assert before["multiplier"] > after["multiplier"] * 5, (before, after)
    assert after["multiplier"] == 1.0, after


def _code_only(src: str) -> str:
    """`src` with its docstrings and comments stripped -- a prose
    explanation of what a value is NOT derived from must not itself trip
    an absence check aimed at the executable code."""
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body[0].value.value = ""
    return ast.unparse(tree)


def test_node_token_trend_never_reads_a_step_type_or_kind_field():
    """Structural guarantee, not just a behavioural one: the EXECUTABLE
    body has no parameter and touches no column that could smuggle a
    declared kind/type back in (the docstring's own prose explaining what
    it is NOT derived from is stripped first, so it cannot satisfy this
    the way a comment satisfied a similar check before, see the project's
    own lesson on that)."""
    import inspect
    from prism_service.services import agent_runs_data
    code = _code_only(inspect.getsource(agent_runs_data.node_token_trend))
    assert "type" not in code and "kind" not in code, (
        "node_token_trend's executable body must derive its number only "
        f"from tokens/model, never a declared kind/type:\n{code}")
    # And it must never classify by matching an endpoint url (the exact
    # mistake this same project caught live on task 25b2a05c).
    assert "url" not in code and "reason-loop" not in code, code


def test_a_low_sample_outlier_node_never_skews_its_peers_baseline(tmp_path):
    """Regression for a real live finding on this same task: red_gate
    carries only 2 ceiling-passing rows at ~926,000 tokens each -- too few
    to be reported itself (indeterminate), but a naive per-SAMPLE pooled
    baseline still let those 2 rows drag the reference for every OTHER
    node, mis-reading typical agentic nodes as 3-4x instead of ~1x. The
    baseline must be the mean of each qualifying node's own average -- one
    vote per node -- so a step with too few runs to speak for itself gets
    no vote in anyone else's number either."""
    db = str(tmp_path / "scores.db")
    rows = []
    for step, avg in (("review_previous_notes", 6_468), ("draft_story", 5_922),
                       ("verify_plan", 9_440), ("write_failing_tests", 6_772),
                       ("implement_tasks", 7_513), ("verify_green_state", 7_409)):
        rows += [_row(run_id=f"{step}-{i}", step=step, tokens=avg) for i in range(20)]
    # The outlier: only 2 samples (below min_samples), each ~150x every
    # peer's typical run.
    rows += [_row(run_id=f"red_gate-{i}", step="red_gate", tokens=926_000)
             for i in range(2)]
    _seed(db, rows)

    steps = ["review_previous_notes", "draft_story", "verify_plan",
             "write_failing_tests", "implement_tasks", "verify_green_state",
             "red_gate"]
    out = node_token_trend(db, steps)

    assert out["red_gate"]["indeterminate"] is True, out["red_gate"]
    for step in steps[:-1]:
        mult = out[step]["multiplier"]
        assert 0.5 < mult < 2.0, (
            f"{step} is a typical agentic node (peer average ~7250) and "
            f"must read near 1x -- a low-sample outlier dragged it to "
            f"{mult}x: {out}")


def test_the_window_orders_by_recorded_at_never_the_mixed_format_started_at(tmp_path):
    """Regression for a real live finding on this same task (team lead,
    2026-08-30): agent_runs.started_at is stored in TWO formats on this
    project's own table -- ISO strings on rows written before 2026-08-19
    ("2026-08-18T22:54:56+00:00") and unix-epoch numbers on rows written
    since ("1788113951.8..."), both landing in the same TEXT-affinity
    column. SQLite's default text collation sorts '2' ahead of '1'
    character-by-character, so 'ORDER BY started_at DESC' reads an
    11-day-old ISO row as MORE recent than a genuinely-fresh epoch row --
    live-verified: 14 such stale rows survive among exactly the 10 step
    ids this feature reads.

    Both tokens values here are individually PLAUSIBLE (well under any
    model's ceiling) so this test exercises ordering alone, never the
    ceiling filter -- and there are more rows than NODE_TREND_WINDOW so a
    wrong sort actually evicts a genuine recent row rather than merely
    reordering ones that would all fit anyway. If a wrongly-sorted stale
    row displaces even one fresh row, the average moves measurably (a
    single 50,000-token row swapped in for a 1,000-token one shifts a
    20-sample average by ~2,450) -- this is not a coincidence a smaller
    fixture could hide."""
    db = str(tmp_path / "scores.db")
    stale_iso_row = _row(
        run_id="stale-iso", step="mixed_format_node", tokens=50_000,
        started_at="2026-08-18T22:54:56.897506+00:00",
        recorded_at="2026-06-01 00:00:00",  # genuinely old ingestion time
    )
    # 25 genuinely fresh rows (> NODE_TREND_WINDOW of 20), all plausible,
    # all epoch-formatted started_at, spread across real recorded_at times.
    fresh_epoch_rows = [
        _row(run_id=f"fresh-{i}", step="mixed_format_node", tokens=1_000,
             started_at="1788113951.84924",
             recorded_at=f"2026-08-30 18:{i:02d}:00")
        for i in range(25)
    ]
    _seed(db, [stale_iso_row] + fresh_epoch_rows)

    out = node_token_trend(db, ["mixed_format_node"])["mixed_format_node"]
    assert out["sample_count"] == 20, (
        f"the window must hold exactly NODE_TREND_WINDOW fresh rows: {out}")
    assert out["avg_tokens"] == 1_000, (
        "the stale 50,000-token row displaced a genuinely-recent row -- "
        f"the window is still influenced by started_at's mixed format: {out}")


# ----------------------------------------------------------------------
# AC-2: too few runs -> honest indeterminate, never a fabricated number.
# ----------------------------------------------------------------------


def test_a_node_with_too_few_runs_is_indeterminate_not_a_fabricated_number(tmp_path):
    db = str(tmp_path / "scores.db")
    _seed(db, [_row(run_id="only-1", step="fresh_node", tokens=500)])
    out = node_token_trend(db, ["fresh_node"])["fresh_node"]
    assert out["indeterminate"] is True, out
    assert out["avg_tokens"] is None, out
    assert out["multiplier"] is None, out
    assert out["sample_count"] == 1, out


def test_a_node_with_zero_runs_never_borrows_a_neighbours_figure(tmp_path):
    db = str(tmp_path / "scores.db")
    _seed(db, [_row(run_id=f"busy-{i}", step="busy_node", tokens=2_000)
               for i in range(10)])
    out = node_token_trend(db, ["busy_node", "silent_node"])
    assert out["busy_node"]["indeterminate"] is False
    silent = out["silent_node"]
    assert silent["indeterminate"] is True, silent
    assert silent["avg_tokens"] is None, silent
    assert silent["multiplier"] is None, silent
    assert silent["sample_count"] == 0, silent


# ----------------------------------------------------------------------
# AC-3: a pre-fix corrupt row is excluded, never aggregated.
# ----------------------------------------------------------------------


def test_a_pre_fix_impossible_row_is_excluded_from_the_average(tmp_path):
    db = str(tmp_path / "scores.db")
    # Real shape of a pre-fix row: model attached, tokens thousands of
    # times its context window (measured live on task fc471aed).
    corrupt = [_row(run_id=f"corrupt-{i}", step="haunted_node",
                     model="claude-sonnet-5", tokens=241_000_000)
               for i in range(3)]
    plausible = [_row(run_id=f"good-{i}", step="haunted_node", tokens=1_200)
                 for i in range(5)]
    _seed(db, corrupt + plausible)

    out = node_token_trend(db, ["haunted_node"])["haunted_node"]
    assert out["indeterminate"] is False, out
    # Hand check: only the 5 plausible rows may feed the average -- if the
    # corrupt rows leaked in, this average would be in the tens of millions.
    assert out["avg_tokens"] == 1_200, out
    assert out["sample_count"] == 5, out


def test_a_model_less_pre_fix_row_is_checked_against_the_global_ceiling(tmp_path):
    db = str(tmp_path / "scores.db")
    corrupt = [_row(run_id="ghost-1", step="haunted_node2", model=None,
                     cost_usd=None, tokens=2_659_518_144)]
    plausible = [_row(run_id=f"good2-{i}", step="haunted_node2", tokens=900)
                 for i in range(4)]
    _seed(db, corrupt + plausible)
    out = node_token_trend(db, ["haunted_node2"])["haunted_node2"]
    assert out["sample_count"] == 4, out
    assert out["avg_tokens"] == 900, out


# ----------------------------------------------------------------------
# AC-4: the trend window is named, and the multiplier/trend reach the
# canvas -- source-checked, since the SPA has no JS test runner (repo
# convention: UI ACs are pinned against the real TSX/TS source).
# ----------------------------------------------------------------------


def _read(*parts: str) -> str:
    path = _WEB.joinpath(*parts)
    assert path.exists(), f"expected {path} to exist"
    return path.read_text(encoding="utf-8")


def _trend_label_fn(src: str) -> str:
    """The body of tokenTrendLabel(), or a NAMED failure.

    Slicing on a bare str.index() makes an absent function raise
    `ValueError: substring not found` -- a red run that says nothing about
    the claim it is meant to carry. The absence IS the finding, so it is
    asserted with the finding's own words, and the end boundary is the
    next top-level declaration rather than one hard-coded neighbour that
    a later edit can rename out from under this test.
    """
    marker = "function tokenTrendLabel"
    assert marker in src, (
        "workflowGraph.ts declares no tokenTrendLabel() -- nothing turns a "
        "node's measured trend into text for the canvas to paint")
    body = src[src.index(marker):]
    rest = body[len(marker):]
    ends = [i for i in (rest.find("\nfunction "), rest.find("\nexport "),
                        rest.find("\nconst ")) if i != -1]
    return body[:len(marker) + min(ends)] if ends else body


def test_get_workflows_carries_the_multiplier_and_trend_per_step():
    src = (_SERVICE_ROOT / "prism_service/api/workflows.py").read_text(encoding="utf-8")
    assert "node_token_trend(" in src, (
        "get_workflows never calls the measured trend function -- the "
        "canvas would have nothing real to draw")
    for field in ("token_multiplier", "avg_tokens", "token_sample_count",
                  "token_window", "token_indeterminate"):
        assert f'step["{field}"]' in src, (
            f"get_workflows never attaches {field!r} to a step -- the SPA "
            "type carries the field but the server never fills it")


def test_the_canvas_renders_the_trend_with_a_named_window():
    src = _read("live", "workflowGraph.ts")
    label_fn = _trend_label_fn(src)
    assert "t.window" in label_fn, (
        "the on-screen label never names the window size -- an unlabelled "
        f"average invites a wrong reading:\n{label_fn}")
    # Both the determinate and indeterminate branches must name it --
    # naming the window only when there IS a number is not honest either.
    assert label_fn.count("t.window") >= 2, label_fn
    assert "drawNode" in src and "tokenTrendLabel(n.tokenTrend)" in src, (
        "drawNode never paints the trend label onto the node card")


def test_an_indeterminate_node_never_paints_a_fabricated_number():
    src = _read("live", "workflowGraph.ts")
    label_fn = _trend_label_fn(src)
    assert re.search(r"indeterminate.*return", label_fn, re.S), (
        "the indeterminate case must short-circuit before any number "
        f"formatting runs:\n{label_fn}")
    assert "×?" in label_fn, (
        "an indeterminate node must render an honest placeholder, never "
        f"a computed figure:\n{label_fn}")


def test_the_slice_never_touches_a_control_plane_policy_file():
    from prism_service.services.control_plane import POLICY_FILES
    touched = {
        "services/prism-service/prism_service/services/agent_runs_data.py",
        "services/prism-service/prism_service/api/workflows.py",
        "services/prism-service/prism_service/web/src/lib/useWorkflowDef.ts",
        "services/prism-service/prism_service/web/src/live/workflowGraph.ts",
        "services/prism-service/tests/unit/test_node_shows_multiplier_and_token_trend.py",
    }
    assert not (touched & set(POLICY_FILES)), (touched, POLICY_FILES)
