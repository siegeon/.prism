"""Regression test for the module-level observer-registry leak (task
44342d64), and the mechanism meant to close it.

task_service._CREATE_OBSERVERS / _STATUS_OBSERVERS (task_service.py:133-183)
are process-global lists. Production populates them exactly once, from
main.py's lifespan calling task_mirror.install() (main.py:406-413) --
nothing on that path ever calls remove_create_observer /
remove_status_observer, because a live server is never supposed to
uninstall its own mirror. That is correct for production and wrong for a
test SUITE: any unit test that boots the real lifespan (quiet_boot,
conftest.py) or otherwise registers an observer through the real
add_create_observer/add_status_observer API leaves it sitting on the list
for every later test in the SAME process, so an UNRELATED task creation in
some other file can trip a completely unrelated observer -- the symptom
pinned separately in
tests/unit/test_switch_on_pushes_backlog.py::test_done_and_cancelled_backlog_is_never_exported.

The fix (mirroring _integration_adapter_registry_isolation,
tests/conftest.py:136-157) is meant to be ONE autouse fixture in the ROOT
conftest.py that snapshots and restores both lists around every test in the
suite. This file proves that mechanism DIRECTLY: test_a below registers a
real observer through the real API and never cleans it up -- no
try/finally, no local isolation fixture of its own -- and test_b, an
unrelated test that runs afterward in the same pytest process (plain
declaration order; no reordering plugin is installed in this project),
asserts the leaked observer is gone and does not fire. If a future "fix"
adds a 4th/5th per-file local autouse fixture instead of the shared
conftest one, this file's own two tests still fail, because this file
carries no such local fixture -- the reset has to come from the root
conftest boundary, not from this file's own discipline.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import task_service  # noqa: E402

_LEAK_CALLS: list = []


def _leaked_create_observer(project, task) -> None:
    """A REAL create observer, registered through the real
    add_create_observer() API -- structurally identical to what
    task_mirror.install() registers from main.py's lifespan (task
    27e543e0). Deliberately left registered by test_a with no uninstall,
    to reproduce the actual production leak path rather than a synthetic
    one."""
    _LEAK_CALLS.append((project, getattr(task, "id", None)))


def test_a_registers_a_create_observer_and_never_uninstalls_it():
    _LEAK_CALLS.clear()
    added = task_service.add_create_observer(_leaked_create_observer)
    assert added, "expected a fresh registration in this pytest process"
    assert _leaked_create_observer in task_service.create_observers(), (
        "sanity: the observer must actually be registered before the "
        "next test can prove it stops firing")
    # Intentionally NO remove_create_observer / try-finally here -- that
    # is the point. Isolation must come from the conftest boundary, not
    # from this test's own cleanup discipline.


def test_b_a_later_unrelated_create_does_not_fire_the_leaked_observer(tmp_path):
    """Runs after test_a in the SAME pytest process. If the root
    conftest.py fixture snapshotted _CREATE_OBSERVERS before test_a ran
    and restored that snapshot when test_a tore down, this file's own
    registry must already be clean here -- before this test does
    anything of its own."""
    assert _leaked_create_observer not in task_service.create_observers(), (
        "test_a's observer leaked into this test -- "
        "task_service._CREATE_OBSERVERS was not reset at the test "
        "boundary (expected a root conftest.py autouse fixture to do "
        "this, mirroring _integration_adapter_registry_isolation)")

    svc = task_service.TaskService(
        db_path=str(tmp_path / "tasks.db"), project="prism")
    svc.create(title="an unrelated task, nothing to do with the mirror")

    assert _LEAK_CALLS == [], (
        "an unrelated TaskService.create() fired an observer that a "
        f"prior, unrelated test registered and never removed: {_LEAK_CALLS}")
