"""Align language is a top-level workflow the SYSTEM controls (task
f07c9cea, epic df0eed4a, owner rule mx-f49a5c: "an agent-shaped
behaviour is a top-level workflow like the conductor, so the system
controls it").

Pins:

  1. models.workflow.steps_for("align_language") / known_workflows()
     register the workflow (collect -> align -> verify -> done, no
     gate -- the whole pass is machine-run).
  2. The Workflows page catalog (api/workflows.get_workflows) lists it
     with real step content.
  3. Its behaviour is a versioned per-project file
     (.prism/behaviors/align_language.json via get_workflow_behavior /
     provide_workflow_behavior), same mechanism "validation" uses.
  4. services.language_alignment.align_language: dry run counts and
     samples without writing; apply writes through TaskService.update
     (so the ste_normalise history row lands) and is idempotent;
     plan_doc/plan_diagram are never touched even when named.
  5. services.language_alignment_worker.run_once_for creates exactly
     ONE visible run task and drives it through the SAME conductor
     report path (api/conductor_flow.flow_start/flow_report)
     services/task_runner.py drives a human-authored task through, all
     the way to status=done -- with a behaviour of enabled=false making
     it a no-op unless force=True.
  6. POST /api/tasks/align-language and the MCP tool task_align_language
     return the same {"run_task_id", "report"} shape.
  7. TasksPage.tsx source carries the backlog banner: the fetch path,
     the two-click preview/run state, and the run-task link target (no
     JS test runner in this repo -- UI ACs are pinned by asserting the
     real TSX source, same discipline test_conductor_page_animated_
     cleanup_ui.py already documents).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASKS_PAGE = _SRC / "pages" / "TasksPage.tsx"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext (mirrors test_understanding_respects_the_
    ontology.py / test_content_is_ste_at_every_write.py) so TaskService,
    the REST routers, and MCP handle_tool all resolve the SAME tmp-backed
    data dir."""
    from prism_service import config as cfg
    from prism_service import project_context as pc

    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield f"align-language-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


@pytest.fixture
def no_real_worktree(tmp_path, monkeypatch):
    """flow_start creates a REAL git worktree by default (task_workspace.
    ensure_workspace). A unit test driving the conductor report path has
    no business shelling out to git -- stub it to a fake record, the same
    substitution test_conductor_tile_requires_claim.py makes for
    workspace_record.

    Also stubs the align_language behaviour file's location: _behavior_file
    needs a real, existing configured project source path, which a
    synthetic per-test project name never has (mirrors
    test_provided_child_behavior_versions_parent_and_preserves_sibling's
    own monkeypatch of _behavior_file)."""
    from prism_service.api import conductor_flow as flow
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        flow.task_workspace, "ensure_workspace",
        lambda task_id, **kw: {"path": "/fake/ws", "branch": "x",
                               "baseline": "0" * 40, "repo_root": "/fake"},
    )
    monkeypatch.setattr(
        workflows_api, "_behavior_file",
        lambda project, workflow: tmp_path / ".prism" / "behaviors" / f"{workflow}.json",
    )


def _seed_tasks(project: str):
    """Five tasks exercising every case align_language must get right:

      loose_a / loose_b — a contraction + a clause-joining semicolon in
        the title (flavored mode fixes both).
      clean — already plain, nothing to fix.
      fenced — its only loose-looking text sits inside a fenced code
        block, which ste.normalize protects and never rewrites.
      deleted — loose text, but status=deleted -- must be skipped
        outright, not just coincidentally clean.

    loose_a also carries loose plan_doc text, to prove align_language
    never touches plan_doc even when it is the one thing left flagged.

    TaskService.create/update ALREADY run the STE normaliser on write
    (task 36283d72) -- passing loose text straight to create() would be
    fixed before this fixture even returns. The loose fields are instead
    written directly to the row with raw SQL, bypassing that pipeline, so
    they land genuinely loose the way an older pre-36283d72 row (or a
    direct DB write) would -- exactly the backlog align_language exists
    to clean up. plan_doc used to be the one exception (TaskService only
    CHECKED it): task dc676e24 (2026-08-26) made plan_doc align at write
    too, through TaskService._align_plan_doc, so it now needs the SAME
    raw-SQL bypass as the other loose fields to land genuinely loose here
    -- a legacy/pre-dc676e24 row is exactly the shape align_language's
    plan_doc/plan_diagram exclusion still exists to leave alone.
    """
    from prism_service.project_context import get_project

    ctx = get_project(project)
    task_svc = ctx.task_svc

    loose_a = task_svc.create(title="placeholder a")
    loose_b = task_svc.create(title="placeholder b")
    clean = task_svc.create(title="Ship the widget")
    fenced = task_svc.create(title="placeholder fenced")
    deleted = task_svc.create(title="placeholder deleted")

    def _raw_set(task_id: str, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        task_svc._db.execute(
            f"UPDATE tasks SET {cols} WHERE id=?",
            (*fields.values(), task_id))
        task_svc._db.commit()

    # plan_doc (task dc676e24, 2026-08-26): seeded via the same raw-SQL
    # bypass as title/description above, now that TaskService aligns
    # plan_doc at write -- see the docstring note.
    _raw_set(loose_a.id, title="We don't need this; it's fine.",
             plan_doc="This isn't final; check it.")
    _raw_set(loose_b.id, title="It doesn't matter; ship it.")
    _raw_set(
        fenced.id, title="See the plan below",
        description=("Follow the steps.\n\n```\nif not ok: raise  "
                     "# don't touch this; leave it\n```\n"),
    )
    _raw_set(deleted.id, title="We don't need this either; drop it.",
             status="deleted")

    return {"loose_a": loose_a.id, "loose_b": loose_b.id, "clean": clean.id,
            "fenced": fenced.id, "deleted": deleted.id}


# ── (1) registration ────────────────────────────────────────────────────

def test_align_language_steps_and_known_workflows():
    from prism_service.models.task import known_workflows
    from prism_service.models.workflow import steps_for

    ids = [s["id"] for s in steps_for("align_language")]
    assert ids == ["collect", "align", "verify", "done"]
    assert "align_language" in known_workflows()

    # No step is a gate -- the whole pass is machine-run (owner rule
    # mx-f49a5c).
    types = {s["id"]: s["type"] for s in steps_for("align_language")}
    assert "gate" not in types.values()
    assert types == {"collect": "intake", "align": "agent",
                     "verify": "agent", "done": "done"}


# ── (2) catalog ──────────────────────────────────────────────────────────

def test_workflows_catalog_lists_align_language_with_step_content(project, monkeypatch):
    from prism_service.api import workflows as workflows_api

    # The "validation" entry is sourced from an external AOS workflow
    # engine over HTTP -- not reachable in a unit test, and not what this
    # test is pinning. Stub it the same way test_the_view_is_project_
    # scoped does; _conductor_behavior_workflows already fails soft to []
    # when the project's source root doesn't exist on disk.
    monkeypatch.setattr(
        workflows_api, "_project_validation_workflow",
        lambda proj: {"id": "validation", "name": "Build and test",
                      "description": "x", "project_type": "python",
                      "steps": [], "bots": [], "occupancy": {}},
    )

    result = workflows_api.get_workflows(project=project)
    entries = {w["id"]: w for w in result["workflows"]}
    assert "align_language" in entries, [w["id"] for w in result["workflows"]]
    entry = entries["align_language"]
    assert entry["name"] == "Align language"
    step_ids = [s["id"] for s in entry["steps"]]
    assert step_ids == ["collect", "align", "verify", "done"]
    for step in entry["steps"]:
        assert step["input"] and step["action"] and step["output"]
    assert "task_count" in entry


# ── (3) behaviour ────────────────────────────────────────────────────────

def test_behavior_defaults_and_provide_bumps_version(tmp_path, monkeypatch, project):
    from prism_service.api import workflows as workflows_api

    target = tmp_path / ".prism" / "behaviors" / "align_language.json"
    monkeypatch.setattr(workflows_api, "_behavior_file",
                        lambda proj, wf: target)

    got = workflows_api.get_workflow_behavior(project, "align_language")
    assert got["path"] == "align_language"
    behavior = got["behavior"]
    assert behavior["enabled"] is True
    assert behavior["mode"] == "apply"
    assert behavior["batch_size"] == 50
    assert behavior["include_imported"] is True
    assert behavior["fields"] == [
        "title", "description", "oracle", "likely_misfire", "stop_if",
        "completion_proof", "premise_notes",
    ]
    assert behavior["behaviorVersion"] == 1

    result = workflows_api.provide_workflow_behavior(
        project, "align_language", 1, {"enabled": False})
    assert result == {"ok": True, "path": "align_language", "version": 2}
    assert target.exists()
    stored = json.loads(target.read_text())
    assert stored["enabled"] is False
    assert stored["behaviorVersion"] == 2

    got2 = workflows_api.get_workflow_behavior(project, "align_language")
    assert got2["behavior"]["behaviorVersion"] == 2
    assert got2["behavior"]["enabled"] is False

    with pytest.raises(Exception) as stale:
        workflows_api.provide_workflow_behavior(
            project, "align_language", 1, {"enabled": True})
    assert getattr(stale.value, "status_code", None) == 409


# ── (4) executor ─────────────────────────────────────────────────────────

def test_dry_run_counts_two_and_writes_nothing(project):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment

    ids = _seed_tasks(project)
    before = {k: get_project(project).task_svc.get(v) for k, v in ids.items()}

    report = language_alignment.align_language(project, apply=False)
    assert report["would_change"] == 2
    assert report["scanned"] == 4, "the deleted task must not be scanned at all"
    sample_ids = {s["task_id"] for s in report["sample"]}
    assert sample_ids <= {ids["loose_a"], ids["loose_b"]}
    assert ids["clean"] not in sample_ids
    assert ids["fenced"] not in sample_ids
    assert ids["deleted"] not in sample_ids

    after = {k: get_project(project).task_svc.get(v) for k, v in ids.items()}
    for key in before:
        assert after[key].title == before[key].title, \
            f"dry run must write nothing, but {key}'s title changed"
    assert after["loose_a"].plan_doc == before["loose_a"].plan_doc


def test_apply_changes_two_with_history_then_second_apply_changes_zero(project):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment

    ids = _seed_tasks(project)
    task_svc = get_project(project).task_svc

    report = language_alignment.align_language(project, apply=True)
    assert report["changed"] == 2
    assert report["per_rule"], "at least one rule must have fired"
    assert "title" in report["per_field"]

    loose_a = task_svc.get(ids["loose_a"])
    loose_b = task_svc.get(ids["loose_b"])
    assert "don't" not in loose_a.title and "n't" not in loose_a.title
    assert ";" not in loose_a.title
    assert "don't" not in loose_b.title
    clean = task_svc.get(ids["clean"])
    assert clean.title == "Ship the widget"

    for task_id in (ids["loose_a"], ids["loose_b"]):
        history = task_svc.history(task_id)
        assert any(h.action == "ste_normalise" for h in history), (
            f"task {task_id} must carry an ste_normalise history row, got "
            f"{[h.action for h in history]}"
        )

    second = language_alignment.align_language(project, apply=True)
    assert second["changed"] == 0, "an immediate second apply must be a no-op"


def test_plan_doc_is_never_touched_even_when_named_in_fields(project):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment

    ids = _seed_tasks(project)
    task_svc = get_project(project).task_svc
    original_plan_doc = task_svc.get(ids["loose_a"]).plan_doc
    assert ";" in original_plan_doc and "isn't" in original_plan_doc

    report = language_alignment.align_language(
        project, apply=True, fields=["plan_doc", "plan_diagram"])
    assert report["changed"] == 0, \
        "plan_doc/plan_diagram must be filtered out even when explicitly named"
    assert task_svc.get(ids["loose_a"]).plan_doc == original_plan_doc

    # Re-anchored (task dc676e24, 2026-08-26): align_language itself still
    # never SELECTS plan_doc as a field to change (proven above -- the
    # explicit-fields call is a no-op and touches nothing). But plan_doc
    # now aligns at write like every other flavored field (task dc676e24),
    # and TaskService._apply_ste runs UNCONDITIONALLY on every update()
    # call, on every flavored field, not just the ones the caller named
    # (same pre-existing rule title/description already lived under --
    # see _apply_ste's own docstring). So a normal pass that fixes only
    # loose_a's TITLE rides that same write and realigns plan_doc as a
    # side effect -- this is the new, correct contract, not a leak.
    language_alignment.align_language(project, apply=True)
    # "This isn't final; check it." -> contraction expanded, semicolon
    # becomes a sentence break (ste.normalize's documented rules; no
    # lexicon synonym in this text, so lexicon.align is a no-op here).
    assert (task_svc.get(ids["loose_a"]).plan_doc
            == "This is not final. Check it.")


def test_batch_size_caps_tasks_touched_per_call(project):
    from prism_service.services import language_alignment

    _seed_tasks(project)
    report = language_alignment.align_language(project, apply=False, batch_size=1)
    assert report["would_change"] == 1


# ── (5) worker / conductor drive ─────────────────────────────────────────

def test_run_once_for_creates_one_task_and_drives_it_to_done(
    project, no_real_worktree,
):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment_worker as worker

    _seed_tasks(project)
    task_svc = get_project(project).task_svc

    result = worker.run_once_for(project, force=True)
    assert "run_task_id" in result, result
    run_task_id = result["run_task_id"]
    assert run_task_id

    all_align_tasks = [t for t in task_svc.list() if t.workflow == "align_language"]
    assert len(all_align_tasks) == 1, \
        f"exactly one run task must exist, got {[t.id for t in all_align_tasks]}"

    run_task = task_svc.get(run_task_id)
    assert run_task.workflow == "align_language"
    assert run_task.status == "done"
    assert run_task.workflow_step == "done"
    assert set(["align-language", "daemon"]) <= set(run_task.tags)
    assert "Align language in" in run_task.title

    history = task_svc.history(run_task_id)
    advances = [h for h in history if h.action == "advance_task"]
    # collect->align, align->verify, verify->done
    assert len(advances) >= 3, [h.details for h in advances]
    assert any("language_alignment_worker_done" in h.action for h in history)

    # The verify step's proof is the last thing written to completion_proof
    # before the worker finalizes the task -- visible in history/on the row.
    verify_proof = json.loads(run_task.completion_proof)
    assert set(verify_proof) == {"before", "after", "second_dry_run_would_change"}
    assert verify_proof["second_dry_run_would_change"] == 0, \
        "the pass just ran, so a fresh scan right after must find nothing left"


def test_behavior_disabled_is_a_noop_unless_forced(project, no_real_worktree):
    from prism_service.api import workflows as workflows_api
    from prism_service.services import language_alignment_worker as worker

    workflows_api.provide_workflow_behavior(
        project, "align_language", 1, {"enabled": False})

    _seed_tasks(project)

    skipped = worker.run_once_for(project)
    assert skipped == {"skipped": "disabled"}

    forced = worker.run_once_for(project, force=True)
    assert "run_task_id" in forced, forced


def test_run_once_for_skips_when_nothing_to_align(project, no_real_worktree):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment_worker as worker

    get_project(project).task_svc.create(title="Ship the widget")
    result = worker.run_once_for(project, force=True)
    assert result == {"skipped": "nothing to align"}


# ── (6) API / MCP parity ────────────────────────────────────────────────

def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def _call_mcp(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    text = asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text
    return json.loads(text)


def test_api_route_dry_run_matches_mcp_tool_dry_run(project):
    _seed_tasks(project)
    client = _api_client()

    resp = client.post(f"/api/tasks/align-language?project={project}&dry_run=true")
    assert resp.status_code == 200, resp.text
    api_body = resp.json()
    assert api_body["run_task_id"] is None
    assert api_body["report"]["would_change"] == 2

    mcp_body = _call_mcp("task_align_language", {"dry_run": True}, project)
    assert mcp_body["run_task_id"] is None
    assert mcp_body["report"]["would_change"] == 2


def test_api_route_real_run_matches_mcp_tool_real_run(project, no_real_worktree):
    _seed_tasks(project)
    client = _api_client()

    resp = client.post(f"/api/tasks/align-language?project={project}&dry_run=false")
    assert resp.status_code == 200, resp.text
    api_body = resp.json()
    assert api_body["run_task_id"]
    assert api_body["report"]["changed"] == 2

    # A second seed + a second real run over MCP proves the SAME shape.
    _seed_tasks(project)
    mcp_body = _call_mcp("task_align_language", {"dry_run": False}, project)
    assert mcp_body["run_task_id"]
    assert mcp_body["report"]["changed"] == 2
    assert mcp_body["run_task_id"] != api_body["run_task_id"]


# ── (7) TasksPage.tsx source (no JS test runner in this repo) ───────────

def test_tasks_page_carries_the_align_language_banner_source():
    src = _TASKS_PAGE.read_text(encoding="utf-8")

    # Imports the typed fetch helpers, never re-implements the calls inline.
    assert "getOntologyRules" in src
    assert "alignLanguage" in src

    # The fetch path for the backlog count.
    assert "getOntologyRules(project)" in src
    assert '"/task/"' in src

    # First-click state: preview count + the initial button label.
    assert "tasks use language not yet aligned with the ontology" in src
    assert "Align language" in src

    # Second-click state: after a dry run, the label and count change.
    assert "tasks would change" in src
    assert "Align language now" in src

    # The real run: dry_run=false through the SAME alignLanguage() helper,
    # never a bespoke fetch.
    assert "alignLanguage(project, true)" in src
    assert "alignLanguage(project, false)" in src

    # The finished run links to the driven task.
    assert "/tasks/${alignRunTaskId}" in src
    assert "data-align-language-run-link" in src
    assert "data-align-language-button" in src
