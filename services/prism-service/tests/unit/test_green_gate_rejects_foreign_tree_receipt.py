"""The green-gate machine seat must refuse a receipt measured on a tree that
does not contain the task's own pinned tests (task e0149f1f).

Regression: task 5a6837a0 reached status=done on
`adapter=http_probe, tree=c162b66` — a commit belonging to task 89e90d1a —
because its per-task scratch worktree was never advanced to the lane's work.
The receipt tooth compared the receipt's tree to the WORKTREE's tree, so both
agreed on the wrong tree and the gate closed on evidence that never saw the
code under review.
"""
from types import SimpleNamespace

from prism_service.services.conductor_service import ConductorService


def _task(verify):
    return SimpleNamespace(id="t1", verify=verify)


def test_pinned_paths_extracted_from_bare_paths():
    assert ConductorService.pinned_test_paths(
        _task(["services/prism-service/tests/unit/test_a.py"])
    ) == ["services/prism-service/tests/unit/test_a.py"]


def test_pinned_paths_extracted_from_full_commands():
    # task.verify is sometimes a whole command, not a path.
    got = ConductorService.pinned_test_paths(
        _task(["python -m pytest services/prism-service/tests/unit/test_a.py "
               "services/prism-service/tests/integration/test_b.py"]))
    assert got == ["services/prism-service/tests/integration/test_b.py",
                   "services/prism-service/tests/unit/test_a.py"]


def test_pinned_paths_survive_windows_separators():
    got = ConductorService.pinned_test_paths(
        _task([r"services\prism-service\tests\unit\test_a.py"]))
    assert got == ["services/prism-service/tests/unit/test_a.py"]


def test_no_pinned_tests_is_not_this_tooths_business():
    svc = ConductorService.__new__(ConductorService)
    assert svc._receipt_tree_missing_reason(
        _task([]), SimpleNamespace(tree_sha="deadbeef")) is None


def test_receipt_without_a_tree_is_not_this_tooths_business():
    svc = ConductorService.__new__(ConductorService)
    assert svc._receipt_tree_missing_reason(
        _task(["services/prism-service/tests/unit/test_a.py"]),
        SimpleNamespace(tree_sha="")) is None


def _git(cwd, *args):
    import subprocess
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=True).stdout.strip()


def _repo_with_two_trees(tmp_path):
    """A real repo: commit ONE has no test file, commit TWO adds the pinned
    test. Mirrors the 5a6837a0 shape — a worktree parked on an older commit
    from other work, and the lane's real commit further along."""
    import os
    r = tmp_path / "repo"
    (r / "services/prism-service/tests/unit").mkdir(parents=True)
    _git(r.parent, "init", "-q", str(r))
    _git(r, "config", "user.email", "t@t"); _git(r, "config", "user.name", "t")
    (r / "other.txt").write_text("from another task\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "foreign task commit")
    foreign = _git(r, "rev-parse", "HEAD")
    (r / "services/prism-service/tests/unit/test_pinned.py").write_text(
        "def test_x():\n    assert True\n")
    _git(r, "add", "-A"); _git(r, "commit", "-qm", "the lane's real work")
    return r, foreign, _git(r, "rev-parse", "HEAD")


def test_foreign_tree_is_REFUSED_with_an_actionable_reason(tmp_path, monkeypatch):
    from prism_service.services import task_workspace
    repo, foreign, _real = _repo_with_two_trees(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    svc = ConductorService.__new__(ConductorService)
    reason = svc._receipt_tree_missing_reason(
        _task(["services/prism-service/tests/unit/test_pinned.py"]),
        SimpleNamespace(tree_sha=foreign))
    assert reason, "a tree lacking the pinned test must be REFUSED"
    assert foreign[:12] in reason and "test_pinned.py" in reason
    assert "distinct actor" in reason


def test_sound_tree_still_APPROVES(tmp_path, monkeypatch):
    # The tooth must NARROW machine adjudication, not disable it.
    from prism_service.services import task_workspace
    repo, _foreign, real = _repo_with_two_trees(tmp_path)
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda _tid: {"path": str(repo)})
    svc = ConductorService.__new__(ConductorService)
    assert svc._receipt_tree_missing_reason(
        _task(["services/prism-service/tests/unit/test_pinned.py"]),
        SimpleNamespace(tree_sha=real)) is None


# ---------------------------------------------------------------------
# AC-2 / AC-5: the ADAPTER must match what the task pins. 5a6837a0 closed
# on adapter=http_probe while its task.verify pinned a pytest file — an
# http probe returning ok says NOTHING about that suite.
# ---------------------------------------------------------------------

PINNED = ["services/prism-service/tests/unit/test_a.py"]


def _svc():
    return ConductorService.__new__(ConductorService)


def test_http_probe_receipt_on_a_pytest_pinned_task_is_REFUSED():
    from prism_service.services import oracle_spec as osp
    reason = _svc()._receipt_adapter_mismatch_reason(
        _task(PINNED), SimpleNamespace(adapter=osp.ADAPTER_HTTP))
    assert reason, "an http probe cannot evidence a pinned pytest suite"
    assert osp.ADAPTER_HTTP in reason and osp.ADAPTER_PYTEST in reason, \
        "the reason must name BOTH what was pinned and what was measured"


def test_browser_receipt_on_a_pytest_pinned_task_is_REFUSED():
    from prism_service.services import oracle_spec as osp
    assert _svc()._receipt_adapter_mismatch_reason(
        _task(PINNED), SimpleNamespace(adapter=osp.ADAPTER_BROWSER))


def test_matching_pytest_adapter_still_APPROVES():
    # Anti-misfire (AC-3): the tooth narrows, it does not disable.
    from prism_service.services import oracle_spec as osp
    assert _svc()._receipt_adapter_mismatch_reason(
        _task(PINNED), SimpleNamespace(adapter=osp.ADAPTER_PYTEST)) is None


def test_task_pinning_no_pytest_files_is_not_this_tooths_business():
    # A probe-oracle task keeps machine adjudication untouched.
    from prism_service.services import oracle_spec as osp
    assert _svc()._receipt_adapter_mismatch_reason(
        _task([]), SimpleNamespace(adapter=osp.ADAPTER_HTTP)) is None


def test_adjudicator_calls_BOTH_teeth_before_approving():
    """AC-5: a refusal must return out of the approve path, never fall
    through to a write. Pin that both reasons are consulted by the
    adjudicate path itself, not merely defined."""
    import inspect
    src = inspect.getsource(ConductorService.adjudicate_green_gate)
    assert "_receipt_tree_missing_reason" in src
    assert "_receipt_adapter_mismatch_reason" in src


# ---------------------------------------------------------------------
# AC-6: the refusal must be RECORDED, not computed and thrown away. A gate
# that parks with an empty gate_reason cannot be self-diagnosed by the
# driver and pings a human at every gate.
# ---------------------------------------------------------------------

class _RecordingTaskSvc:
    def __init__(self, gate_reason=""):
        self.updates, self.history = [], []
        self._task = SimpleNamespace(id="t1", gate_reason=gate_reason)

    def get(self, _tid):
        return self._task

    def update(self, tid, **kw):
        self.updates.append((tid, kw))

    def record_history(self, tid, **kw):
        self.history.append((tid, kw))


def test_a_refusal_RECORDS_its_reason_and_stays_pending():
    svc = _svc()
    svc._task_svc = _RecordingTaskSvc()
    svc._park_green_refusal("t1", "receipt measured at tree abc123, which "
                                  "does NOT contain this task's pinned test")
    assert svc._task_svc.updates, "the refusal reason must be RECORDED"
    _tid, kw = svc._task_svc.updates[0]
    assert kw["gate_state"] == "pending", "a refusal NEVER writes passed"
    assert "abc123" in kw["gate_reason"]
    assert svc._task_svc.history, "the refusal must leave an audit row"


def test_an_unchanged_refusal_does_not_respam_history():
    """The seat re-sweeps every 20s; the same reason must not append a
    history row on every pass."""
    reason = "receipt measured at tree abc123, which does NOT contain"
    svc = _svc()
    svc._task_svc = _RecordingTaskSvc(gate_reason=reason)
    svc._park_green_refusal("t1", reason)
    assert not svc._task_svc.history


def test_adjudicator_records_the_refusal_for_both_teeth():
    import inspect
    src = inspect.getsource(ConductorService.adjudicate_green_gate)
    assert src.count("_park_green_refusal") >= 2, \
        "both the tree tooth and the adapter tooth must record their reason"
