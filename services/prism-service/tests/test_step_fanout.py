"""Per-step fanout telemetry — set_step_fanout UPSERT + phase_progress basis.

The fanout signal counts EPHEMERAL sub-agent units dispatched vs returned for
the CURRENT workflow step (e.g. "8 test-writers handed out, 3 back") — distinct
from phase_progress' CHILD-TASK 'children' basis. With no child tasks (as for
write_failing_tests) fanout must win over the wall-clock 'time' estimate.
"""


def _cond(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    scores_db = str(tmp_path / "scores.db")
    task_svc = TaskService(str(tmp_path / "tasks.db"), scores_db=scores_db)
    cond = ConductorService(scores_db, enable_engine=False, task_svc=task_svc)
    return cond, task_svc


def test_set_step_fanout_upserts_and_phase_progress_prefers_fanout(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="fanout task")
    task_svc.update(t.id, workflow_step="write_failing_tests")

    row = cond.set_step_fanout(t.id, "write_failing_tests", 8, 3)
    assert row["dispatched"] == 8 and row["returned"] == 3

    pp = cond.phase_progress(t.id)
    assert pp["fanout_dispatched"] == 8
    assert pp["fanout_returned"] == 3
    assert pp["basis"] == "fanout"
    assert abs(pp["pct"] - 3 / 8) < 1e-6


def test_set_step_fanout_upsert_overwrites(tmp_path):
    cond, task_svc = _cond(tmp_path)
    t = task_svc.create(title="fanout upsert")
    task_svc.update(t.id, workflow_step="write_failing_tests")

    cond.set_step_fanout(t.id, "write_failing_tests", 8, 3)
    row = cond.set_step_fanout(t.id, "write_failing_tests", 8, 8)
    assert row["dispatched"] == 8 and row["returned"] == 8
    assert cond._step_fanout(t.id, "write_failing_tests") == (8, 8)
