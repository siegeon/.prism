"""Pin test: registered multiplier blocks (prism_service/blocks/) are
surfaced as their own /workflows catalog entry, with real run counts and
a project-wide "N tasks / M nodes / K blocks" banner -- owner
2026-09-13/14, task b490fabc: "im not seeing how many tasks, and how few
workflow nodes... you have not got the hang of creating the multiplier
blocks that are pydantic."

A declared Block that is never surfaced on the canvas is exactly the
same invisibility problem as a hand-built Python branch -- this pins
that `get_workflows` actually lists them.
"""
from __future__ import annotations

import types

import pytest

# Importing these registers every block this repo has declared so far --
# the count assertions below must stay true regardless of how many more
# land after this file is written.
from prism_service.blocks import list_blocks  # noqa: F401
from prism_service.services import deploy_worker  # noqa: F401
from prism_service.services import design_packet  # noqa: F401
from prism_service.services import gate_adjudicator  # noqa: F401
from prism_service.services import resume_actuator  # noqa: F401
from prism_service.services import task_workspace  # noqa: F401
from prism_service.blocks import red_blocks  # noqa: F401


class _Svc:
    """Minimal stand-in for task_svc, matching test_api_workflows.py's own
    (the endpoint only ever LISTS)."""

    def __init__(self, tasks):
        self.tasks = list(tasks)

    def list(self, status=None, assigned_agent=None, tag=None,
             story_file=None, parent_id=None, id=None):
        return list(self.tasks)


def _scripted_validation(project="prism"):
    return {"id": "validation", "name": "Build and test",
           "description": "", "project_type": "python", "steps": []}


def _get_workflows(monkeypatch, tasks=()):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        workflows_api, "get_project",
        lambda p: types.SimpleNamespace(task_svc=_Svc(tasks)))
    monkeypatch.setattr(workflows_api, "_project_validation_workflow",
                        _scripted_validation)
    monkeypatch.setattr(workflows_api, "_conductor_behavior_workflows",
                        lambda project: [])
    return workflows_api.get_workflows("prism")


def test_worker_seat_blocks_entry_lists_every_registered_block(monkeypatch):
    body = _get_workflows(monkeypatch)
    by_id = {w["id"]: w for w in body["workflows"]}
    assert "worker_seat_blocks" in by_id, (
        f"no worker_seat_blocks entry: {[w['id'] for w in body['workflows']]}")

    entry = by_id["worker_seat_blocks"]
    step_routes = {s["route"] for s in entry["steps"]}
    registered_ids = {b.id for b in list_blocks()}
    assert registered_ids, "no blocks registered at all -- import order broke"
    assert registered_ids <= step_routes, (
        f"registered blocks missing from the catalog: "
        f"{registered_ids - step_routes}")

    # A step's route is the block's OWN id -- the exact key run_block
    # already records every call under, so _attach_node_trend_batch's
    # route-keyed lookup finds it with no new counting code.
    for step in entry["steps"]:
        assert step["id"] == step["route"]
        assert "run_count" in step
        assert "running_now" in step


def test_worker_seat_blocks_entry_is_a_root_no_parent_id(monkeypatch):
    body = _get_workflows(monkeypatch)
    by_id = {w["id"]: w for w in body["workflows"]}
    assert "parent_id" not in by_id["worker_seat_blocks"]


def test_the_banner_counts_are_present_and_sane(monkeypatch):
    tasks = [
        types.SimpleNamespace(id="t1", status="in_progress",
                              workflow_step="write_failing_tests"),
        types.SimpleNamespace(id="t2", status="done",
                              workflow_step="green_gate"),
    ]
    body = _get_workflows(monkeypatch, tasks=tasks)

    assert body["task_count"] == 2, (
        "task_count must be the WHOLE project's task count, done/"
        f"cancelled included -- got {body['task_count']}")
    assert body["block_count"] == len(list_blocks())
    assert body["block_count"] >= 13, (
        "at least the 3 landing-1 blocks, the 4 landing-2 red.* blocks, "
        "the 5 landing-2b blocks (deploy.on_signal, workspace.recreate, "
        "adjudicator.unconditional_first_sweep, adjudicator.fair_cursor, "
        "adjudicator.inconclusive_rewind_backoff), and "
        "red.rewind_on_exhausted_budget must be registered, got "
        f"{body['block_count']}")
    assert body["node_count"] >= body["block_count"], (
        "node_count sums every declared step across the whole catalog, "
        "which must be at least as many as the blocks alone")


def test_every_landing_block_id_is_a_route_on_the_catalog_entry(monkeypatch):
    """The since-7.13.330 coverage check: every block this repo has
    registered so far must be a real route on worker_seat_blocks -- a
    block declared and never surfaced would be invisible on the canvas
    exactly like the Python branches this whole effort exists to fix."""
    EXPECTED = {
        "certainty.derive_oracle", "resume.clear_stale_park",
        "adjudicator.drain", "red.targets_from_acs", "red.context_pack",
        "red.prompt_compose", "red.materialize", "deploy.on_signal",
        "workspace.recreate", "adjudicator.unconditional_first_sweep",
        "adjudicator.fair_cursor", "adjudicator.inconclusive_rewind_backoff",
        "red.rewind_on_exhausted_budget",
    }
    body = _get_workflows(monkeypatch)
    by_id = {w["id"]: w for w in body["workflows"]}
    routes = {s["route"] for s in by_id["worker_seat_blocks"]["steps"]}
    missing = EXPECTED - routes
    assert not missing, f"blocks missing from the catalog: {missing}"


def test_the_api_process_alone_registers_every_block():
    """THE LIVE DEFECT (owner 2026-09-13/14): GET /api/workflows on a
    freshly-started API process reported block_count 7, not 12+ --
    registration only happened for blocks whose seat module something
    ELSE in that process had already imported for an unrelated reason.
    The worker-host process imports design_packet/resume_actuator/
    gate_adjudicator/deploy_worker/task_workspace anyway (it drives
    them), so it never showed the bug; the API process serving this
    endpoint does not necessarily import any of them on its own.

    Proven in a FRESH subprocess that imports ONLY
    prism_service.api.workflows -- never the seat modules directly --
    so this cannot pass by accident from another test file's imports
    still being warm in the SAME process (import caching would hide
    exactly this defect)."""
    import subprocess
    import sys

    script = (
        "from prism_service.api import workflows as wf\n"
        "from prism_service.blocks import list_blocks\n"
        "print(len(list_blocks()))\n"
    )
    proc = subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    count = int(proc.stdout.strip())
    assert count >= 13, (
        f"the API process alone (no worker module explicitly imported) "
        f"registered only {count} blocks -- importing "
        f"prism_service.api.workflows must be enough on its own")
