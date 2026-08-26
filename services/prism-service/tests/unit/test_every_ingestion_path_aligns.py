"""Every text writer registers with Align language and cannot drift
(task c7edf4e2, epic cc9a44c8).

Two independent halves.

DYNAMIC: ste.on_apply (services/ste.py) fires a listener every time ste
actually normalises a piece of text -- normalize() is the function every
real write path in this codebase calls (directly, or through
TaskService._apply_ste / MemoryService.store), so it is the one true
observation point. services.language_alignment registers a listener at
import that turns a call's stack frames into ONE path label and records
one hit in its coverage() registry. This file drives EVERY real
ingestion path this task named and asserts (a) a spy registered directly
via ste.on_apply saw the call, and (b) coverage(project) lists the
expected label with count >= 1.

Signals align ON ARRIVAL since task ed034701 (epic cc9a44c8): SignalStore.
create runs the aligner over subject and body, keeps the raw text beside
the aligned text, and the registry labels the hit by its entry point
(api.signals for POST /api/signals, mcp.signal_post for the MCP tool).
Promotion then creates a TASK through TaskService, which aligns again
(idempotent). Both are pinned below.

STATIC: every .py under prism_service (excluding tests) is scanned for
an INSERT/UPDATE statement that references a column named title,
description, oracle, likely_misfire, completion_proof, premise_notes,
plan_doc, subject, body, or summary on the tasks/memories/signals
tables. Every such hit must live in task_service.py, memory_service.py,
or signal_store.py -- the three modules the "writing standard" section of
CLAUDE.md names as owning free text on the way into the store. A hit
anywhere else is a NEW, unaligned write path this task's whole point is
to catch before it drifts.

TaskService.ensure_external_intake's own raw INSERT (task_service.py)
passes the location rule for free -- it lives in the one allowed file --
but a SECOND assertion checks it specifically: it must either call
_apply_ste itself, or be named in this file's own NAMED_EXCEPTIONS dict
with the reason it does not (task 683e65eb: the CREATE path re-aligns
one call later, through work_item_sync._import_one's follow-up
TaskService.update()).
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PACKAGE_ROOT = _SERVICE_ROOT / "prism_service"


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext (mirrors test_understanding_respects_the_
    ontology.py / test_content_is_ste_at_every_write.py) so the REST
    routers, MCP handle_tool, and every service below all resolve the
    SAME tmp-backed data dir -- including where coverage() persists its
    align_language_coverage.json."""
    from prism_service import config as cfg
    from prism_service import project_context as pc

    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield f"ingestion-paths-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


@pytest.fixture
def no_real_worktree(tmp_path, monkeypatch):
    """flow_start creates a REAL git worktree by default (task_workspace.
    ensure_workspace); the align_language behaviour file needs a real,
    configured project source path. Both copied verbatim from
    test_align_language_workflow.py's own fixture of the same name, for
    the conductor_flow / language_alignment_worker scenario below."""
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


@pytest.fixture
def spy():
    """A listener registered directly via ste.on_apply (task c7edf4e2's
    own public hook), independent of services.language_alignment's own
    listener -- this is the DIRECT proof the hook fired at all, not just
    that the coverage file happens to agree. Removed after the test so
    spies never accumulate across the suite."""
    from prism_service.services import ste

    calls: list[tuple[str, list[tuple[str, str, str, str]]]] = []

    def _spy(mode, frames):
        calls.append((mode, frames))

    ste.on_apply(_spy)
    yield calls
    if _spy in ste._LISTENERS:
        ste._LISTENERS.remove(_spy)


LOOSE_TEXT = "We don't need this; it's fine."


def _labels_seen(calls) -> list[str]:
    """Convenience for a test that just wants to confirm the spy fired at
    all, without duplicating _path_label's own logic."""
    from prism_service.services import language_alignment as la
    return [la._path_label(frames) for _mode, frames in calls]


# ── (1) POST /api/tasks, PATCH /api/tasks/{id} -> api.tasks ────────────


def test_api_task_create_and_update_register(project, spy):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    from prism_service.services import language_alignment as la

    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    client = TestClient(app)

    r = client.post("/api/tasks", params={"project": project},
                     json={"title": "api create check", "description": LOOSE_TEXT})
    assert r.status_code in (200, 201), r.text
    tid = r.json()["task"]["id"]

    r = client.patch(f"/api/tasks/{tid}", params={"project": project},
                      json={"description": "Another loose one; still here."})
    assert r.status_code == 200, r.text

    assert len(spy) >= 2
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "api.tasks" in rows
    assert rows["api.tasks"]["count"] >= 2
    assert rows["api.tasks"]["known"] is True


# ── (2) MCP task_create / task_update -> mcp.task_create / mcp.task_update ─


def _mcp_call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    result = asyncio.run(handle_tool(tool, args, project_id=project_id))
    return json.loads(result[0].text)


def test_mcp_task_create_and_update_register(project, spy):
    from prism_service.services import language_alignment as la

    created = _mcp_call(
        "task_create", {"title": "mcp create check", "description": LOOSE_TEXT},
        project)
    _mcp_call(
        "task_update",
        {"id": created["id"], "description": "Another loose one; still here."},
        project)

    assert len(spy) >= 2
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "mcp.task_create" in rows
    assert "mcp.task_update" in rows
    assert rows["mcp.task_create"]["known"] is True
    assert rows["mcp.task_update"]["known"] is True


# ── (3) MCP memory_store -> mcp.memory_store ────────────────────────────


def test_mcp_memory_store_registers(project, spy):
    from prism_service.services import language_alignment as la

    _mcp_call(
        "memory_store",
        {"domain": "architecture", "name": "mcp memory check",
         "description": "a memory; with a semicolon in it.",
         "type": "pattern", "classification": "tactical"},
        project)

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "mcp.memory_store" in rows
    assert rows["mcp.memory_store"]["count"] >= 1


# ── (4) MemoryService.store called directly -> memory_service ──────────


def test_direct_memory_service_store_registers(project, spy):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment as la

    ctx = get_project(project)
    ctx.memory_svc.store(
        domain="architecture", name="direct memory check",
        description="a memory; with a semicolon in it.",
        type="pattern", classification="tactical")

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "memory_service" in rows
    assert rows["memory_service"]["count"] >= 1


# ── (5) POST /api/memory/entry/{id}/action (supersede) -> api.memory ───


def test_api_memory_supersede_registers(project, spy):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import memory as memory_api
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment as la

    ctx = get_project(project)
    entry = ctx.memory_svc.store(
        domain="architecture", name="api memory check",
        description="the original description",
        type="pattern", classification="tactical")

    app = FastAPI()
    app.include_router(memory_api.router, prefix="/api/memory")
    client = TestClient(app)

    spy.clear()  # drop the seed store() above -- this test is about the API path
    r = client.post(
        f"/api/memory/entry/{entry.id}/action", params={"project": project},
        json={"action": "supersede",
              "description": "a new description; with a semicolon."})
    assert r.status_code == 200, r.text

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "api.memory" in rows
    assert rows["api.memory"]["count"] >= 1


# ── (6) API signals: create does NOT align, promote DOES -> api.signals ──


def test_signal_create_does_not_align_but_promote_does(project, spy):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import signals as signals_api
    from prism_service.services import language_alignment as la

    app = FastAPI()
    app.include_router(signals_api.router, prefix="/api/signals")
    client = TestClient(app)

    posted = client.post(
        "/api/signals", params={"project": project},
        json={"channel": "slack", "channel_ref": "C1/2", "subject": "ping",
              "body": "please look at this; it matters", "sender": "alice"})
    assert posted.status_code == 200, posted.text
    signal = posted.json()["signal"]

    # Re-anchored for task ed034701 (epic cc9a44c8): a signal aligns ON
    # ARRIVAL now -- SignalStore.create runs the aligner over subject and
    # body and keeps the raw text beside it -- so the spy sees two calls
    # and coverage lists the api.signals entry point before anything is promoted.
    assert len(spy) == 2
    assert "api.signals" in {row["path"] for row in la.coverage(project)}
    assert signal["body"] == "please look at this; it matters"
    assert "It matters" in signal["aligned_body"]

    r = client.post(
        f"/api/signals/{signal['id']}/promote", params={"project": project},
        json={"title": "go fix it", "description": LOOSE_TEXT})
    assert r.status_code == 200, r.text

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "api.signals" in rows
    assert rows["api.signals"]["count"] >= 1


def test_mcp_signal_post_aligns_on_arrival(project, spy):
    """Re-anchored for task ed034701 (epic cc9a44c8): signal_post over MCP
    reaches SignalStore.create, which aligns subject and body on arrival
    and keeps the raw text. The spy sees the two calls and coverage lists
    the mcp.signal_post entry point."""
    from prism_service.services import language_alignment as la

    _mcp_call(
        "signal_post",
        {"channel": "slack", "channel_ref": "C3/4", "subject": "ping",
         "body": "please look; at this", "sender": "bob"},
        project)

    assert len(spy) == 2
    assert "mcp.signal_post" in {row["path"] for row in la.coverage(project)}


# ── (7) work_item_sync import (fake GitHub adapter) -> work_item_sync ──


def test_work_item_sync_import_registers(project, tmp_path, spy):
    from prism_service.models.integration import ExternalEntityInput
    from prism_service.project_context import get_project
    from prism_service.services.integration_store import IntegrationStore
    from prism_service.services.work_item_sync import (
        PulledPage, WorkItemSyncService)
    from prism_service.services import language_alignment as la

    store = IntegrationStore(str(tmp_path / "integrations.db"))
    tasks = get_project(project).task_svc
    conn = store.ensure_connection("workspace-a", "github", "install-1")
    cont = store.ensure_container("workspace-a", conn.id, "repository", ".prism")

    class _FakeAdapter:
        provider = "github"

        def pull_page(self, connection, container, cursor, page_token):
            return PulledPage(
                entities=[ExternalEntityInput(
                    entity_kind="issue", remote_id="gh-1", display_key="#1",
                    title="raw github headline", body=LOOSE_TEXT,
                    url="https://github.com/x/y/issues/1",
                    remote_status="open", status_category="open")],
                next_page_token=None, next_cursor=None)

    svc = WorkItemSyncService(store, intake=tasks)
    svc.register(_FakeAdapter())
    svc.pull_container("workspace-a", conn, cont)

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "work_item_sync" in rows
    assert rows["work_item_sync"]["count"] >= 1


# ── (8) task_runner._route_proof -> task_runner ─────────────────────────
# The public run_one_step/sweep_once entry points shell out to claude_cli
# (a real subprocess/LLM call) -- not a cheap unit fixture. _route_proof
# is the standalone helper that does the ACTUAL text write every step
# outcome routes through (services/task_runner.py); calling it directly
# exercises the real production write path without the subprocess cost.


def test_task_runner_route_proof_registers(project, spy):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment as la
    from prism_service.services import task_runner

    ctx = get_project(project)
    task = ctx.task_svc.create(title="route proof check")

    task_runner._route_proof(
        ctx.task_svc, task.id, "implement_tasks",
        "done; something worth recording.")

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "task_runner" in rows
    assert rows["task_runner"]["count"] >= 1


# ── (9) language_alignment_worker + conductor_flow's own report path ───
# services.language_alignment_worker.run_once_for is itself a real
# production entry point (the daemon seat, and POST /api/tasks/align-
# language / MCP task_align_language all call it) -- driving it end to
# end also exercises api.conductor_flow.flow_report -> conductor_service.
# advance_task -> TaskService.update, all in the SAME call.


def test_language_alignment_worker_and_conductor_flow_register(
    project, no_real_worktree, spy,
):
    from prism_service.project_context import get_project
    from prism_service.services import language_alignment as la
    from prism_service.services import language_alignment_worker as worker

    ctx = get_project(project)
    task = ctx.task_svc.create(title="placeholder")
    # Bypass TaskService.create's own STE pass (task 36283d72 already
    # normalises on write) with a raw UPDATE, so this task lands
    # genuinely loose -- the exact backlog align_language exists to find
    # (same technique test_align_language_workflow.py's _seed_tasks uses).
    ctx.task_svc._db.execute(
        "UPDATE tasks SET title=? WHERE id=?", (LOOSE_TEXT, task.id))
    ctx.task_svc._db.commit()

    res = worker.run_once_for(project, force=True)
    assert "run_task_id" in res, res

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "language_alignment_worker" in rows
    assert "conductor_flow" in rows


# ── (10) event_handlers._persist_reflection -> reflection ──────────────


def test_reflection_memory_write_registers(project, spy):
    from prism_service.project_context import get_project
    from prism_service.services import event_handlers
    from prism_service.services import language_alignment as la

    ctx = get_project(project)
    verdict = {"new_memories": [{
        "domain": "architecture", "name": "reflection check",
        "description": "a reflected memory; with a semicolon.",
        "type": "pattern", "classification": "tactical",
    }]}
    event_handlers._persist_reflection(ctx, verdict)

    assert len(spy) >= 1
    rows = {row["path"]: row for row in la.coverage(project)}
    assert "reflection" in rows
    assert rows["reflection"]["count"] >= 1


# ── (11) an unclassifiable caller still registers, honestly, as unknown ─


def test_an_unmapped_caller_registers_as_unknown_not_silently(project, spy):
    """A call this test file itself makes (module prism_service is not on
    the stack at all here -- this file lives under tests/unit) must still
    show up in coverage, tagged known=False, rather than vanish. This is
    the behaviour that keeps the registry honest about a genuinely NEW
    ingestion path nobody has named yet."""
    from prism_service.services import ste
    from prism_service.services import language_alignment as la

    ste.normalize(LOOSE_TEXT, mode="flavored")

    assert len(spy) >= 1
    rows = la.coverage(project)
    unknown_rows = [r for r in rows if not r["known"]]
    assert unknown_rows, rows
    assert all(r["path"].startswith("unknown:") for r in unknown_rows)


# ── STATIC: every sensitive-column write lives in the three allowed files ──


_SENSITIVE_COLUMNS = (
    "title", "description", "oracle", "likely_misfire", "completion_proof",
    "premise_notes", "plan_doc", "subject", "body", "summary",
)
_SENSITIVE_TABLES = ("tasks", "memories", "signals")
_ALLOWED_WRITE_FILES = {"task_service.py", "memory_service.py", "signal_store.py"}

# INSERT [OR <verb>] INTO <table> ... / UPDATE <table> SET ...
_INSERT_RE = re.compile(
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(\w+)", re.IGNORECASE)
_UPDATE_RE = re.compile(r"UPDATE\s+(\w+)\s+SET", re.IGNORECASE)
_COLUMN_RE = re.compile(
    r"\b(" + "|".join(_SENSITIVE_COLUMNS) + r")\b", re.IGNORECASE)

# How far past the INSERT/UPDATE keyword to look for the column list --
# generous enough to cover a multi-line Python string literal spanning
# the whole statement, without reading into the NEXT unrelated statement.
_STATEMENT_WINDOW = 600


def _iter_prism_service_py_files():
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        if "tests" in path.relative_to(_PACKAGE_ROOT).parts:
            continue
        if "web" in path.relative_to(_PACKAGE_ROOT).parts:
            continue  # no .py under web/ is a real source file (build output)
        yield path


def _sensitive_write_hits() -> list[tuple[Path, int, str, str]]:
    """[(file, line_number, table, matched_column), ...] for every
    INSERT/UPDATE this static sweep finds against a sensitive table that
    also references a sensitive column, across the whole prism_service
    tree."""
    hits: list[tuple[Path, int, str, str]] = []
    for path in _iter_prism_service_py_files():
        text = path.read_text(encoding="utf-8")
        for pattern in (_INSERT_RE, _UPDATE_RE):
            for m in pattern.finditer(text):
                table = m.group(1).lower()
                if table not in _SENSITIVE_TABLES:
                    continue
                window = text[m.end(): m.end() + _STATEMENT_WINDOW]
                col_match = _COLUMN_RE.search(window)
                if not col_match:
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append((path, line_no, table, col_match.group(1).lower()))
    return hits


def test_every_sensitive_column_write_lives_in_the_three_allowed_files():
    hits = _sensitive_write_hits()
    assert hits, "the static sweep found nothing at all -- likely a regex regression"
    offenders = [
        f"{path.relative_to(_PACKAGE_ROOT)}:{line} (table={table}, column={col})"
        for path, line, table, col in hits
        if path.name not in _ALLOWED_WRITE_FILES
    ]
    assert not offenders, (
        "a task/memory/signal free-text column is written outside "
        f"task_service.py/memory_service.py/signal_store.py: {offenders}")


# ensure_external_intake's own raw INSERT lives in task_service.py (passes
# the location rule above for free) but never calls _apply_ste directly --
# task 683e65eb re-aligns the CREATE path one call later, through
# work_item_sync._import_one's own follow-up TaskService.update(). Named
# here, with the reason, exactly as this task's brief requires.
NAMED_EXCEPTIONS = {
    "ensure_external_intake": "import path re-aligns via _import_one (task 683e65eb)",
}


def test_ensure_external_intake_raw_insert_is_a_named_exception():
    task_service_path = _PACKAGE_ROOT / "services" / "task_service.py"
    text = task_service_path.read_text(encoding="utf-8")

    m = re.search(r"\n    def ensure_external_intake\(", text)
    assert m, "ensure_external_intake not found in task_service.py"
    next_def = re.search(r"\n    def \w+\(", text[m.end():])
    body_end = m.end() + (next_def.start() if next_def else len(text) - m.end())
    body = text[m.start():body_end]

    assert "INSERT" in body.upper(), "expected a raw INSERT in ensure_external_intake"

    calls_apply_ste = "_apply_ste(" in body
    is_named_exception = "ensure_external_intake" in NAMED_EXCEPTIONS

    assert calls_apply_ste or is_named_exception, (
        "ensure_external_intake's raw INSERT neither runs _apply_ste nor "
        "is a documented NAMED_EXCEPTIONS entry")
    if not calls_apply_ste:
        assert NAMED_EXCEPTIONS["ensure_external_intake"] == (
            "import path re-aligns via _import_one (task 683e65eb)")
