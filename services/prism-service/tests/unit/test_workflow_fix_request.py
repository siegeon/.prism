"""The repair boundary returns a TYPED step validation, not a transcript.

Task 3f926ef1 ("Structure conductor step validation"), child of epic
4e6e7417. Its oracle: calling the fix-request path for a failed validation
run returns and queues a concise, Pydantic-validated step result carrying
structured failures and evidence provenance, and the user-visible command
result no longer embeds the whole raw process output.

These pin the contract as SHIPPED on main -- api.workflows.queue_workflow_fix
building a ConductorStepValidation from the authoritative run -- rather than
the earlier draft API on the task's own branch (a record_step_validation /
SourceSnapshot pair that never landed). PRISM re-reads its own recorded
evidence: the caller supplies IDs only, and the raw output stays behind an
evidence URI with only its LENGTH carried forward.

Also covers the half of epic 4e6e7417's oracle that lives at this boundary:
a run whose recorded snapshot cannot be reconstructed is refused EXPLICITLY,
never silently repaired against ambient HEAD.
"""
from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

from prism_service.api import workflows as wf
from prism_service.services.source_snapshot import capture_source_snapshot

INSTANCE = "run-abc123"
# Long enough that embedding it whole would be exactly the defect the task
# names; the failure lines are real pytest-shaped output.
RAW_OUTPUT = (
    "collected 3 items\n"
    + ("noise line that nobody should have to read\n" * 200)
    + "FAILED tests/unit/test_thing.py::test_one - AssertionError: boom\n"
    + "FAILED tests/unit/test_thing.py::test_two - AssertionError: bang\n"
    + "2 failed, 1 passed in 1.23s\n"
)


def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args],
                       capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)} -> {r.stderr}")
    return r.stdout.strip()


class _TaskSvc:
    def __init__(self):
        self.created = []
        self.updated = []

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
    # The failed run validated a DIRTY tree -- the case the snapshot exists for.
    (root / "a.txt").write_text("what the failed run actually saw\n",
                                encoding="utf-8")
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


def _wire_engine(monkeypatch, snapshot: dict, status="failed", output=RAW_OUTPUT):
    """The authoritative run + definition, as the engine would serve them."""
    def _engine(path, **_kw):
        if path.startswith("/workflows/instances/"):
            return {"data": {"project": "prism", "sourceSnapshot": snapshot,
                             "tests": {"status": status, "output": output,
                                       "exitCode": 1}}}
        if path.startswith("/workflows/definitions/"):
            # The full ProjectWorkflow/ScriptedStep shape -- validated by the
            # real models, so this fixture cannot drift from the contract.
            return {"id": "validation", "name": "Build and test",
                    "description": "Build and test the project",
                    "project": "prism", "projectType": "python",
                    "steps": [{"id": "test", "title": "Run tests",
                               "purpose": "Run the project's test suite",
                               "runner": "pytest", "command": "pytest -q",
                               "workingDirectory": "services/prism-service",
                               "timeoutSeconds": 900, "dependsOn": [],
                               "success": "exit_code == 0"}]}
        raise AssertionError(f"unexpected engine path {path}")

    monkeypatch.setattr(wf, "_workflow_engine_json", _engine)


def _snapshot_payload(repo: Path) -> dict:
    """A REAL snapshot of the dirty tree. capture_source_snapshot already
    emits the engine's wire shape (schemaVersion/repositoryRoot/baseCommit/
    snapshotCommit/tree/dirty), so this is the genuine recorded identity,
    not a hand-built lookalike that could drift from it."""
    return dict(capture_source_snapshot(str(repo)))


def _request():
    return wf.WorkflowFixRequest(instance_id=INSTANCE, step_id="test")


# --- AC-1: a typed result, not a transcript -------------------------------

def test_the_result_is_a_validated_step_object_with_structured_failures(
        repo, svc, monkeypatch):
    _wire_engine(monkeypatch, _snapshot_payload(repo))

    out = wf.queue_workflow_fix("prism", "validation", _request())

    v = out["validation"]
    assert v["kind"] == "conductor.step_validation"
    assert v["outcome"] == "failed"
    assert v["step_id"] == "test"
    # STRUCTURED, not scraped prose: each failure is its own object.
    checks = [f["check"] for f in v["failures"]]
    assert "tests/unit/test_thing.py::test_one" in checks, v["failures"]
    assert "tests/unit/test_thing.py::test_two" in checks, v["failures"]
    # It really is the Pydantic model, so the shape cannot drift silently.
    wf.ConductorStepValidation.model_validate(
        {**v, "source_snapshot": out["source_snapshot"]})


# --- AC-2: the raw transcript is referenced, never embedded ---------------

def test_the_queued_task_does_not_embed_the_raw_transcript(
        repo, svc, monkeypatch):
    _wire_engine(monkeypatch, _snapshot_payload(repo))

    out = wf.queue_workflow_fix("prism", "validation", _request())

    description = svc.created[0]["description"]
    assert "noise line that nobody should have to read" not in description, (
        "the whole raw process output must stay behind the evidence URI -- "
        "embedding it is the defect this task exists to close")
    assert len(description) < len(RAW_OUTPUT), (
        f"description {len(description)} chars vs raw {len(RAW_OUTPUT)}")
    # The LENGTH is carried so a reader knows how much is behind the link.
    assert out["validation"]["raw_output_chars"] == len(RAW_OUTPUT)


# --- AC-3: evidence provenance -------------------------------------------

def test_the_result_names_where_its_evidence_lives(repo, svc, monkeypatch):
    snap = _snapshot_payload(repo)
    _wire_engine(monkeypatch, snap)

    out = wf.queue_workflow_fix("prism", "validation", _request())

    assert out["validation"]["evidence_uri"] == f"/workflows/instances/{INSTANCE}"
    # The exact source identity the failed run executed against travels with
    # the result, so the repair is reconstructable rather than approximate.
    assert out["source_snapshot"]["snapshotCommit"] == snap["snapshotCommit"]
    assert out["source_snapshot"]["tree"] == snap["tree"]
    assert out["workspace"]["baseline"] == snap["snapshotCommit"], (
        "the repair workspace must be built at the RECORDED snapshot commit")


# --- AC-4 (epic 4e6e7417): an unreconstructable snapshot is refused -------

def test_an_unreconstructable_snapshot_is_refused_not_silently_repaired(
        repo, svc, monkeypatch):
    snap = _snapshot_payload(repo)
    snap["snapshotCommit"] = "0" * 40      # valid shape, present nowhere
    _wire_engine(monkeypatch, snap)

    with pytest.raises(HTTPException) as exc:
        wf.queue_workflow_fix("prism", "validation", _request())

    assert exc.value.status_code == 409
    assert "snapshot is unavailable" in str(exc.value.detail)
    assert svc.created == [], (
        "no repair task may be queued against ambient HEAD as a silent "
        "fallback when the recorded source cannot be reconstructed")


def test_a_snapshot_from_another_repository_is_refused(
        repo, svc, monkeypatch, tmp_path):
    snap = _snapshot_payload(repo)
    snap["repositoryRoot"] = str(tmp_path / "somewhere-else")
    _wire_engine(monkeypatch, snap)

    with pytest.raises(HTTPException) as exc:
        wf.queue_workflow_fix("prism", "validation", _request())

    assert exc.value.status_code == 409
    assert "another repository" in str(exc.value.detail)


def test_a_run_with_no_snapshot_at_all_is_refused(repo, svc, monkeypatch):
    _wire_engine(monkeypatch, None)

    with pytest.raises(HTTPException) as exc:
        wf.queue_workflow_fix("prism", "validation", _request())

    assert exc.value.status_code == 409
    assert "no reconstructable source snapshot" in str(exc.value.detail)


# --- the negative control: a step that did not fail -----------------------

def test_a_passing_step_cannot_have_a_fix_requested(repo, svc, monkeypatch):
    _wire_engine(monkeypatch, _snapshot_payload(repo), status="passed")

    with pytest.raises(HTTPException) as exc:
        wf.queue_workflow_fix("prism", "validation", _request())

    assert exc.value.status_code == 409
    assert "only be requested for a failed step" in str(exc.value.detail)
    assert svc.created == []
