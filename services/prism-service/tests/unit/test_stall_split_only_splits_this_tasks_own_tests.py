"""The stall splitter may only create children for THIS task's own tests.

Live failure, task 72ccaf94 on 2026-08-29. Its `completion_proof` was a
verify_green_state report that mentioned ten pytest node ids from suites the
slice never touched -- test_auto_updater, test_lifespan_lock_recovery,
test_pidfile_lifecycle, test_task_page_bundle -- because the step had noted
which unrelated suites were already failing on the branch. `_handle_stall`
read that task-level field, made a child per id, and then made them AGAIN on
the next stall. The board reached 30 children in three identical sets of ten,
none of which could ever move the parent. The owner's words: "you need to
finish this task before you make more tasks, this is what will cause the
system to crash."

Two rules, pinned here:
  1. When a task pins a suite in `verify`, a child may only be created for a
     test id belonging to that suite.
  2. Splitting twice must not duplicate the board -- an id already covered by
     an OPEN child is skipped.

A task that pins NOTHING keeps the original split-everything behaviour; that
contract is exercised by test_task_runner_stall_detection.py and is not
superseded here.
"""

from prism_service.services import task_runner as tr

_PROOF = (
    "Ran the full suite. Pre-existing failures unrelated to this slice:\n"
    "  tests/unit/test_auto_updater.py::test_apply_update_refuses_in_docker\n"
    "  tests/unit/test_lifespan_lock_recovery.py::test_lifespan_starts_threads\n"
    "  tests/unit/test_task_page_bundle.py::test_task_page_chunk_is_smaller\n"
    "This slice's own red test:\n"
    "  tests/unit/test_brain_fts_no_orphans_on_reindex.py::test_stale_term\n")


class _Task:
    def __init__(self, **kw):
        self.id = kw.get("id", "72ccaf94")
        self.completion_proof = kw.get("completion_proof", _PROOF)
        self.verify = kw.get("verify", [])
        self.priority = 95
        self.tags = []
        self.status = kw.get("status", "in_progress")


class _Svc:
    """Minimal TaskService stand-in: records what the splitter creates."""

    def __init__(self, parent, children=None):
        self.task = parent
        self._children = list(children or [])
        self.created = []
        self.updated = {}

    def get(self, _id):
        return self.task

    def list(self, parent_id=None, **_kw):
        return list(self._children)

    def create(self, **kw):
        child = _Task(id=f"child-{len(self.created)}", verify=kw.get("verify", []))
        child.title = kw.get("title", "")
        self.created.append(child)
        self._children.append(child)
        return child

    def update(self, _id, **kw):
        self.updated.update(kw)

    def record_history(self, *_a, **_kw):
        pass


def _stall(svc):
    return tr._handle_stall(svc, svc.task.id, "implement_tasks")


def test_only_this_tasks_pinned_suite_becomes_a_child(monkeypatch):
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _t: False)
    parent = _Task(verify=[
        "services/prism-service/tests/unit/test_brain_fts_no_orphans_on_reindex.py"])
    svc = _Svc(parent)
    _stall(svc)
    made = [v for c in svc.created for v in c.verify]
    assert made == [
        "tests/unit/test_brain_fts_no_orphans_on_reindex.py::test_stale_term"], (
        "only the pinned suite's test may become a child; got "
        f"{made!r} -- the proof also named auto_updater/lifespan/task_page "
        "ids that belong to no suite this task pins")
    for junk in ("auto_updater", "lifespan", "task_page"):
        assert not any(junk in v for v in made), f"{junk} leaked into a child"


def test_a_second_stall_does_not_duplicate_existing_children(monkeypatch):
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _t: False)
    pinned = ("services/prism-service/tests/unit/"
              "test_brain_fts_no_orphans_on_reindex.py")
    parent = _Task(verify=[pinned])
    already = _Task(id="existing", verify=[
        "tests/unit/test_brain_fts_no_orphans_on_reindex.py::test_stale_term"])
    already.status = "pending"
    svc = _Svc(parent, children=[already])
    _stall(svc)
    assert svc.created == [], (
        "an id an OPEN child already covers must not be split again; "
        f"created {[c.verify for c in svc.created]!r}")


def test_a_cancelled_child_does_not_block_a_fresh_split(monkeypatch):
    """Cancelled/done children are not live coverage, so the id is free."""
    monkeypatch.setattr(tr, "_stall_work_is_shipped", lambda _t: False)
    pinned = ("services/prism-service/tests/unit/"
              "test_brain_fts_no_orphans_on_reindex.py")
    dead = _Task(id="dead", verify=[
        "tests/unit/test_brain_fts_no_orphans_on_reindex.py::test_stale_term"])
    dead.status = "cancelled"
    svc = _Svc(_Task(verify=[pinned]), children=[dead])
    _stall(svc)
    assert len(svc.created) == 1, (
        "a cancelled child leaves the test uncovered, so the split must "
        "still happen")
