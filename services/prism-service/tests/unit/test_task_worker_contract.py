"""Worker contract (goalbuddy state.yaml T003) — allowed_files / verify / stop_if
and the parallel-safety check (ported from goalbuddy parallel-plan.mjs).
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


def test_contract_fields_default_empty(tmp_path):
    t = _svc(tmp_path).create(title="x")
    assert t.allowed_files == [] and t.verify == [] and t.stop_if == []


def test_contract_fields_round_trip(tmp_path):
    svc = _svc(tmp_path)
    t = svc.create(
        title="x",
        allowed_files=["a.py", "b.py"],
        verify=["pytest tests/unit/test_x.py"],
        stop_if=["Need files outside allowed_files."],
    )
    g = svc.get(t.id)
    assert g.allowed_files == ["a.py", "b.py"]
    assert g.verify == ["pytest tests/unit/test_x.py"]
    assert g.stop_if == ["Need files outside allowed_files."]
    svc.update(t.id, allowed_files=["a.py", "b.py", "c.py"])
    assert svc.get(t.id).allowed_files == ["a.py", "b.py", "c.py"]


def test_disjoint_allowlists_are_parallel_safe():
    from prism_service.services.conductor_service import (
        can_run_parallel, overlapping_allowed_files,
    )
    safe = [["a.py", "b.py"], ["c.py"], ["d.py", "e.py"]]
    assert can_run_parallel(safe) is True
    assert overlapping_allowed_files(safe) == set()


def test_overlapping_allowlists_are_not_parallel_safe():
    from prism_service.services.conductor_service import (
        can_run_parallel, overlapping_allowed_files,
    )
    clash = [["a.py", "shared.py"], ["c.py"], ["shared.py"]]
    assert can_run_parallel(clash) is False
    assert overlapping_allowed_files(clash) == {"shared.py"}
