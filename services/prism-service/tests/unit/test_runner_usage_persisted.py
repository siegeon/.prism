"""Per-run `claude -p` usage is CARRIED into the agent_runs row (task 9a51e670).

`inference/claude_cli._usage_from_result` already lifts the run's authoritative
usage off the single ``type=="result"`` stream event. Every consumer then
dropped it: `task_runner` read only `final_text()`/`run_id`, and the one
conductor-path writer `_record_agent_run` re-derived tokens by replaying a
Claude transcript for the reported session id. A bot reports under the SEAT
NAME ``prism-task-runner``, which is not a session UUID and has no transcript,
so its row was pinned at ``tokens=0`` by construction -- not by a misconfigured
transcript directory.

These tests pin the repair: stop re-deriving, start carrying.

  * the runner's own usage reaches the row (tokens + model + cost_usd);
  * usage comes from the RESULT event only, never summed across stream chunks
    (the task's declared stop_if);
  * the transcript path still serves real human sessions, and is consulted
    ONLY when no usage was carried;
  * a bot step publishes a `tokens.turn` so /live meters it with no human
    session attached, with a MONOTONIC per-session running total;
  * `cost_usd` lands as one column on the EXISTING agent_runs table -- no new
    table (the task's other stop_if) -- and reaches the Trace read surface.
"""
from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# Mirrors a real manifest line observed from a live task-runner step on this
# very task (run 1a50eaef594a: 8915 tokens, $1.670074, claude-fable-5).
USAGE = {
    "input_tokens": 7000,
    "output_tokens": 1915,
    "cache_read_input_tokens": 400,
    "cache_creation_input_tokens": 100,
    "cost_usd": 1.670074,
    "model": "claude-fable-5",
}
EXPECTED_TOKENS = USAGE["input_tokens"] + USAGE["output_tokens"]  # 8915
SEAT = "prism-task-runner"


@pytest.fixture()
def make_task():
    """A task under a fresh throwaway project, with its REAL per-task git
    workspace torn down afterwards (mirrors the headless-drive fixture)."""
    from prism_service.project_context import get_project
    from prism_service.services import task_workspace as tw

    created: list[str] = []

    def _make(**kwargs):
        project = "usage-" + uuid.uuid4().hex[:8]
        ctx = get_project(project)
        task = ctx.task_svc.create(title=kwargs.pop("title", "usage task"),
                                   **kwargs)
        created.append(task.id)
        return ctx, task, project

    yield _make

    for task_id in created:
        tw.remove_workspace(task_id)


class _FakeResult:
    """A ClaudeCliResult stand-in that carries usage, as the real one does."""

    def __init__(self, text: str, usage: dict | None = None,
                 exit_code: int = 0, run_id: str = "run-usage") -> None:
        self._text = text
        self.exit_code = exit_code
        self.run_id = run_id
        self.usage = dict(usage or {})

    def final_text(self) -> str:
        return self._text


def _fake_invoke(calls, usage, text="## Premises\n- ok - UNVERIFIED\n"):
    def _invoke(prompt, **kwargs):
        calls.append({"prompt": prompt, **kwargs})
        return _FakeResult(text, usage=usage)
    return _invoke


def _scores_db(project: str) -> str:
    from prism_service.project_context import get_project
    return str(get_project(project)._data_dir / "scores.db")


def _rows(project: str, task_id: str) -> list[dict]:
    from prism_service.services.agent_runs_data import get_agent_runs
    return get_agent_runs(_scores_db(project), task_id=task_id)


async def _drain_after(body) -> list[dict]:
    """Subscribe to the REAL bus, run `body`, return whatever landed."""
    from prism_service.events import bus

    q = bus.subscribe()
    try:
        body()
        await asyncio.sleep(0)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        return events
    finally:
        bus.unsubscribe(q)


# ---------------------------------------------------------------------------
# AC-1 -- the runner's own usage reaches the row (the task's pinned verify)
# ---------------------------------------------------------------------------

def test_task_runner_writes_invoke_usage(make_task, monkeypatch):
    """A task-runner-driven step lands an agent_runs row carrying the run's
    OWN token count and model -- not a transcript-derived zero."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                  project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    calls: list[dict] = []
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke(calls, USAGE))

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res
    assert len(calls) == 1, "one step must invoke claude exactly once"

    rows = _rows(project, task.id)
    assert rows, "a driven step must leave an agent_runs row"
    row = rows[0]
    assert row["session_id"] == SEAT, row
    assert row["tokens"] == EXPECTED_TOKENS, (
        f"the row must carry the run's own token count "
        f"({EXPECTED_TOKENS}); got {row['tokens']!r} -- this is the "
        "captured-then-dropped bug")
    assert row["model"] == USAGE["model"], (
        f"the model that actually ran must reach the row; got {row['model']!r}")


def test_runner_row_carries_cost_usd(make_task, monkeypatch):
    """The run's authoritative total_cost_usd is persisted too, so the owner
    can query dollar cost per step per bot."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                  project=project)
    ctx.task_svc.update(task.id, status="in_progress")
    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke([], USAGE))

    tr.run_one_step(project, task.id)

    row = _rows(project, task.id)[0]
    assert row.get("cost_usd") == pytest.approx(USAGE["cost_usd"]), row


def test_missing_usage_never_crashes_the_drive(make_task, monkeypatch):
    """A result WITHOUT usage (older fakes, a crashed run) must still drive
    the step -- telemetry is best-effort and never breaks a transition."""
    from prism_service.api import conductor_flow as cf
    from prism_service.inference import claude_cli
    from prism_service.services import task_runner as tr

    ctx, task, project = make_task()
    cf.flow_start(cf.Ident(task_id=task.id, session_id="human"),
                  project=project)
    ctx.task_svc.update(task.id, status="in_progress")

    class _NoUsage(_FakeResult):
        def __init__(self):
            super().__init__("## Premises\n- ok - UNVERIFIED\n")
            del self.usage  # not even the attribute

    monkeypatch.setattr(claude_cli, "invoke",
                        lambda prompt, **kw: _NoUsage())

    res = tr.run_one_step(project, task.id)
    assert res["ok"] is True, res
    assert ctx.task_svc.get(task.id).workflow_step == "draft_story"


# ---------------------------------------------------------------------------
# AC-2 -- RESULT event only, never a sum across stream chunks (stop_if)
# ---------------------------------------------------------------------------

def test_usage_is_result_event_not_chunk_sum():
    """The identical usage object recurs on many stream chunks; only the
    single result event is authoritative. Summing multi-counts the turn."""
    from prism_service.inference.claude_cli import _usage_from_result

    chunk = {"type": "assistant",
             "usage": {"input_tokens": 7000, "output_tokens": 1915}}
    parsed = [chunk, chunk, chunk, {
        "type": "result",
        "usage": {"input_tokens": 7000, "output_tokens": 1915,
                  "cache_read_input_tokens": 400,
                  "cache_creation_input_tokens": 100},
        "total_cost_usd": 1.670074,
        "modelUsage": {"claude-fable-5": {"inputTokens": 7000}},
    }]

    got = _usage_from_result(parsed)
    assert got["input_tokens"] == 7000, "must be the result event's, not 4x"
    assert got["output_tokens"] == 1915
    assert got["cost_usd"] == pytest.approx(1.670074)
    assert got["model"] == "claude-fable-5"


def test_carry_path_never_walks_per_chunk_events():
    """Structural pin on the stop_if: the carry path reads the usage dict the
    primitive already computed. Neither file may walk the per-chunk stream
    event list, which is the only way a chunk sum could reappear."""
    for rel in ("prism_service/services/task_runner.py",
                "prism_service/services/conductor_service.py"):
        src = (_SERVICE_ROOT / rel).read_text(encoding="utf-8")
        assert "parsed_events" not in src, (
            f"{rel} must not walk per-chunk stream events; usage comes from "
            "InvokeResult.usage (the RESULT event) only")


# ---------------------------------------------------------------------------
# AC-3 -- the transcript path still serves humans, and only when needed
# ---------------------------------------------------------------------------

def test_transcript_used_without_usage_and_skipped_with_it(make_task,
                                                           monkeypatch):
    """Exclusivity, both directions. A human report carries no usage and must
    still be attributed from its live transcript exactly as before; a carried
    usage must NOT also consult the transcript (double-counting is this
    task's named likely_misfire)."""
    from prism_service.services import claude_transcripts

    ctx, task, project = make_task()
    svc = ctx.conductor_svc

    seen: list[str] = []

    def _fake_events(session_id, *a, **k):
        seen.append(session_id)
        return []

    monkeypatch.setattr(claude_transcripts, "live_token_events_for_session",
                        _fake_events)

    svc._record_agent_run(task.id, "implement_tasks", "S-human", usage=None)
    assert seen == ["S-human"], (
        "a step reported WITHOUT usage must still be attributed from its "
        f"transcript; transcript lookups seen: {seen!r}")

    seen.clear()
    svc._record_agent_run(task.id, "implement_tasks", SEAT, usage=USAGE)
    assert seen == [], (
        "a step whose usage was CARRIED must not also replay a transcript "
        f"— that is the double-count misfire; lookups seen: {seen!r}")


# ---------------------------------------------------------------------------
# AC-4 -- a bot step moves the /live meter with no human session attached
# ---------------------------------------------------------------------------

def test_bot_step_publishes_tokens_turn(make_task):
    """The write path publishes a tokens.turn shaped exactly like the work
    ticker's, so /live meters bot work with no UI change."""
    from prism_service.routes.sse import _WORK_EVENT_TYPES
    from prism_service.services import work_stream  # noqa: F401  (shape ref)

    ctx, task, project = make_task()
    svc = ctx.conductor_svc

    events = asyncio.run(_drain_after(
        lambda: svc._record_agent_run(task.id, "implement_tasks", SEAT,
                                      usage=USAGE)))

    turns = [e for e in events if e.get("type") == "tokens.turn"]
    assert len(turns) == 1, (
        f"a bot step must publish exactly one tokens.turn; got {events!r}")
    turn = turns[0]
    assert turn["type"] in _WORK_EVENT_TYPES, (
        "a type outside _WORK_EVENT_TYPES is filtered off /sse/work entirely")
    assert turn["session_id"] == SEAT
    assert turn["task_id"] == task.id
    assert turn["out_tokens"] == USAGE["output_tokens"]
    assert turn["tokens_total"] >= turn["out_tokens"]
    # Field-for-field the work ticker's shape (work_stream.py:154-165), so the
    # existing graphState consumer needs no change.
    assert set(turn) == {
        "project", "type", "task_id", "parent_id", "session_id",
        "out_tokens", "dt_s", "tok_s", "tokens_total", "ts",
    }, sorted(turn)


def test_tokens_turn_total_is_monotonic_across_steps(make_task):
    """graphState.ts:1057 DISCARDS any update whose tokens_total is lower than
    the node already holds. So the published total must be the session's
    RUNNING total; a per-step figure would make step 2 vanish from /live."""
    ctx, task, project = make_task()
    svc = ctx.conductor_svc

    first = asyncio.run(_drain_after(
        lambda: svc._record_agent_run(task.id, "write_failing_tests", SEAT,
                                      usage=USAGE)))
    second = asyncio.run(_drain_after(
        lambda: svc._record_agent_run(task.id, "implement_tasks", SEAT,
                                      usage=USAGE)))

    t1 = [e for e in first if e.get("type") == "tokens.turn"][0]
    t2 = [e for e in second if e.get("type") == "tokens.turn"][0]
    assert t2["tokens_total"] > t1["tokens_total"], (
        "tokens_total must accumulate across a session's steps "
        f"({t1['tokens_total']} -> {t2['tokens_total']}), or /live silently "
        "drops the later step")


# ---------------------------------------------------------------------------
# AC-5 -- cost_usd is ONE column on the EXISTING table, and reaches the read
# ---------------------------------------------------------------------------

_OLD_AGENT_RUNS_DDL = """
    CREATE TABLE agent_runs (
        run_id TEXT NOT NULL, workflow_name TEXT, task_id TEXT,
        session_id TEXT, agent_id TEXT NOT NULL, parent_agent_id TEXT,
        role TEXT, step TEXT NOT NULL, model TEXT, started_at TEXT,
        ended_at TEXT, duration_ms INTEGER, tokens INTEGER, tool_uses INTEGER,
        ok INTEGER, gate_state TEXT, verdict_summary TEXT, evidence_ref TEXT,
        recorded_at TEXT DEFAULT (datetime('now')),
        PRIMARY KEY (run_id, agent_id, step)
    );
"""


def test_cost_usd_column_added_idempotently_to_existing_db(tmp_path):
    """A scores.db written at the OLD schema gains the column in place: data
    survives, the migration is a no-op the second time, and NO new table
    appears (the task's stop_if)."""
    from prism_service.services import agent_runs_data as ard

    db = str(tmp_path / "scores.db")
    conn = sqlite3.connect(db)
    conn.executescript(_OLD_AGENT_RUNS_DDL)
    conn.execute(
        "INSERT INTO agent_runs (run_id, agent_id, step, tokens) "
        "VALUES ('legacy-run', 'legacy-agent', 'implement', 4242)")
    conn.commit()
    conn.close()

    # Run the connect path twice; each time as if this process had never seen
    # the file, so the migration itself is exercised repeatedly.
    for _ in range(2):
        ard._SCHEMA_READY.discard(db)
        ard._connect(db).close()

    conn = sqlite3.connect(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(agent_runs)")]
        assert "cost_usd" in cols, f"cost_usd not added; columns: {cols}"
        assert cols.count("cost_usd") == 1, f"column added twice: {cols}"
        kept = conn.execute(
            "SELECT tokens FROM agent_runs WHERE run_id='legacy-run'"
        ).fetchone()
        assert kept and kept[0] == 4242, "pre-existing row must survive"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == {"agent_runs"}, (
            f"no NEW table may be introduced (stop_if); found {tables}")
    finally:
        conn.close()


def test_trace_returns_cost_per_step_and_task_total(tmp_path):
    """The Trace read surface returns per-run cost and a task total, so the
    owner can query cost per step per bot. /api/tasks/{id}/trace returns this
    payload verbatim (api/tasks.py:846)."""
    from prism_service.services.agent_runs_data import (
        build_task_trace,
        upsert_agent_run,
    )

    db = str(tmp_path / "scores.db")
    base = dict(
        workflow_name="conductor", task_id="T-cost", session_id=SEAT,
        parent_agent_id=None, role="dev", model="claude-fable-5",
        ended_at="2026-08-18T12:01:00+00:00", duration_ms=60000,
        tool_uses=None, ok=True, gate_state="none", verdict_summary=None,
        evidence_ref=None,
    )
    upsert_agent_run(db, dict(base, run_id="r1", agent_id="a1",
                              step="write_failing_tests", tokens=8915,
                              cost_usd=1.670074,
                              started_at="2026-08-18T12:00:00+00:00"))
    upsert_agent_run(db, dict(base, run_id="r2", agent_id="a2",
                              step="implement_tasks", tokens=4000,
                              cost_usd=0.75,
                              started_at="2026-08-18T12:05:00+00:00"))

    trace = build_task_trace(db, "T-cost")
    steps = trace["sessions"][0]["steps"]
    assert [s["step"] for s in steps] == ["write_failing_tests",
                                          "implement_tasks"]
    assert steps[0]["cost_usd"] == pytest.approx(1.670074), steps[0]
    assert steps[1]["cost_usd"] == pytest.approx(0.75), steps[1]
    assert trace["totals"]["cost_usd"] == pytest.approx(1.670074 + 0.75), (
        trace["totals"])
    # The token invariant the module already guarantees still holds.
    assert trace["totals"]["tokens"] == 8915 + 4000
