"""Conductor role/model/token attribution on advance_task.

The role/tier engine makes every SDLC transition write an agent_runs row
attributing the work to a canonical ROLE (role_for_step of the step),
the MODEL the driver reported, and the TOKENS windowed from the
transcript (0 when no transcript exists in a test). This pins that seam:
drive a task through a couple of transitions carrying model="test-model-x"
and assert the agent_runs spine captured role + model + tokens.

May be RED until the conductor attribution write lands (advance_task
grows a `model=` param and stamps agent_runs). The contract asserted
here is correct against the described feature.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _services(tmp_path):
    """Brain seeds the scores.db schema (incl. agent_runs) the way
    production does; ConductorService + TaskService share that scores.db
    so an attribution write is readable back through agent_runs_data."""
    from prism_service.engines.brain_engine import Brain
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=scores_db,
    )
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(
        scores_db, enable_engine=False, task_svc=task_svc,
    )
    return task_svc, cond, scores_db


# The step LEFT/entered across the first two transitions. Both interpretations
# (attribute the step just left vs. just entered) cover review_previous_notes:
# it is entered on advance #1 and left on advance #2. role_for_step -> 'sm'.
_ATTRIBUTED_STEP = "review_previous_notes"


def test_advance_writes_agent_run_with_role_model_tokens(tmp_path):
    from prism_service.models import roles
    from prism_service.services.agent_runs_data import get_agent_runs

    task_svc, cond, scores_db = _services(tmp_path)
    t = task_svc.create(title="attribute the driver")
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this role/model/token attribution walk (unrelated to premise
    # content) can leave review_previous_notes.
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising role/model/token "
        "attribution, not a real premise claim - UNVERIFIED\n"))

    # Two transitions so a real (non-empty) step is both entered and left.
    r1 = cond.advance_task(t.id, session_id="S-attr", model="test-model-x")
    assert r1["ok"] is True, r1
    r2 = cond.advance_task(t.id, session_id="S-attr", model="test-model-x")
    assert r2["ok"] is True, r2

    rows = get_agent_runs(scores_db, task_id=t.id)
    assert rows, "no agent_runs row written for a driven task"

    attributed = [r for r in rows if r["step"] == _ATTRIBUTED_STEP]
    assert attributed, (
        f"no agent_runs row for step {_ATTRIBUTED_STEP!r}; got "
        f"steps={sorted(r['step'] for r in rows)}"
    )
    row = attributed[0]
    assert row["role"] == roles.role_for_step(_ATTRIBUTED_STEP) == "sm", row
    assert row["model"] == "test-model-x", row
    # tokens windowed from the transcript — absent in tests -> 0 is fine,
    # but it must be a concrete int, never NULL.
    assert row["tokens"] is not None, "tokens must not be NULL"
    assert isinstance(row["tokens"], int) and row["tokens"] >= 0, row


def test_every_written_run_role_matches_its_step(tmp_path):
    """Whatever rows the attribution write emits, each must carry the
    canonical role for its own step — no divergent per-call labelling."""
    from prism_service.models import roles
    from prism_service.services.agent_runs_data import get_agent_runs

    task_svc, cond, scores_db = _services(tmp_path)
    t = task_svc.create(title="role matches step")
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this walk (unrelated to premise content) can leave
    # review_previous_notes.
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising role/model/token "
        "attribution, not a real premise claim - UNVERIFIED\n"))
    for _ in range(3):
        res = cond.advance_task(t.id, session_id="S-attr", model="test-model-x")
        if not res.get("ok"):
            break

    rows = get_agent_runs(scores_db, task_id=t.id)
    assert rows, "no agent_runs rows written"
    for r in rows:
        assert r["role"] == roles.role_for_step(r["step"]), (
            f"row for step {r['step']!r} has role {r['role']!r}, "
            f"expected {roles.role_for_step(r['step'])!r}"
        )


def test_per_role_aggregate_groups_the_roles(tmp_path):
    """get_agent_run_aggregates rolls the attribution up per role so the
    /learning 'token cost per role' panel has data grouped by role id."""
    from prism_service.services.agent_runs_data import get_agent_run_aggregates

    task_svc, cond, scores_db = _services(tmp_path)
    t = task_svc.create(title="per-role rollup")
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this walk (unrelated to premise content) can leave
    # review_previous_notes.
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising role/model/token "
        "attribution, not a real premise claim - UNVERIFIED\n"))
    for _ in range(3):
        res = cond.advance_task(t.id, session_id="S-attr", model="test-model-x")
        if not res.get("ok"):
            break

    agg = get_agent_run_aggregates(scores_db)
    assert agg["total_runs"] >= 1, agg
    per_role = {r["role"] for r in agg["per_role"]}
    assert "sm" in per_role, (
        f"Steward-owned steps were driven but 'sm' missing from per_role: {agg}"
    )


# ---------------------------------------------------------------------------
# Task 67b4b2f6 — the ORACLE reads the TRACE PAYLOAD, not get_agent_runs.
#
# oracle: "GET /api/tasks/<id>/trace?project=prism returns sessions[] where
# each step entry has a non-empty step, a non-empty role that equals
# role_for_step of that step, a non-empty model, and a token count. The sum
# of the row token counts equals totals.tokens."
#
# The three tests above stop at the get_agent_runs spine. These pin the
# payload the route (api/tasks.py:1017 -> agent_runs_data.build_task_trace)
# actually hands the Trace tab (TaskDetailPage.tsx:1390).
#
# The live trace of THIS task at 7.13.239 already breaks the non-empty-model
# clause:
#   {"step":"plan_gate","role":"sm","model":null,"tokens":0,...}
#   (session "qa-adjudicator-session")
# A seat that reports no model leaves conductor_service._record_agent_run
# (line 1858) passing model=None straight to upsert_agent_run, so the row —
# and the tab — show a BLANK attribution instead of naming who ran the step.
# The tests below reproduce that hermetically.
# ---------------------------------------------------------------------------


def _drive(cond, task_id, times=3, model="test-model-x"):
    """Advance the task `times` times, stopping at the first refusal."""
    for _ in range(times):
        res = cond.advance_task(task_id, session_id="S-attr", model=model)
        if not res.get("ok"):
            break


def _seeded_task(task_svc, title):
    t = task_svc.create(title=title)
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising role/model/token "
        "attribution, not a real premise claim - UNVERIFIED\n"))
    return t


def test_trace_step_rows_carry_the_four_oracle_fields(tmp_path):
    """AC-1/AC-2/AC-3: every step entry the Trace tab renders names its
    step, the canonical role for that step, a NON-EMPTY model, and an
    integer token count."""
    from prism_service.models import roles
    from prism_service.services.agent_runs_data import build_task_trace

    task_svc, cond, scores_db = _services(tmp_path)
    t = _seeded_task(task_svc, "trace rows carry the oracle fields")
    # A seat that reports no model — exactly what the live plan_gate row on
    # task 67b4b2f6 did — must still be attributed by name, never as null.
    # Two model-less advances: the first enters review_previous_notes, the
    # second LEAVES it, so the row that seat stamps is the one the tab shows.
    cond.advance_task(t.id, session_id="S-attr", model=None)
    cond.advance_task(t.id, session_id="S-attr", model=None)
    _drive(cond, t.id, times=1)

    trace = build_task_trace(scores_db, t.id)
    entries = [st for s in trace["sessions"] for st in s["steps"]]
    assert entries, f"no trace rows for a driven task: {trace}"
    for st in entries:
        assert st["step"], f"trace row with an empty step: {st}"
        assert st["role"] == roles.role_for_step(st["step"]), (
            f"row for step {st['step']!r} has role {st['role']!r}, "
            f"expected {roles.role_for_step(st['step'])!r}"
        )
        assert st["model"], (
            f"trace row for step {st['step']!r} reports a blank model "
            f"({st['model']!r}); a step whose driver named no model must be "
            f"attributed by name, never as null: {st}"
        )
        assert isinstance(st["tokens"], int) and st["tokens"] >= 0, st


def test_trace_totals_equal_the_sum_of_its_row_tokens(tmp_path):
    """AC-4: totals.tokens is the sum of the rows a person can see — no
    row is counted that the tab does not show, and none is dropped."""
    from prism_service.services.agent_runs_data import build_task_trace

    task_svc, cond, scores_db = _services(tmp_path)
    t = _seeded_task(task_svc, "trace totals equal the rows")
    _drive(cond, t.id, times=3)

    trace = build_task_trace(scores_db, t.id)
    entries = [st for s in trace["sessions"] for st in s["steps"]]
    assert entries, f"no trace rows for a driven task: {trace}"
    assert sum(st["tokens"] for st in entries) == trace["totals"]["tokens"], (
        f"row tokens {[st['tokens'] for st in entries]} do not sum to "
        f"totals.tokens {trace['totals']['tokens']}: {trace}"
    )
    assert trace["totals"]["steps"] == len(entries), trace


def test_a_step_reported_without_a_model_is_named_not_null(tmp_path):
    """AC-6: the write seam records the honest absence. A driver that
    reports no model gets a NAMED attribution ('unreported'), so the
    Trace tab can say who ran the step instead of showing a blank cell."""
    from prism_service.services.agent_runs_data import get_agent_runs

    task_svc, cond, scores_db = _services(tmp_path)
    t = _seeded_task(task_svc, "an unreported model is named")
    cond.advance_task(t.id, session_id="S-attr", model=None)
    cond.advance_task(t.id, session_id="S-attr", model=None)

    rows = get_agent_runs(scores_db, task_id=t.id)
    assert rows, "no agent_runs row written for a driven task"
    blank = [r for r in rows if not r["model"]]
    assert not blank, (
        f"{len(blank)} agent_runs row(s) written with a blank model: "
        f"{[(r['step'], r['model']) for r in blank]}"
    )
