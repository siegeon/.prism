"""A task's workspace must RESOLVE, or no receipt can be judged fresh.

Task 4cbac65a. Measured live on 2026-08-28 across ten tasks parked at
green_gate: every one reported an empty current tree while its workspace
directory existed on disk under PRISM_DATA_DIR/task_workspaces/<task_id>.

Cause: `green_rewind._workspace_path` falls back to
`task_workspace.workspace_path`, which did not exist. `getattr` returned
None, the helper returned "", and `oracle_spec.current_tree_sha("")`
returns "". Two consequences, both silent:

1. A PASSING receipt can never be confirmed fresh, so the gate refuses it
   as unshipped and the task stalls (8 of the 10 held a passing receipt).
2. `green_rewind.maybe_rewind` bails at its `if not tree` guard, so the
   red-receipt rewind shipped as task ad92c0e9 could never fire in
   production despite being green, merged and deployed.

The resolver must RESOLVE ONLY -- never create -- because it is called on
a gate check, and materialising a checkout there would give a workspace to
tasks that never had one.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


class _Task:
    """A task row whose `workspace` field is empty -- the shape every one of
    the ten stalled tasks actually had."""

    def __init__(self, task_id: str) -> None:
        self.id = task_id
        self.workspace = None


def test_workspace_path_exists_as_a_public_resolver(tmp_path, monkeypatch):
    import prism_service.data_dir as data_dir_module
    from prism_service.services import task_workspace

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(task_workspace, "resolve_data_dir", lambda: tmp_path,
                        raising=False)
    fn = getattr(task_workspace, "workspace_path", None)
    assert callable(fn), (
        "task_workspace.workspace_path does not exist, so "
        "green_rewind._workspace_path's fallback silently returns '' for "
        "every task whose workspace field is empty")


def test_it_resolves_a_directory_that_exists(tmp_path, monkeypatch):
    import prism_service.data_dir as data_dir_module
    from prism_service.services import task_workspace

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(task_workspace, "resolve_data_dir", lambda: tmp_path,
                        raising=False)
    tid = "4cbac65a-0000-0000-0000-000000000000"
    (tmp_path / "task_workspaces" / tid).mkdir(parents=True)
    got = task_workspace.workspace_path(tid)
    assert str(got) == str(tmp_path / "task_workspaces" / tid)


def test_it_returns_empty_when_there_is_no_workspace(tmp_path, monkeypatch):
    """A task that never had a checkout must stay unresolvable -- the gate
    reads that as 'no tree', which is the honest answer."""
    import prism_service.data_dir as data_dir_module
    from prism_service.services import task_workspace

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(task_workspace, "resolve_data_dir", lambda: tmp_path,
                        raising=False)
    assert task_workspace.workspace_path("no-such-task-id") == ""


def test_it_resolves_only_and_never_creates(tmp_path, monkeypatch):
    import prism_service.data_dir as data_dir_module
    from prism_service.services import task_workspace

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(task_workspace, "resolve_data_dir", lambda: tmp_path,
                        raising=False)
    tid = "never-created"
    task_workspace.workspace_path(tid)
    assert not (tmp_path / "task_workspaces" / tid).exists(), (
        "the resolver created a checkout; it is called on a gate check and "
        "must never materialise a workspace")


def test_green_rewind_resolves_the_same_directory(tmp_path, monkeypatch):
    """The seam that actually failed in production: a task row with an
    empty workspace field must still resolve to its real checkout, and the
    tree sha must come back non-empty for a real git repo."""
    import prism_service.data_dir as data_dir_module
    from prism_service.services import task_workspace, green_rewind, oracle_spec

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)
    monkeypatch.setattr(task_workspace, "resolve_data_dir", lambda: tmp_path,
                        raising=False)
    tid = "4cbac65a-1111-1111-1111-111111111111"
    ws = tmp_path / "task_workspaces" / tid
    ws.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-q", "--allow-empty", "-m", "seed"],
                   cwd=ws, check=True)

    resolved = green_rewind._workspace_path(_Task(tid))
    assert str(resolved) == str(ws), (
        "green_rewind could not resolve a workspace that exists on disk")
    assert oracle_spec.current_tree_sha(resolved), (
        "current_tree_sha came back empty, so no receipt can ever be judged "
        "fresh and maybe_rewind bails at its 'if not tree' guard")
