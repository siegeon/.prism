"""Project context — manages per-project service instances.

Each project gets its own isolated set of services (brain, tasks, memory,
workflow, conductor, governance) backed by its own data directory.

Service instances are lazily created and cached.
"""

from __future__ import annotations

import threading
from typing import Optional

from prism_service.config import project_data_dir, list_projects, PROJECTS_DIR, DEFAULT_PROJECT


class ProjectContext:
    """Holds service instances for a single project."""

    def __init__(self, project_id: str) -> None:
        self.project_id = project_id
        self._data_dir = project_data_dir(project_id)

        # Lazy service instances
        self._brain_svc = None
        self._graph_svc = None
        self._task_svc = None
        self._workflow_svc = None
        self._memory_svc = None
        self._conductor_svc = None
        self._governance = None
        self._janitor_svc = None
        self._verifier_svc = None
        self._graph_annotate_svc = None

    @property
    def brain_svc(self):
        if self._brain_svc is None:
            from prism_service.services.brain_service import BrainService
            self._brain_svc = BrainService(
                brain_db=str(self._data_dir / "brain.db"),
                graph_db=str(self._data_dir / "graph.db"),
                scores_db=str(self._data_dir / "scores.db"),
                tasks_db=str(self._data_dir / "tasks.db"),
            )
            # Wire graph service for source-file staging
            self._brain_svc.graph_svc = self.graph_svc
        return self._brain_svc

    @property
    def graph_svc(self):
        if self._graph_svc is None:
            from prism_service.services.graph_service import GraphService
            self._graph_svc = GraphService(
                project_data_dir=str(self._data_dir),
                graph_db_path=str(self._data_dir / "graph.db"),
            )
        return self._graph_svc

    @property
    def task_svc(self):
        if self._task_svc is None:
            from prism_service.services.task_service import TaskService
            self._task_svc = TaskService(
                db_path=str(self._data_dir / "tasks.db"),
                scores_db=str(self._data_dir / "scores.db"),
            )
        return self._task_svc

    @property
    def workflow_svc(self):
        if self._workflow_svc is None:
            from prism_service.services.workflow_service import WorkflowService
            self._workflow_svc = WorkflowService(
                workflow_dir=str(self._data_dir / "workflow"),
            )
        return self._workflow_svc

    @property
    def memory_svc(self):
        if self._memory_svc is None:
            from prism_service.services.memory_service import MemoryService
            self._memory_svc = MemoryService(
                mulch_dir=str(self._data_dir / "mulch"),
                task_svc=self.task_svc,
            )
        return self._memory_svc

    @property
    def conductor_svc(self):
        if self._conductor_svc is None:
            from prism_service.services.conductor_service import ConductorService
            # Wire verifier_svc up-front so Conductor v2 gates (issue
            # #79 [3/4]) can consult it. attach_verifier_service() is
            # also exposed for late binding when callers (tests, MCP)
            # build a ConductorService bare.
            # project_root = the host agent checkout (the working tree the
            # driver edits). _verify_gate hands it to verifier.run() so Tier0
            # scopes the REAL diff, not the daemon cwd (task 3826dac3).
            # CLAUDE_PROJECT_DIR is the hook-provided host root; fall back to
            # the daemon cwd so the seam is never None in production.
            import os
            from pathlib import Path

            def _checkout_root() -> str:
                # Editable/dev: this package lives INSIDE the repo working
                # tree, so derive the checkout root from __file__ (parents[3]
                # == repo root). This keeps the gate verifier wired to the REAL
                # checkout regardless of the detached daemon's cwd or a bounce
                # that dropped PRISM_PROJECT_ROOT — the env-loss that made the
                # verifier go blind and forced gate overrides. Falls back to
                # cwd when not in a source tree (installed wheel).
                try:
                    root = Path(__file__).resolve().parents[3]
                    if (root / ".git").exists():
                        return str(root)
                except (IndexError, OSError):
                    pass
                return os.getcwd()

            project_root = (os.environ.get("PRISM_PROJECT_ROOT")
                            or os.environ.get("CLAUDE_PROJECT_DIR")
                            or _checkout_root())
            self._conductor_svc = ConductorService(
                scores_db=str(self._data_dir / "scores.db"),
                task_svc=self.task_svc,
                verifier_svc=self.verifier_svc,
                project_root=project_root,
            )
        return self._conductor_svc

    @property
    def janitor_svc(self):
        if self._janitor_svc is None:
            from prism_service.services.janitor_service import JanitorService
            self._janitor_svc = JanitorService(
                scores_db=str(self._data_dir / "scores.db"),
            )
        return self._janitor_svc

    @property
    def graph_annotate_svc(self):
        if self._graph_annotate_svc is None:
            from prism_service.services.graph_annotate import GraphAnnotateService
            self._graph_annotate_svc = GraphAnnotateService(self.graph_svc)
        return self._graph_annotate_svc

    @property
    def verifier_svc(self):
        if self._verifier_svc is None:
            from prism_service.services.verifier_service import VerifierService
            # Workspace is the host project root, not the container's
            # data dir — Tier 0 needs to run linters against the actual
            # source tree. Hook script will pass workspace via the MCP
            # call when the SessionStart hook knows ${CLAUDE_PROJECT_DIR}.
            self._verifier_svc = VerifierService(
                scores_db=str(self._data_dir / "scores.db"),
                brain_db=str(self._data_dir / "brain.db"),
                tasks_db=str(self._data_dir / "tasks.db"),
                memory_dir=str(self._data_dir / "mulch"),
                workspace=None,   # set per-call by run() arg
            )
        return self._verifier_svc

    @property
    def governance(self):
        if self._governance is None:
            from prism_service.services.governance import GovernanceEngine
            self._governance = GovernanceEngine(
                self.memory_svc, self.task_svc, self.brain_svc,
            )
        return self._governance


# ---------------------------------------------------------------------------
# Global registry of project contexts (thread-safe)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_contexts: dict[str, ProjectContext] = {}


def get_project(project_id: Optional[str] = None) -> ProjectContext:
    """Get or create a ProjectContext for the given project ID."""
    pid = project_id or DEFAULT_PROJECT
    with _lock:
        if pid not in _contexts:
            _contexts[pid] = ProjectContext(pid)
        return _contexts[pid]


def get_all_projects() -> list[str]:
    """List all project IDs that have data on disk."""
    return list_projects()


def create_project(project_id: str) -> ProjectContext:
    """Create a new project (creates its data directory)."""
    project_data_dir(project_id)  # ensure dirs exist
    return get_project(project_id)


def release_project(project_id: str) -> None:
    """Drop the cached ProjectContext for `project_id`, closing any SQLite
    connections owned by its services. Idempotent — safe to call for an
    unknown id. Called by the DELETE /api/projects/{name} endpoint before
    the on-disk dir is removed so future creates of the same name get a
    fresh context bound to a freshly-seeded data directory.
    """
    with _lock:
        ctx = _contexts.pop(project_id, None)
    if ctx is None:
        return
    # Close any services that hold open SQLite handles. Each service
    # exposes a best-effort .close() if it has connections; missing ones
    # are silently ignored so we can free files on Windows.
    for attr in ("_brain_svc", "_graph_svc", "_task_svc",
                 "_memory_svc", "_workflow_svc"):
        svc = getattr(ctx, attr, None)
        if svc is None:
            continue
        for close_name in ("close", "shutdown"):
            close = getattr(svc, close_name, None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
                break
