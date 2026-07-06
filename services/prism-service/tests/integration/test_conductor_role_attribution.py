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
