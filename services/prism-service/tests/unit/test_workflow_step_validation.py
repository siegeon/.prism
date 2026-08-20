import pytest
from fastapi import HTTPException


def test_start_sends_immutable_source_snapshot(monkeypatch):
    from prism_service.api import workflows
    from prism_service.services import source_snapshot

    snapshot = {
        "schemaVersion": 1, "repositoryRoot": "/repo",
        "baseCommit": "a" * 40, "snapshotCommit": "b" * 40,
        "tree": "c" * 40, "dirty": True, "includedUntracked": 2,
    }
    monkeypatch.setattr(source_snapshot, "capture_source_snapshot", lambda root: snapshot)
    seen = {}
    monkeypatch.setattr(
        workflows, "_workflow_engine_json",
        lambda path, method="GET", body=None: seen.update(
            path=path, method=method, body=body,
        ) or {"instanceId": "run-1"},
    )

    result = workflows.start_workflow_run("validation", project="prism")
    assert result == {"instanceId": "run-1"}
    assert seen["body"] == {"sourceSnapshot": snapshot}


def test_fix_rejects_run_without_snapshot_provenance(monkeypatch):
    from prism_service.api import workflows

    monkeypatch.setattr(
        workflows, "_workflow_engine_json",
        lambda path, method="GET", body=None: {
            "data": {
                "project": "prism",
                "tests": {"status": "failed", "exitCode": 1, "output": "failed"},
            },
        },
    )

    with pytest.raises(HTTPException, match="reconstructable source snapshot") as caught:
        workflows.queue_workflow_fix(
            "prism", "validation",
            workflows.WorkflowFixRequest(instance_id="legacy", step_id="test"),
        )
    assert caught.value.status_code == 409
