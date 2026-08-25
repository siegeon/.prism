"""EXIT-half reachability tooth for green_gate (task c944cac2).

Counterpart to the AUTHORING-time check (oracle_authoring._unwired_reason,
task 597839d9): that one refuses at task-creation when allowed_files names
no existing production module. THIS tooth refuses at green_gate, reading the
task's real worktree GIT DIFF (never allowed_files -- measured inert on
48/48 active tasks), and it is SYMBOL-level: a NEW production entry point
(a method/function the diff adds) needs a non-test production REFERENCE
somewhere else in the tree, not just a module that is merely imported.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True)


def _init_repo(tmp_path):
    """A throwaway git repo with one baseline commit, so the diff-based
    check has a `baseline` rev to compare the fixture edits against."""
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-q", "-m", "baseline"], tmp_path)
    sha = _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()
    return sha


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Diff-driven reachability: new production entry points added by the WORKING
# TREE diff against `baseline` need a non-test caller elsewhere in the tree.
# `unreachable_entry_point_reason_for_diff(workspace, baseline)` is the
# testable core (git-level, no allowed_files, no task_workspace lookup).
# ---------------------------------------------------------------------------

def test_new_entry_point_without_any_production_caller_is_refused(tmp_path):
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def register(self, adapter):\n"
          "        self._adapter = adapter\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason, (
        "a new .register() with zero production callers must be refused")
    assert "register" in reason
    assert "mod.py" in reason


def test_new_entry_point_with_a_production_caller_is_not_refused(tmp_path):
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def register(self, adapter):\n"
          "        self._adapter = adapter\n")
    _write(tmp_path / "pkg" / "wire.py",
          "from pkg.mod import Thing\n"
          "t = Thing()\n"
          "t.register(object())\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason == "", (
        f"a real production caller (pkg/wire.py) must clear the tooth: "
        f"{reason!r}")


def test_same_file_caller_is_a_production_reference(tmp_path):
    """task 3baadd19 AC-5 (_SpendTracker.charge): a new symbol whose only
    caller is a SIBLING function in the SAME module -- the ordinary way to
    write a private helper -- must NOT be refused. The old code excluded
    the defining file from the search entirely, so this pattern always
    false-positived as unreachable."""
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    # `_use_it` is prefixed so it is exempt from its OWN reachability check
    # (this fixture's point is `register`'s same-file caller, not a second
    # new public symbol that would need its own caller too).
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def register(self, adapter):\n"
          "        self._adapter = adapter\n"
          "\n"
          "\n"
          "def _use_it():\n"
          "    t = Thing()\n"
          "    t.register(object())\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason == "", (
        f"a caller in the SAME file as the definition must clear the "
        f"tooth: {reason!r}")


def test_symbols_own_def_line_does_not_satisfy_its_own_reference_check(
        tmp_path):
    """The same-file scan must exclude the symbol's own `def` line --
    otherwise every new symbol would trivially reference itself and this
    tooth would never refuse anything."""
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def register(self, adapter):\n"
          "        self._adapter = adapter\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason, (
        "a symbol with NO caller anywhere -- not even a real call in its "
        "own file -- must still be refused")
    assert "register" in reason


def test_module_wired_but_symbol_unreferenced_is_still_refused(tmp_path):
    """The EXACT attempt-1 shape (work_item_sync.py, 0665c829/d09bee0b/
    7362c14e): the module IS imported/constructed elsewhere (module-level
    'wired'), but its one NEW entry point still has no caller. A module-level
    check (is this file imported anywhere) sees `wire.py` and stays quiet;
    this tooth must still refuse on the symbol."""
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def existing(self):\n"
          "        return 1\n\n"
          "    def register(self, adapter):\n"
          "        self._adapter = adapter\n")
    _write(tmp_path / "pkg" / "wire.py",
          "from pkg.mod import Thing\n"
          "t = Thing()\n"
          "t.existing()\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason, (
        "Thing is constructed and .existing() is called, but .register() "
        "still has no caller -- a module-level check would wrongly pass "
        "this; the tooth must be symbol-level")
    assert "register" in reason


# ---------------------------------------------------------------------------
# Tolerated false-positive sources (plan_doc): a naive "no explicit call
# site" grep would wrongly refuse each of these legitimate shapes.
# ---------------------------------------------------------------------------

def test_fastapi_route_decorator_entry_point_is_tolerated(tmp_path):
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "routes.py",
          "from fastapi import APIRouter\n"
          "router = APIRouter()\n\n"
          "@router.post('/widgets')\n"
          "def create_widget(payload):\n"
          "    return payload\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason == "", (
        f"a route handler is called by the framework via its decorator, "
        f"not by an explicit call site -- must not be refused: {reason!r}")


def test_dynamic_dispatch_by_string_name_is_tolerated(tmp_path):
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def handle_widget(self):\n"
          "        return 'ok'\n")
    _write(tmp_path / "pkg" / "dispatch.py",
          "ACTION_TABLE = {\n"
          "    'handle_widget': 'widget-created',\n"
          "}\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason == "", (
        f"getattr(obj, 'handle_widget')-style dispatch references the "
        f"symbol only as a string; must not be refused: {reason!r}")


def test_reference_only_from_the_spa_is_tolerated(tmp_path):
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "mod.py",
          "class Thing:\n"
          "    def export_report(self):\n"
          "        return {}\n")
    _write(tmp_path / "web" / "src" / "Api.tsx",
          "export async function exportReport() {\n"
          "  return fetch('/api/export_report');\n"
          "}\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason == "", (
        f"a route reached only from the SPA's fetch string must not be "
        f"refused: {reason!r}")


# ---------------------------------------------------------------------------
# Real-repo grounding (task 27e543e0's worked example): the module-level
# helper run against ACTUAL production files, no synthetic repo. This is
# what "the tooth catches the symbol shape" and "does not false-refuse the
# real wired shape" mean concretely -- both facts verified live in this
# session (git grep), not assumed.
# ---------------------------------------------------------------------------

def test_real_repo_workitemsync_register_has_no_production_caller():
    """Grounds the actual live defect: `grep -rn '\\.register\\(' prism_service`
    (this session) returns only atexit.register in main.py -- WorkItemSync's
    own .register() has never had a production caller."""
    from prism_service.services.reachability_check import (
        _has_production_reference)

    found = _has_production_reference(
        "register",
        defining_file="prism_service/services/work_item_sync.py",
        repo_root=_SERVICE_ROOT)
    assert found is False, (
        "WorkItemSync.register() has no production caller today -- if this "
        "now returns True, the real bug was independently fixed and this "
        "grounding fact needs re-verifying, not silencing")


def test_real_repo_task_mirror_install_has_a_production_caller():
    """Grounds the tolerated wired shape (27e543e0's AC-1 wiring group):
    main.py:478 calls `task_mirror.install()` in the real lifespan -- this
    is exactly the wiring group's own claim, read from source, not assumed."""
    from prism_service.services.reachability_check import (
        _has_production_reference)

    found = _has_production_reference(
        "install",
        defining_file="prism_service/services/task_mirror.py",
        repo_root=_SERVICE_ROOT)
    assert found is True, (
        "task_mirror.install() is called from main.py's lifespan in "
        "production; the tooth must not false-refuse this real wired shape")


def test_task_with_no_real_workspace_is_not_refused(tmp_path):
    """A synthetic task built straight from TaskService (no real conductor
    worktree ever created for it -- exactly the shape every existing
    test_gate_adjudicator*.py fixture uses) must abstain, not refuse: a
    validator that blocks work it cannot even inspect gets routed around
    (oracle_authoring.py's own doctrine) and would break all 11 neighbouring
    tests attempt 1 broke."""
    from prism_service.services.task_service import TaskService
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason)

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    task = task_svc.create(title="no worktree behind this one")

    reason = unreachable_entry_point_reason(task)
    assert reason == "", (
        f"a task with no resolvable workspace must never be refused: "
        f"{reason!r}")


# ---------------------------------------------------------------------------
# Wired into the machine seat: `ConductorService.adjudicate_green_gate` is
# the ONE chokepoint (mirrors test_gate_adjudicator.py's own doctrine
# comment). A truthy reachability refusal must PARK pending WITH the reason
# (never flip to failed, never strand an empty gate_reason); an empty
# reachability reason must not block an otherwise-passing gate.
# ---------------------------------------------------------------------------

_HTTP_ORACLE = "GET http://127.0.0.1:9/health returns 200 with the current build info"


def _gated_task(tmp_path, oracle=_HTTP_ORACLE, proof_type=""):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="adjudicate me", oracle=oracle,
                        proof_type=proof_type)
    # Runner+pass signal in completion_proof so the PRE-EXISTING, unrelated
    # green_gate_artifact_reason tooth (conductor_service.py) clears -- these
    # tests validate the NEW reachability tooth, not that older artifact-
    # proof requirement.
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    completion_proof="pytest run: 3 passed, 0 failed")
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc):
    from prism_service.services.conductor_service import ConductorService
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    return cond


def test_unwired_fixture_slice_parks_at_green_gate_naming_the_symbol(
        tmp_path, monkeypatch):
    """The deliberately-unwired fixture shape this task's oracle asks for:
    the reachability tooth fires, and the gate must PARK pending with a
    gate_reason naming the missing production construction site -- never
    silently flip to failed (CLAUDE.md: a computed-and-dropped reason is
    only half shipped)."""
    from prism_service.services import reachability_check
    task_svc, task = _gated_task(tmp_path)
    cond = _conductor(tmp_path, task_svc)

    monkeypatch.setattr(
        reachability_check, "unreachable_entry_point_reason",
        lambda t: ("unreachable entry point: Thing.register in "
                   "pkg/mod.py has no non-test production caller"))

    res = cond.adjudicate_green_gate(task.id)
    assert res is None, (
        "a refused reachability check must never auto-approve the gate")
    after = task_svc.get(task.id)
    assert after.gate_state == "pending", (
        f"must park pending, never failed: got {after.gate_state!r}")
    assert "register" in (after.gate_reason or ""), (
        f"gate_reason must name the missing construction site: "
        f"{after.gate_reason!r}")


def test_reachable_slice_is_not_blocked_by_the_new_tooth(tmp_path, monkeypatch):
    """A wired slice (the 27e543e0 shape: empty reachability reason) must
    still reach machine approval -- the new tooth is a PRE-FLIGHT that only
    ever narrows, never blocks the receipt tooth's own approve path."""
    from prism_service.services import oracle_spec as osp
    from prism_service.services import reachability_check
    task_svc, task = _gated_task(tmp_path)
    cond = _conductor(tmp_path, task_svc)

    monkeypatch.setattr(
        reachability_check, "unreachable_entry_point_reason", lambda t: "")

    class _R:
        adapter = osp.ADAPTER_HTTP
        job_id = "fresh-pass"
        tree_sha = "deadbeef"
        spec_hash = "sha256:fake"
        passed = True
        status = "passed"
        policy_hash = ""
        # Runner+pass signal so this double clears the PRE-EXISTING,
        # unrelated green_gate_artifact_reason tooth (conductor_service.py)
        # -- this test validates the NEW reachability tooth's non-
        # interference, not that older artifact-proof requirement.
        reason = "pytest run: 3 passed, 0 failed (test double)"
    monkeypatch.setattr(cond, "_oracle_receipt_refusal",
                        lambda *a, **k: ("", _R()))

    res = cond.adjudicate_green_gate(task.id)
    assert res is not None and res.get("ok") is True, (
        f"an empty reachability reason must not block an otherwise-passing "
        f"gate: {res}")
    assert task_svc.get(task.id).gate_state == "passed"
