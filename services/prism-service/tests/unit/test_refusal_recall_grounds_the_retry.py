"""A refused write_failing_tests draft tells the next attempt why
(task 08e666ff).

THE DEFECT. `workflow_step_reason_loop`'s test_drafted rubric can refuse a
draft (unresolvable import, a pinned test id left undefined) and
`_dispatch_declared_steps` already stops the chain and records the refusal
text as an agent_runs row (task_runner._record_codified_run) -- but nothing
reads that row back. The next attempt at write_failing_tests calls
reason-loop with the SAME static prompt from write-failing-tests-loop.json,
so the model never learns why its last draft was refused and can return the
identical bad draft again (observed on task bb3d1f6a).

THE FIX. A new codified node, `refusal-recall`, declared between `gather`
and `loop` in write-failing-tests-loop.json. It reads back the most recent
`reason-loop` agent_runs row for the task; when that row is a refused
test_drafted verdict, it returns the raw reason plus a fully-framed block
telling the model exactly what to fix. A later PASSING draft self-clears
it -- there is no manual clearing path anywhere.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_NODE_PATH = (_REPO_ROOT / ".prism" / "behaviors" / "conductor"
              / "write-failing-tests-loop.json")


def _node() -> dict:
    return json.loads(_NODE_PATH.read_text(encoding="utf-8"))


def _routes(doc: dict) -> list[str]:
    return [(s.get("url") or "").split("/steps/")[-1].split("?")[0]
            for s in doc.get("steps") or []]


# ----------------------------------------------------------------------
# THE DECLARATION: recall sits between gather and loop; loop interpolates
# the block it exports.
# ----------------------------------------------------------------------

def test_the_node_version_moved():
    assert _node()["version"] >= 7, (
        "the declaration changed (a new recall step) and the node "
        "version did not move")


def test_refusal_recall_is_declared_between_gather_and_loop():
    doc = _node()
    routes = _routes(doc)
    assert "refusal-recall" in routes, routes
    assert routes.index("context-enrich") < routes.index("refusal-recall"), (
        f"recall must run AFTER gather (context-enrich): {routes!r}")
    assert routes.index("refusal-recall") < routes.index("reason-loop"), (
        f"recall must run BEFORE the loop (reason-loop): {routes!r}")


def test_recall_step_carries_the_task_id():
    doc = _node()
    step = next(s for s in doc["steps"]
                if "refusal-recall" in (s.get("url") or ""))
    body = json.loads(step["body"])
    assert body.get("task_id") == "${taskId}", body


def test_every_inner_body_parses_as_json():
    """Guards against a hand-edit that breaks the outer file's own JSON
    escaping -- every step's body string must ALSO parse."""
    doc = _node()
    for step in doc["steps"]:
        json.loads(step["body"])  # raises on malformed JSON


def test_loop_prompt_interpolates_the_refusal_block():
    """UPDATED (owner 2026-09-13/14, pre-red multiplier blocks): the loop
    step's own body no longer carries a static prompt -- it interpolates
    ${prompt}, filled by the new "compose" node (red-prompt-compose) a
    step earlier. refusalBlock now flows INTO that compose step (asserted
    below), same as scaffoldBlock -- see
    test_scaffold_grounds_the_draft_in_real_signatures.py's matching
    update. ${verify}/${brainContext} are superseded (by targetsBlock,
    and dropped respectively -- see that same test's docstring); ${oracle}
    is now threaded into compose directly rather than into the loop step."""
    doc = _node()
    loop_step = next(s for s in doc["steps"]
                     if "reason-loop" in (s.get("url") or ""))
    loop_body = json.loads(loop_step["body"])
    assert loop_body.get("prompt") == "${prompt}"

    compose_step = next(s for s in doc["steps"]
                        if "red-prompt-compose" in (s.get("url") or ""))
    compose_body = json.loads(compose_step["body"])
    assert compose_body.get("refusal_block", "").startswith("${"), (
        "compose step must thread refusal_block from recall: "
        f"{compose_body!r}")
    assert compose_body.get("oracle", "").startswith("${")


# ----------------------------------------------------------------------
# THE ENDPOINT: reads back the most recent reason-loop row for the task.
# ----------------------------------------------------------------------

def _seed_reason_loop_row(db: Path, task_id: str, *, ok: bool,
                          summary: str, started_at: float) -> None:
    from prism_service.services import agent_runs_data

    agent_runs_data.upsert_agent_run(str(db), {
        "run_id": f"run-{started_at}", "workflow_name": "implement",
        "task_id": task_id, "agent_id": "conductor", "role": "sm",
        "step": "reason-loop", "model": "codified", "tokens": 0,
        "cost_usd": 0.0, "ok": 1 if ok else 0,
        "started_at": started_at, "ended_at": started_at,
        "duration_ms": 0, "verdict_summary": summary[:500],
    })


@pytest.fixture(autouse=True)
def _hermetic_scores_db(monkeypatch, tmp_path):
    from prism_service.services import task_runner

    db = tmp_path / "scores.db"
    monkeypatch.setattr(task_runner, "_scores_db_for", lambda project: str(db))
    return db


def test_no_prior_attempt_reports_no_refusal():
    from prism_service.api import workflows as wf

    resp = wf.workflow_step_refusal_recall(
        wf.RefusalRecallRequest(task_id="t-never-drafted"), project="prism")

    assert resp.refusal_reason == ""
    assert resp.refusal_block == ""


def test_the_most_recent_refusal_is_recalled(_hermetic_scores_db):
    from prism_service.api import workflows as wf

    reason = ("test_drafted: imports unresolvable module(s): "
              "prism_service.lexicon")
    _seed_reason_loop_row(
        _hermetic_scores_db, "t-1", ok=False, summary=reason,
        started_at=1000.0)

    resp = wf.workflow_step_refusal_recall(
        wf.RefusalRecallRequest(task_id="t-1"), project="prism")

    assert resp.refusal_reason == reason
    assert reason in resp.refusal_block
    assert "refused" in resp.refusal_block.lower()
    assert "fix" in resp.refusal_block.lower()


def test_a_later_passing_draft_self_clears_the_refusal(_hermetic_scores_db):
    """No manual clearing anywhere -- a later PASS is what retires it."""
    from prism_service.api import workflows as wf

    _seed_reason_loop_row(
        _hermetic_scores_db, "t-2", ok=False,
        summary="test_drafted: imports unresolvable module(s): prism",
        started_at=1000.0)
    _seed_reason_loop_row(
        _hermetic_scores_db, "t-2", ok=True,
        summary="ran as a declared step", started_at=2000.0)

    resp = wf.workflow_step_refusal_recall(
        wf.RefusalRecallRequest(task_id="t-2"), project="prism")

    assert resp.refusal_reason == "", (
        "a later passing draft must self-clear the earlier refusal")
    assert resp.refusal_block == ""


def test_a_refusal_from_a_different_task_is_never_recalled(_hermetic_scores_db):
    from prism_service.api import workflows as wf

    _seed_reason_loop_row(
        _hermetic_scores_db, "t-other", ok=False,
        summary="test_drafted: imports unresolvable module(s): prism",
        started_at=1000.0)

    resp = wf.workflow_step_refusal_recall(
        wf.RefusalRecallRequest(task_id="t-mine"), project="prism")

    assert resp.refusal_reason == ""
    assert resp.refusal_block == ""


def test_no_task_id_reports_no_refusal():
    from prism_service.api import workflows as wf

    resp = wf.workflow_step_refusal_recall(
        wf.RefusalRecallRequest(task_id=""), project="prism")

    assert resp.refusal_reason == ""
    assert resp.refusal_block == ""


def test_a_broken_recall_degrades_to_no_refusal_never_raises(monkeypatch):
    """Same never-raise rule as _record_node_run: a broken recall must let
    the draft proceed rather than break the step."""
    from prism_service.api import workflows as wf
    from prism_service.services import agent_runs_data

    def _boom(*a, **kw):
        raise RuntimeError("scores.db is corrupt")

    monkeypatch.setattr(agent_runs_data, "get_agent_runs", _boom)

    resp = wf.workflow_step_refusal_recall(
        wf.RefusalRecallRequest(task_id="t-3"), project="prism")

    assert resp.refusal_reason == ""
    assert resp.refusal_block == ""


def test_refusal_recall_is_registered_in_step_handlers():
    from prism_service.services import task_runner

    assert "refusal-recall" in task_runner._step_handlers()


def test_refusal_recall_exports_camel_and_snake_for_interpolation():
    """_exported_variables must be able to fill ${refusalBlock} in the
    NEXT declared step -- both spellings, like every other typed step
    response (StepEnrichResponse already relies on this)."""
    from prism_service.api import workflows as wf
    from prism_service.services.task_runner import _exported_variables

    resp = wf.RefusalRecallResponse(
        refusal_reason="test_drafted: bad import",
        refusal_block="A previous draft was refused: test_drafted: bad import")

    out = _exported_variables(resp)
    assert out.get("refusal_block") == resp.refusal_block
    assert out.get("refusalBlock") == resp.refusal_block
