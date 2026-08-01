"""RED - a producing actor cannot post its own gate pass (task 682b7e48).

POST /api/agent-runs/ingest takes ``row: dict = Body(...)`` and writes it
through unvalidated (api/agent_runs.py:27-36 ->
services/agent_runs_data.py:36-60), so any HTTP client on the box can
assert ``gate_state="passed"`` for any task/step. The conductor's own
writer never uses that door: ConductorService._record_agent_run builds the
row in-process and calls upsert_agent_run DIRECTLY at
services/conductor_service.py:1661. That is the guard's whole foothold -
the HTTP boundary is by construction a NON-machine caller, so the refusal
never has to trust a payload field.

Drives the REAL router with a FastAPI TestClient over a REAL on-disk
scores.db, mirroring tests/unit/test_api_agent_runs.py:33-90. Nothing on
the persistence path is doubled, so a green result here is a claim about
the wire a person actually curls, not about a mock.

RED TODAY: there is no refusal helper, ingest persists a passing
gate_state verbatim and answers ok:true, a producer POST can overwrite a
genuine machine row at the same upsert key, and no gate_source annotation
exists on the read path or on the Trace tab a human reads.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_API_SRC = _SERVICE_ROOT / "prism_service" / "api" / "agent_runs.py"
_DATA_SRC = (_SERVICE_ROOT / "prism_service" / "services"
             / "agent_runs_data.py")
_TSX_SRC = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
            / "TaskDetailPage.tsx")

# The named refusal the ingest response must carry (AC-4). Shape mirrored
# by hand from same_actor_override_reason (conductor_service.py:523-541);
# that module is a POLICY_FILE (control_plane.py:48) and is deliberately
# NOT imported here.
REFUSAL_TOKEN = "producer_cannot_record_gate_verdict"

# Fields a caller sets for itself. The guard must branch on NONE of them
# (AC-2) - a workflow_name == "conductor" check blocks only the honest.
_IDENTITY_FIELDS = ("workflow_name", "session_id", "agent_id", "model")


def _seed_schema(scores_db: str) -> None:
    """Create the real scores.db schema (incl. agent_runs) the way
    production does, via Brain's CREATE TABLE block in brain_engine.py."""
    from prism_service.engines.brain_engine import Brain
    Brain(
        brain_db=str(Path(scores_db).parent / "brain.db"),
        graph_db=str(Path(scores_db).parent / "graph.db"),
        scores_db=scores_db,
    )


def _row(**kw) -> dict:
    """One ingest payload. Defaults mirror the honest producer row the
    implement.js emitter sends (.claude/workflows/implement.js:213-232):
    real timings, ok, a verdict summary, and NO gate verdict."""
    base = dict(
        run_id="run-p", workflow_name="implement", task_id="T-1",
        session_id="S-1", agent_id="agent-p", parent_agent_id=None,
        role="qa", step="write_failing_tests", model="claude-opus-5",
        started_at="2026-07-30T12:00:00+00:00",
        ended_at="2026-07-30T12:01:00+00:00", duration_ms=60000,
        tokens=1234, tool_uses=7, ok=True,
        verdict_summary="red trace captured", evidence_ref="sha:abc1234",
    )
    base.update(kw)
    return base


def _client(tmp_path, monkeypatch):
    """Mount the REAL agent_runs router over a REAL scores.db in tmp.
    get_project is patched so the router resolves scores_db via
    ctx._data_dir / 'scores.db' - the api/learning.py pattern."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import agent_runs as agent_runs_api

    scores_db = str(tmp_path / "scores.db")
    _seed_schema(scores_db)

    class _Ctx:
        _data_dir = tmp_path

    monkeypatch.setattr(agent_runs_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(agent_runs_api.router, prefix="/api/agent-runs")
    return TestClient(app), scores_db


def _get_rows(client, **params):
    got = client.get("/api/agent-runs",
                     params={"project": "prism", **params})
    assert got.status_code == 200, got.text
    return got.json()["rows"]


def _func_node(path: Path, name: str):
    """The ast node for a top-level function, or None. Parsing beats
    substring-matching: an explanatory COMMENT has satisfied a bare-name
    assertion three separate times in this repo."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == name:
                return node
    return None


def _branch_tests(node) -> list[str]:
    """Every condition the function branches on, unparsed. Covers `if`,
    ternaries and bare comparisons, which is where an identity check
    would have to live."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, (ast.If, ast.IfExp)):
            out.append(ast.unparse(sub.test))
        elif isinstance(sub, ast.Compare):
            out.append(ast.unparse(sub))
    return out


def _strip_js_comments(src: str) -> str:
    """Drop /* */ , {/* */} and // comments so a comment can never satisfy
    a source assertion (the repeated failure mode in the lessons)."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)//.*$", "", src)
    return src


def _trace_step_row_body() -> str:
    """The rendered body of TraceStepRow (TaskDetailPage.tsx:553-577),
    comments removed. Not a fixed-size window: it runs to the function's
    own closing brace at column 0."""
    src = _TSX_SRC.read_text(encoding="utf-8")
    start = src.index("function TraceStepRow(")
    end = src.index("\n}\n", start)
    return _strip_js_comments(src[start:end])


def _jsx_expression_around(body: str, needle: str) -> str:
    """The innermost {...} JSX expression container holding `needle`, by
    brace-matching - so the assertion reads the ENCLOSING branch, never a
    fixed character window around the match."""
    idx = body.index(needle)
    depth, start = 0, None
    for i in range(idx, -1, -1):
        if body[i] == "}":
            depth += 1
        elif body[i] == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
    if start is None:
        return ""
    depth = 0
    for j in range(start, len(body)):
        if body[j] == "{":
            depth += 1
        elif body[j] == "}":
            depth -= 1
            if depth == 0:
                return body[start:j + 1]
    return body[start:]


# ----------------------------------------------------------------------
# AC-1 - a passing gate_state posted over HTTP is not persisted.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("passing", [
    "passed",
    "PASSED",
    "  passed  ",
    "green_gate:passed",   # live compound form (agent_runs carries these)
    "approved",
    "green",
])
def test_passing_gate_state_from_http_is_refused(
        tmp_path, monkeypatch, passing):
    client, _ = _client(tmp_path, monkeypatch)
    post = client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="self-approve", agent_id="ag-x", step="green_gate",
                  gate_state=passing),
    )
    # 200, not 4xx: test_api_agent_runs.py:133 pins 200 on ingest and
    # test_team_boundary_hardening.py:197-205 pins 403 for an unauthorized
    # project. The content refusal must not blur the authorization contract.
    assert post.status_code == 200, post.text
    body = post.json()
    assert body.get("ok") is False, (
        f"ingest accepted a producer-written gate_state={passing!r} "
        f"(body={body}) - any HTTP caller can still assert its own pass"
    )
    rows = _get_rows(client)
    landed = [r for r in rows if r["run_id"] == "self-approve"]
    assert landed == [], (
        f"the refused row was persisted anyway: {landed}"
    )


# ----------------------------------------------------------------------
# AC-2 - the guard rests on something the caller cannot set for itself.
# ----------------------------------------------------------------------


def test_spoofed_conductor_identity_is_refused_identically(
        tmp_path, monkeypatch):
    """The exact spoof named in the ticket: a caller dressing itself as the
    machine seat. MACHINE_SEATS (conductor_service.py:198-207) are strings
    a payload can simply contain, so they must not be the guard's input."""
    client, _ = _client(tmp_path, monkeypatch)
    honest = client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="honest", agent_id="ag-h", step="green_gate",
                  gate_state="passed"),
    ).json()
    spoof = client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="spoof", workflow_name="conductor",
                  session_id="conductor-autoclear",
                  agent_id="conductor-adjudicator", model="machine",
                  step="green_gate", gate_state="passed",
                  started_at="2026-07-30T12:00:00+00:00", duration_ms=453),
    ).json()
    assert spoof.get("ok") is False, (
        f"spoofing workflow_name=conductor / session_id=conductor-autoclear "
        f"bought the caller a persisted pass: {spoof}"
    )
    assert spoof.get("refused") == honest.get("refused") == REFUSAL_TOKEN, (
        f"the spoofed caller got a different answer than the plain one "
        f"(honest={honest}, spoof={spoof}) - the guard is keying on identity"
    )
    rows = _get_rows(client)
    assert [r for r in rows if r["run_id"] in ("honest", "spoof")] == [], rows


def test_guard_does_not_key_on_caller_supplied_identity():
    """Read the DECISION, not the response shape: no branch in the ingest
    handler or the refusal helper may compare a caller-supplied identity
    field. Parsed with ast, so a comment naming the field cannot satisfy
    it and echoing agent_id back in the response body cannot trip it."""
    ingest = _func_node(_API_SRC, "ingest")
    assert ingest is not None, "api/agent_runs.py has no ingest handler"
    assert "producer_gate_verdict_reason" in ast.unparse(ingest), (
        "ingest never consults a refusal helper - a passing gate_state is "
        "still handed straight to upsert_agent_run (api/agent_runs.py:34)"
    )
    nodes = [ingest]
    for fn in ("producer_gate_verdict_reason", "is_passing_gate_state"):
        node = _func_node(_DATA_SRC, fn)
        assert node is not None, f"agent_runs_data.py has no {fn}()"
        nodes.append(node)
    for node in nodes:
        for cond in _branch_tests(node):
            for field in _IDENTITY_FIELDS:
                assert field not in cond, (
                    f"{node.name}() branches on caller-supplied {field!r} "
                    f"in `{cond}` - a spoofable string blocks only the "
                    "honest caller"
                )


# ----------------------------------------------------------------------
# AC-3 - the conductor's in-process seat is untouched. The guard belongs
# at the HTTP door; putting it in upsert_agent_run would break the only
# writer that is entitled to record a verdict.
# ----------------------------------------------------------------------


def test_in_process_writer_still_records_a_machine_pass(
        tmp_path, monkeypatch):
    from prism_service.services.agent_runs_data import (
        get_agent_runs, upsert_agent_run,
    )
    _client(tmp_path, monkeypatch)          # seeds the schema
    scores_db = str(tmp_path / "scores.db")
    # Exactly what ConductorService._record_agent_run builds in-process
    # (conductor_service.py:1643-1661): its own seat identity and
    # server-clock timings, never over HTTP.
    upsert_agent_run(scores_db, _row(
        run_id="drive-1", workflow_name="conductor",
        task_id="T-machine", session_id="conductor-adjudicator",
        agent_id="conductor-adjudicator", step="green_gate", role="sm",
        model="machine", duration_ms=453, gate_state="passed",
        verdict_summary="verified:plan_coverage",
    ))
    rows = get_agent_runs(scores_db, task_id="T-machine")
    assert len(rows) == 1, rows
    assert rows[0]["gate_state"] == "passed", (
        "the trusted in-process writer lost its verdict - the guard was "
        "put in the data layer instead of at the HTTP boundary "
        "(conductor_service.py:1661 calls upsert_agent_run directly)"
    )


# ----------------------------------------------------------------------
# AC-4 - the refusal names itself, so a driver can self-diagnose.
# ----------------------------------------------------------------------


def test_refusal_names_itself_and_says_who_owns_it(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    body = client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="named", agent_id="ag-n", step="red_gate",
                  gate_state="passed"),
    ).json()
    assert body.get("refused") == REFUSAL_TOKEN, (
        f"no machine-readable refusal token on the response: {body}"
    )
    reason = str(body.get("reason") or "")
    assert reason.strip(), (
        "the refusal carries no reason - a tooth that computes a refusal "
        "and throws it away has only half-shipped"
    )
    low = reason.lower()
    assert "conductor" in low and "gate" in low, (
        f"the reason does not say the conductor owns gate state: {reason!r}"
    )
    # Same shape as same_actor_override_reason (conductor_service.py:523-541):
    # "" when acceptable, a named sentence when refused.
    from prism_service.services.agent_runs_data import (
        producer_gate_verdict_reason,
    )
    assert producer_gate_verdict_reason(_row()) == "", (
        "the helper must return an empty reason for an acceptable row"
    )
    assert producer_gate_verdict_reason(_row(gate_state="passed")).strip()


# ----------------------------------------------------------------------
# AC-5 - the refusal is NARROW. Ordinary telemetry is untouched: the
# shared fixture at test_api_agent_runs.py:62 posts gate_state="none" on
# all 10 of its tests, and live data carries "pending"/"failed" and the
# compound "story_gate:pending".
# ----------------------------------------------------------------------


@pytest.mark.parametrize("kw,expect", [
    ({}, None),                                  # gate_state absent
    ({"gate_state": None}, None),
    ({"gate_state": "none"}, "none"),
    ({"gate_state": "pending"}, "pending"),
    ({"gate_state": "failed"}, "failed"),
    ({"gate_state": "story_gate:pending"}, "story_gate:pending"),
])
def test_non_passing_gate_states_round_trip(
        tmp_path, monkeypatch, kw, expect):
    client, _ = _client(tmp_path, monkeypatch)
    post = client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="ordinary", agent_id="ag-o", **kw),
    )
    assert post.status_code == 200, post.text
    assert post.json().get("ok") is True, (
        f"ordinary telemetry {kw} was refused: {post.json()} - the guard "
        "must be narrow to PASSING values or it breaks every normal row"
    )
    rows = [r for r in _get_rows(client) if r["run_id"] == "ordinary"]
    assert len(rows) == 1, rows
    assert rows[0]["gate_state"] == expect, (
        f"gate_state did not round-trip: sent {kw}, read back "
        f"{rows[0]['gate_state']!r}"
    )


# ----------------------------------------------------------------------
# AC-7 - a fabricated passing row never reaches the task's real gate
# state, and never shows up as a gate verdict on the surface a person
# reads (the Trace tab lozenge, TaskDetailPage.tsx:564).
# ----------------------------------------------------------------------


def test_refused_post_does_not_touch_task_gate_state(tmp_path, monkeypatch):
    from prism_service.services.agent_runs_data import build_task_trace
    from prism_service.services.task_service import TaskService

    client, scores_db = _client(tmp_path, monkeypatch)
    svc = TaskService(db_path=str(tmp_path / "tasks.db"))
    task = svc.create(title="a real ticket under drive")
    before = svc.get(task.id).gate_state

    client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="fabricated", agent_id="ag-f", step="green_gate",
                  task_id=task.id, gate_state="passed"),
    )
    assert svc.get(task.id).gate_state == before, (
        "a producer POST moved the task's real gate state"
    )
    trace = build_task_trace(scores_db, task.id)
    seen = [str(s.get("gate_state") or "").lower()
            for sess in trace["sessions"] for s in sess["steps"]]
    assert not any("pass" in g for g in seen), (
        f"the fabricated pass is rendered as a gate verdict on the Trace "
        f"tab: {seen}"
    )


# ----------------------------------------------------------------------
# AC-8 - history is preserved, not deleted. The audit trail stays
# append-only, and the refusal must not let a producer OVERWRITE a
# genuine machine row at the same (run_id, agent_id, step) upsert key.
# ----------------------------------------------------------------------


def test_no_delete_or_mutation_of_history(tmp_path, monkeypatch):
    from prism_service.services.agent_runs_data import upsert_agent_run

    client, scores_db = _client(tmp_path, monkeypatch)
    upsert_agent_run(scores_db, _row(
        run_id="hist", workflow_name="conductor", task_id="T-hist",
        session_id="conductor-adjudicator", agent_id="conductor-adjudicator",
        step="green_gate", model="machine", duration_ms=453,
        gate_state="passed", verdict_summary="verified:plan_coverage",
    ))
    post = client.post(
        "/api/agent-runs/ingest", params={"project": "prism"},
        json=_row(run_id="hist", task_id="T-hist",
                  agent_id="conductor-adjudicator", step="green_gate",
                  gate_state="passed", verdict_summary="I approve myself"),
    )
    assert post.json().get("ok") is False, post.text
    rows = [r for r in _get_rows(client) if r["run_id"] == "hist"]
    assert len(rows) == 1, rows
    assert rows[0]["verdict_summary"] == "verified:plan_coverage", (
        "a producer POST overwrote a genuine machine row through the "
        "ON CONFLICT DO UPDATE at agent_runs_data.py:50-58 - refusing the "
        "write outright is what keeps the history intact"
    )
    api_src = _API_SRC.read_text(encoding="utf-8")
    data_src = _DATA_SRC.read_text(encoding="utf-8")
    assert "@router.delete" not in api_src, "a delete route was added"
    for src in (api_src, data_src):
        assert "DELETE FROM agent_runs" not in src, "history is deleted"
    assert data_src.count("UPDATE agent_runs") == 1, (
        "a new mutating statement was added; the only permitted UPDATE is "
        "the pre-existing token backfill at agent_runs_data.py:225-229"
    )


# ----------------------------------------------------------------------
# AC-9 - a reader can tell a genuine machine decision from a
# producer-written row WITHOUT inspecting null timings by hand. The 68
# historical implement/passed rows stay readable and get annotated.
# ----------------------------------------------------------------------


def test_gate_source_marks_machine_and_unattributed(tmp_path, monkeypatch):
    from prism_service.services.agent_runs_data import (
        build_task_trace, get_agent_runs, upsert_agent_run,
    )
    _client(tmp_path, monkeypatch)
    scores_db = str(tmp_path / "scores.db")
    upsert_agent_run(scores_db, _row(
        run_id="g", workflow_name="conductor", task_id="T-src",
        session_id="conductor-adjudicator", agent_id="conductor-adjudicator",
        step="green_gate", model="machine",
        started_at="2026-07-30T10:00:00+00:00", duration_ms=453,
        gate_state="passed",
    ))
    # A historical producer-written row: passing verdict, no server-clock
    # signature (all 68 live rows have model=None and started_at=None).
    upsert_agent_run(scores_db, _row(
        run_id="p", workflow_name="implement", task_id="T-src",
        session_id="", agent_id="ag-p", step="red_gate", model=None,
        started_at=None, ended_at=None, duration_ms=None,
        gate_state="passed",
    ))
    steps = {s["step"]: s for sess in build_task_trace(scores_db, "T-src")
             ["sessions"] for s in sess["steps"]}
    assert steps["green_gate"].get("gate_source") == "machine", steps
    assert steps["red_gate"].get("gate_source") == "unattributed", (
        f"a producer-written pass is indistinguishable from a machine "
        f"decision on the Trace tab: {steps}"
    )
    rows = {r["step"]: r for r in get_agent_runs(scores_db, task_id="T-src")}
    assert rows["green_gate"].get("gate_source") == "machine", rows
    assert rows["red_gate"].get("gate_source") == "unattributed", rows


def test_trace_tab_renders_the_gate_source_distinction():
    """AC-9's UI half. The PRISM SPA has no JS test runner, so a UI AC is
    pinned by asserting the ACTUAL TSX source (the convention documented
    at tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6).

    A derived field the UI never draws is the unreachable-affordance
    misfire, so this asserts the RENDERED <Lozenge tag inside the branch
    that reads gate_source - with every comment stripped first, because an
    explanatory comment has satisfied this kind of assertion three times."""
    whole = _TSX_SRC.read_text(encoding="utf-8")
    trace_step_type = whole[whole.index("type TraceStep ="):
                            whole.index("type TraceSession")]
    assert "gate_source" in _strip_js_comments(trace_step_type), (
        "the TraceStep type never carries gate_source, so the annotation "
        f"cannot reach the row renderer: {trace_step_type}"
    )
    body = _trace_step_row_body()
    assert "gate_source" in body, (
        "TraceStepRow never reads step.gate_source - the distinction is "
        "invisible to the person reading the Trace tab "
        "(TaskDetailPage.tsx:553-577)"
    )
    branch = _jsx_expression_around(body, "gate_source")
    assert branch, "gate_source sits outside any JSX expression container"
    assert "<Lozenge" in branch, (
        "the gate_source branch renders no <Lozenge - asserting the "
        f"constant instead of the affordance a person sees: {branch!r}"
    )
    assert ("===" in branch or "!==" in branch or "&&" in branch), (
        f"the gate_source JSX expression is not a branch: {branch!r}"
    )
