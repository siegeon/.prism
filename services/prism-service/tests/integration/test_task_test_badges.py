"""RED scaffold — the TESTS tab reports the last REAL run (task f3e8d477).

Pins the two seams that make the evidence package honest:
  * run=true PERSISTS what it executed (today it returns and forgets), and
  * the read path (run=false) HYDRATES badges from that record while still
    never executing pytest — the 30-60s page hang must not come back.

Plus the UI: the tab must offer an explicit Run control, and page load must
keep its run=false fetch.

Imports live INSIDE the tests so the file collects and fails at runtime.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"

PINNED = "test_pinned_example"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """A real task whose pinned test file lives in a fake task worktree, so the
    endpoint's content-based discovery finds exactly one test."""
    from prism_service import config, project_context
    from prism_service.services import test_run_store

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    project_context._contexts.clear()
    ctx = project_context.get_project("badgeproj")
    task = ctx.task_svc.create(title="badge pin task")

    ws = tmp_path / "ws"
    tests_dir = ws / "services" / "prism-service" / "tests" / "integration"
    tests_dir.mkdir(parents=True)
    # The file NAMES the task id — that is how discovery associates it.
    (tests_dir / "test_pinned_example.py").write_text(
        f'"""Pins task {task.id}."""\n\n\ndef {PINNED}():\n    assert True\n',
        encoding="utf-8")

    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(ws), "baseline": "base"})

    store = test_run_store.TestRunStore(str(tmp_path / "test_runs.db"))
    test_run_store.set_test_run_store(store)
    yield {"task_id": task.id, "store": store, "ws": ws}
    test_run_store.set_test_run_store(None)
    project_context._contexts.clear()


def _get_tests(task_id, run=False):
    from prism_service.api.tasks import get_task_tests

    return get_task_tests(task_id, run=run, project="badgeproj")


# ── AC-3: the read path hydrates and NEVER executes pytest ─────────────

def test_read_path_hydrates_without_running_pytest(pinned, monkeypatch):
    from prism_service.api import tasks as tasks_api

    # A previous real run is on record...
    pinned["store"].record_run(
        pinned["task_id"], "anytree",
        [{"name": PINNED, "file": "tests/integration/test_pinned_example.py",
          "status": "passed"}])

    # ...and the read path must NOT spawn pytest (that is the 30-60s hang).
    def _boom(*a, **k):
        raise AssertionError("run=false must never execute pytest")
    monkeypatch.setattr(tasks_api, "_run_pinned_tests", _boom)

    payload = _get_tests(pinned["task_id"], run=False)
    rows = {r["name"]: r for r in payload["tests"]}
    assert PINNED in rows, "discovery must still find the pinned test"
    assert rows[PINNED]["status"] == "passed", (
        "the badge must reflect the persisted run, not NOT RUN")


# ── AC-4: a run STICKS ─────────────────────────────────────────────────

def test_run_true_persists_so_the_next_read_reports_it(pinned, monkeypatch):
    from prism_service.api import tasks as tasks_api

    monkeypatch.setattr(
        tasks_api, "_run_pinned_tests",
        lambda root, files: {PINNED: "passed"})

    ran = _get_tests(pinned["task_id"], run=True)
    assert ran.get("ran") is True

    # The store now holds it — the outcome outlived the response.
    assert pinned["store"].statuses_for(pinned["task_id"])[PINNED]["status"] == "passed"

    # And a plain read (no execution) reports the same thing.
    def _boom(*a, **k):
        raise AssertionError("read path must not execute pytest")
    monkeypatch.setattr(tasks_api, "_run_pinned_tests", _boom)
    again = {r["name"]: r for r in _get_tests(pinned["task_id"], run=False)["tests"]}
    assert again[PINNED]["status"] == "passed"


def test_unrun_test_still_reads_not_run(pinned, monkeypatch):
    from prism_service.api import tasks as tasks_api
    monkeypatch.setattr(tasks_api, "_run_pinned_tests",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("no run expected")))
    rows = {r["name"]: r for r in _get_tests(pinned["task_id"], run=False)["tests"]}
    assert rows[PINNED].get("status") in (None, "", "not-run")


# ── AC-7: the UI offers a Run control and stays fast on load ───────────

def test_tests_tab_offers_an_explicit_run_control():
    src = _read(_WEB / "components" / "plan" / "PlanView.tsx")
    assert "Run tests" in src, "the TESTS tab must offer an explicit Run control"
    assert "onRunTests" in src or "runTests" in src, (
        "the control must be wired to a run action")


def test_page_load_does_not_request_a_run():
    src = _read(_WEB / "pages" / "TaskDetailPage.tsx")
    assert "runTests" in src, "the page must pass a run action down to the tab"
    # The INITIAL fetch must stay run=false — reintroducing run=true on load is
    # the 30-60s hang this ticket must not resurrect.
    assert "/tests?project=" in src, "initial load must remain the cheap read"
