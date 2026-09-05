"""A repair workspace reconstructs the bytes the failed run actually saw.

Epic 4e6e7417 ("Ship snapshot-backed repair workspaces"). Its oracle: PRISM
can validate a deliberately DIRTY source tree, request a repair for a failed
step, and prove the resulting conductor workspace carries the identical
recorded snapshot fingerprint and file contents.

THE INCIDENT THIS CLOSES: a repair worktree built from ambient committed
HEAD validated files that were absent from -- or different in -- the run it
was supposed to repair, because HEAD had moved on since. The fix is that the
snapshot recorded WITH the run is the source of truth at repair time.

Drives the REAL shipped path end to end against a real git repository:
source_snapshot.capture_source_snapshot writes and pins a commit for the
dirty tree, and api.workflows.queue_workflow_fix rebuilds the workspace at
that commit via task_workspace.ensure_workspace. Nothing about the snapshot
is faked -- only the workflow ENGINE (a separate service) is stubbed, and it
is stubbed with the models' own validated wire shape.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from prism_service.api import workflows as wf
from prism_service.services import task_workspace
from prism_service.services.source_snapshot import capture_source_snapshot

INSTANCE = "run-b51700f6"
DIRTY_TRACKED = "what the failed run actually saw\n"
DIRTY_UNTRACKED = "untracked input the run used\n"


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


class _TaskSvc:
    def __init__(self):
        self.created, self.updated = [], []

    def create(self, **kw):
        self.created.append(kw)
        return types.SimpleNamespace(id=f"task-{len(self.created)}",
                                     status="pending")

    def update(self, tid, **kw):
        self.updated.append((tid, kw))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("original\n", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    monkeypatch.setattr(task_workspace, "resolve_data_dir",
                        lambda: tmp_path / "data")
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(root))
    return root


@pytest.fixture
def svc(monkeypatch):
    s = _TaskSvc()
    monkeypatch.setattr(wf, "get_project",
                        lambda p: types.SimpleNamespace(task_svc=s))
    return s


def _wire_engine(monkeypatch, snapshot):
    def _engine(path, **_kw):
        if path.startswith("/workflows/instances/"):
            return {"data": {"project": "prism", "sourceSnapshot": snapshot,
                             "tests": {"status": "failed", "exitCode": 1,
                                       "output": "FAILED tests/unit/test_x.py::test_y - boom\n"
                                                 "1 failed in 0.10s\n"}}}
        if path.startswith("/workflows/definitions/"):
            return {"id": "validation", "name": "Build and test",
                    "description": "Build and test the project",
                    "project": "prism", "projectType": "python",
                    "steps": [{"id": "test", "title": "Run tests",
                               "purpose": "Run the project's test suite",
                               "runner": "pytest", "command": "pytest -q",
                               "workingDirectory": ".", "timeoutSeconds": 900,
                               "dependsOn": [], "success": "exit_code == 0"}]}
        raise AssertionError(f"unexpected engine path {path}")

    monkeypatch.setattr(wf, "_workflow_engine_json", _engine)


def _dirty_and_snapshot(repo: Path) -> dict:
    """Make the tree dirty exactly as a real failing run would have, then
    record the snapshot the run carries."""
    (repo / "a.txt").write_text(DIRTY_TRACKED, encoding="utf-8")
    (repo / "fixture.txt").write_text(DIRTY_UNTRACKED, encoding="utf-8")
    return dict(capture_source_snapshot(str(repo)))


def _request():
    return wf.WorkflowFixRequest(instance_id=INSTANCE, step_id="test")


def test_the_repair_workspace_holds_the_snapshots_bytes_not_advanced_head(
        repo, svc, monkeypatch):
    """THE EPIC'S ORACLE. A dirty tree is validated and snapshotted; real
    work then lands on the repo, moving HEAD away from what the run saw. The
    repair workspace must still carry the snapshot's fingerprint and its
    exact file contents."""
    snapshot = _dirty_and_snapshot(repo)
    _wire_engine(monkeypatch, snapshot)

    # Unrelated work lands AFTER the failed run -- the shape of the incident.
    (repo / "a.txt").write_text("later unrelated change\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "work landed after the failed run")
    advanced_head = _git(repo, "rev-parse", "HEAD")
    assert advanced_head != snapshot["snapshotCommit"]

    out = wf.queue_workflow_fix("prism", "validation", _request())

    # IDENTICAL FINGERPRINT: the workspace is based on the recorded commit.
    assert out["workspace"]["baseline"] == snapshot["snapshotCommit"], (
        "the repair worktree must be based on the RECORDED snapshot, not on "
        "HEAD as it stands at repair time")
    assert out["source_snapshot"]["tree"] == snapshot["tree"]

    # IDENTICAL CONTENTS: both the dirty tracked edit and the untracked file
    # the run actually used are present, and the later commit is not.
    ws = Path(out["workspace"]["path"])
    assert (ws / "a.txt").read_text(encoding="utf-8") == DIRTY_TRACKED, (
        "the repair workspace must reproduce the exact bytes the failed run "
        "validated, not whatever landed on HEAD afterwards")
    assert (ws / "fixture.txt").read_text(encoding="utf-8") == DIRTY_UNTRACKED


def test_the_caller_supplies_only_ids_and_prism_rereads_its_own_evidence(
        repo, svc, monkeypatch):
    """The repair boundary takes IDs, never a caller-carried snapshot or
    path: PRISM re-reads the authoritative run rather than trusting the
    browser or an agent."""
    _wire_engine(monkeypatch, _dirty_and_snapshot(repo))

    fields = set(wf.WorkflowFixRequest.model_fields)
    assert fields == {"instance_id", "step_id"}, (
        f"the repair request must carry only identifiers; got {fields}")

    out = wf.queue_workflow_fix("prism", "validation", _request())
    assert out["queued"] is True
    assert Path(out["workspace"]["path"]).exists()


def test_an_unreconstructable_snapshot_materializes_no_workspace(
        repo, svc, monkeypatch):
    """If the recorded commit cannot be resolved, repair fails EXPLICITLY --
    it never silently falls back to ambient HEAD, which is the whole reason
    the snapshot exists."""
    snapshot = _dirty_and_snapshot(repo)
    snapshot["snapshotCommit"] = "0" * 40      # valid shape, present nowhere
    _wire_engine(monkeypatch, snapshot)
    ambient_head = _git(repo, "rev-parse", "HEAD")

    with pytest.raises(HTTPException) as exc:
        wf.queue_workflow_fix("prism", "validation", _request())

    assert exc.value.status_code == 409
    assert "snapshot is unavailable" in str(exc.value.detail)
    assert svc.created == [], "no repair task may be queued against ambient HEAD"
    assert _git(repo, "rev-parse", "HEAD") == ambient_head
