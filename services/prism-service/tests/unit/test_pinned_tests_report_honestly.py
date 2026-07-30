"""RED — pinned-test discovery/execution reports what actually ran (task
9f3c57dc).

Owner 2026-07-29, at task ae67ed5c's green gate: "if the tests are not
green, then we are not awaiting a green gate". GET /api/tasks/{id}/tests?
run=true showed "0 / 8" with a fresh timestamp while the 6 real pinned tests
PASSED at the tree under review. Nothing had failed; nothing had RUN, and
the response could not tell the difference.

Three independent measurement defects, pinned here (api/tasks.py):
  1. _run_pinned_tests returns {} (not None) when pytest COLLECTS ZERO
     tests, so a collection failure is indistinguishable from "we ran it,
     0 passed" (AC-1, AC-3).
  2. get_task_tests unions pinned files from TWO roots (service checkout +
     task worktree) but historically executed the whole batch in ONE fixed
     root, so a single file that lives only in the OTHER root aborted
     collection for every file in the batch (AC-1, AC-2).
  3. file_owns_task attributed a file to whichever task id was merely FIRST
     mentioned in its module docstring, even when that mention was prose
     provenance ("reviewing task X's gate") rather than a declared owner
     (AC-4, AC-5). commit ce1c3c4 hand-patched the one offending file; the
     mechanism was untouched.

Imports live INSIDE the tests so the file collects and fails at runtime
(the red gate wants rc==1, never a collection error).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _new_project(tmp_path, monkeypatch, name):
    """A clean project namespace + task, isolated per test (mirrors the
    `pinned` fixture in tests/integration/test_task_test_badges.py)."""
    from prism_service import config, project_context

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    project_context._contexts.clear()
    ctx = project_context.get_project(name)
    return ctx


def _fake_ws(tmp_path, rel_dir: str, filename: str, body: str) -> Path:
    """Write a test file under tmp_path/ws/services/prism-service/<rel_dir>/
    and return the ws root path (what workspace_for()["path"] would be)."""
    ws = tmp_path / "ws"
    tests_dir = ws / "services" / "prism-service" / rel_dir
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / filename).write_text(body, encoding="utf-8")
    return ws


# ── AC-1: an in-flight, worktree-only pinned file reports its REAL count ──

def test_in_flight_worktree_only_test_reports_real_pass_count(tmp_path, monkeypatch):
    """No mocking of _run_pinned_tests here — TWO REAL pytest subprocesses
    run against TWO real files: one lives only in the checkout root (a
    sibling test already merged), one lives only in the task's worktree
    (the in-flight case). Before the fix both files were pooled into ONE
    _run_pinned_tests(one_root, both_files) call; the file absent from
    that one root made pytest abort collection ('file or directory not
    found') for BOTH, so even the file that would have passed alone was
    stamped 0 / N. This is the exact 6/8 -> 0/8 field failure, at 2 tests."""
    from prism_service.api import tasks as tasks_api
    from prism_service.services import task_workspace

    ctx = _new_project(tmp_path, monkeypatch, "honestproj1")
    task = ctx.task_svc.create(title="honest pin task 1")

    ws = _fake_ws(
        tmp_path, "tests/unit", "test_honest_pin_worktree_side.py",
        f'"""Pin the gate tooth (task {task.id})."""\n\n\n'
        "def test_honest_pin_worktree_side():\n    assert True\n")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(ws), "baseline": "base"})

    checkout = tmp_path / "checkout1"
    (checkout / "tests" / "unit").mkdir(parents=True)
    (checkout / "tests" / "unit" / "test_honest_pin_checkout_side.py").write_text(
        f'"""Pin the gate tooth (task {task.id})."""\n\n\n'
        "def test_honest_pin_checkout_side():\n    assert True\n",
        encoding="utf-8")
    monkeypatch.setattr(tasks_api, "_checkout_tests_root",
                        lambda: checkout / "tests")

    payload = tasks_api.get_task_tests(task.id, run=True, project="honestproj1")

    assert payload["ran"] is True, (
        f"both files are real and collectable — expected ran=True, got {payload}")
    assert not payload.get("could_not_run"), (
        f"a real successful run is not could-not-run: {payload}")
    rows = {r["name"]: r for r in payload["tests"]}
    assert rows["test_honest_pin_worktree_side"]["status"] == "passed", (
        f"the worktree-side test must report its real pass: {payload}")
    assert rows["test_honest_pin_checkout_side"]["status"] == "passed", (
        f"the checkout-side test must report its real pass: {payload}")


# ── AC-2: one uncollectable root must not zero out another root's passes ──

def test_one_uncollectable_root_does_not_zero_out_another_roots_passes(
        tmp_path, monkeypatch):
    """Two DIFFERENT roots (checkout-side + worktree-side) each pin one file
    for this task. Before the fix, both files were handed to a SINGLE
    _run_pinned_tests(one_root, both_files) call, so a file absent from
    that one root aborted collection for BOTH. The fix must call
    _run_pinned_tests once PER ROOT, so a group that cannot run never drags
    down a group that can."""
    from prism_service.api import tasks as tasks_api
    from prism_service.services import task_workspace

    ctx = _new_project(tmp_path, monkeypatch, "honestproj2")
    task = ctx.task_svc.create(title="honest pin task 2")

    ws = _fake_ws(
        tmp_path, "tests/unit", "test_honest_pin_ws_side.py",
        f'"""Pin the gate tooth (task {task.id})."""\n\n\n'
        "def test_honest_pin_ws_side():\n    assert True\n")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(ws), "baseline": "base"})

    checkout = tmp_path / "checkout"
    (checkout / "tests" / "unit").mkdir(parents=True)
    (checkout / "tests" / "unit" / "test_honest_pin_checkout_side.py").write_text(
        f'"""Pin the gate tooth (task {task.id})."""\n\n\n'
        "def test_honest_pin_checkout_side():\n    assert True\n",
        encoding="utf-8")
    monkeypatch.setattr(tasks_api, "_checkout_tests_root",
                        lambda: checkout / "tests")

    calls: list[tuple[str, list]] = []

    def fake_run(root, files):
        calls.append((str(root), list(files)))
        if str(root) == str(checkout):
            return None  # this root's file cannot be collected
        return {"test_honest_pin_ws_side": "passed"}

    monkeypatch.setattr(tasks_api, "_run_pinned_tests", fake_run)

    payload = tasks_api.get_task_tests(task.id, run=True, project="honestproj2")

    assert len(calls) == 2, (
        f"expected one _run_pinned_tests call PER ROOT (2), got {calls}")
    called_roots = {c[0] for c in calls}
    ws_run_root = ws / "services" / "prism-service"
    assert called_roots == {str(ws_run_root), str(checkout)}, calls
    for root, files in calls:
        assert len(files) == 1, (
            "each root's call must carry only ITS OWN file, never the "
            f"other root's — got {files} for root {root}")

    rows = {r["name"]: r for r in payload["tests"]}
    assert rows["test_honest_pin_ws_side"]["status"] == "passed", (
        "the worktree-side test's real pass must survive even though the "
        f"checkout-side group could not run: {payload}")
    assert payload["ran"] is True
    assert not payload.get("could_not_run"), (
        "at least one group ran successfully, so this is not could-not-run")


# ── AC-3: a real collection failure must render as COULD NOT RUN, never 0/N ─

def test_zero_collection_reports_could_not_run_not_zero_of_n(tmp_path):
    """Part 1 — the exact owner repro, verbatim: a REAL pytest subprocess
    against a path that does not exist in service_root. pytest exits with
    'file or directory not found' / 'collected 0 items', so 0 PASSED/FAILED
    lines parse. That must be None, never {} (which the caller reads as a
    confident, arithmetically-false 0 / N)."""
    from prism_service.api.tasks import _run_pinned_tests

    empty_root = tmp_path / "runroot"
    (empty_root / "tests" / "unit").mkdir(parents=True)
    result = _run_pinned_tests(empty_root, ["tests/unit/test_does_not_exist.py"])
    assert result is None, (
        f"a collection failure must be None (could-not-run), got {result!r} "
        "— an empty dict is read by the caller as '0 tests, all failed'")


def test_zero_collection_reports_could_not_run_at_endpoint_level(
        tmp_path, monkeypatch):
    """Part 2 — the RESPONSE CONTRACT: when every group fails to run, the
    endpoint must say could_not_run=True and ran=False, never stamp a
    numeric 0 / N with a fresh timestamp."""
    from prism_service.api import tasks as tasks_api
    from prism_service.services import task_workspace

    ctx = _new_project(tmp_path, monkeypatch, "honestproj3")
    task = ctx.task_svc.create(title="honest pin task 3")

    ws = _fake_ws(
        tmp_path, "tests/unit", "test_honest_pin_three.py",
        f'"""Pin the gate tooth (task {task.id})."""\n\n\n'
        "def test_honest_pin_three():\n    assert True\n")
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: {"path": str(ws), "baseline": "base"})
    monkeypatch.setattr(tasks_api, "_run_pinned_tests", lambda *a, **k: None)

    payload = tasks_api.get_task_tests(task.id, run=True, project="honestproj3")

    assert payload["ran"] is False, (
        "no group produced a real status — ran must stay False")
    assert payload.get("could_not_run") is True, (
        f"a total run failure must be flagged could_not_run, got {payload}")
    rows = {r["name"]: r for r in payload["tests"]}
    assert rows["test_honest_pin_three"].get("status") in (None, "", "not-run"), (
        "must never fabricate a passed/failed status for a run that "
        f"never happened: {payload}")


# ── AC-4: a bare prose citation with NO declared owner attributes nobody ──

FOREIGN = "ae67ed5c-0000-4000-8000-000000000000"
OWNER = "9f3c57dc-fd90-4722-9dfa-ba221ada1c8d"


def test_prose_citation_without_declared_owner_does_not_attribute():
    """The exact ce1c3c4 shape: a docstring that CITES a foreign task id in
    prose ("reviewing task X's gate") with no "(task <id>)" declaration
    anywhere. A prose mention must never change what a gate measures."""
    from prism_service.api.tasks import file_owns_task

    src = (
        '"""Owner 2026-07-29, reviewing task %s\'s gate: the decision '
        'packet should say visual evidence.\n"""\n'
        "def test_x():\n    assert True\n" % FOREIGN
    )
    assert file_owns_task(src, FOREIGN) is False, (
        "a bare prose citation with no declared '(task <id>)' owner must "
        "never attribute the file to the cited task")


# ── AC-5: the declared "(task <id>)" owner still wins over a later citation ─

def test_declared_owner_convention_still_wins_over_later_citation():
    """No regression of task e0149f1f's fix: a DECLARED owner named first
    with "(task <id>)" still owns the file even when another task is cited
    afterward in prose."""
    from prism_service.api.tasks import file_owns_task

    src = (
        '"""Pin the gate tooth (task %s).\n\n'
        'Regression: task %s closed on a foreign tree.\n"""\n'
        "def test_x():\n    assert True\n" % (OWNER, FOREIGN)
    )
    assert file_owns_task(src, OWNER) is True
    assert file_owns_task(src, FOREIGN) is False
