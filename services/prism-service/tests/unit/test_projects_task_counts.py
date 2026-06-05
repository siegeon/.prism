"""GET /api/projects task_counts — cold-start non-empty project resolver.

Pins the backend half of the cold-start fix (task bf391534, follow-up to
#137 item 3): GET /api/projects must keep the flat `projects` list intact
(backward-compatible) AND add a `task_counts` {project: int} map so the SPA
resolver can pick the busiest non-default project on first load instead of
landing on the empty 'default' blank state.

These are RED until api/projects.list_projects() returns task_counts.

Isolation: tmp_path + monkeypatched PROJECTS_DIR so nothing escapes into
the live /data volume. tasks.db is seeded directly (the same per-project
SQLite file dashboard.py COUNT(*)s over) so the assertion exercises the
real on-disk seam, not a service-class shim.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from prism_service import config
from prism_service.api import projects as projects_api
from prism_service import project_context


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    monkeypatch.setattr(config, "PROJECTS_DIR", root)
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", root)
    monkeypatch.setattr(projects_api, "DEFAULT_PROJECT", "default")
    project_context._contexts.clear()
    return root


def _seed_project(name: str, task_count: int) -> Path:
    """Create a project data dir with a tasks.db holding `task_count` rows.

    Writes the minimal `tasks` table the COUNT(*) query reads — mirrors
    the per-project tasks.db that task_service / dashboard.py operate on.
    """
    pdir = config.project_data_dir(name)  # seeds the data dir
    db = pdir / "tasks.db"
    c = sqlite3.connect(str(db))
    c.execute(
        "CREATE TABLE IF NOT EXISTS tasks ("
        "id TEXT PRIMARY KEY, title TEXT NOT NULL, "
        "status TEXT DEFAULT 'pending', created_at TEXT NOT NULL)"
    )
    for i in range(task_count):
        c.execute(
            "INSERT INTO tasks (id, title, created_at) VALUES (?, ?, ?)",
            (f"{name}-{i}", f"task {i}", "2026-06-05T00:00:00Z"),
        )
    c.commit()
    c.close()
    return pdir


def test_projects_list_unchanged_backward_compatible(isolated_projects_root):
    """The flat `projects` list stays present and contains every project."""
    _seed_project("default", 0)
    _seed_project("prism", 5)
    _seed_project("graphify", 2)

    out = projects_api.list_projects()

    assert "projects" in out
    assert isinstance(out["projects"], list)
    assert set(out["projects"]) == {"default", "prism", "graphify"}


def test_projects_returns_task_counts_map(isolated_projects_root):
    """New additive `task_counts` map reports correct per-project row counts."""
    _seed_project("default", 0)
    _seed_project("prism", 5)
    _seed_project("graphify", 2)

    out = projects_api.list_projects()

    assert "task_counts" in out, "missing task_counts map — cold-start resolver is blind"
    counts = out["task_counts"]
    assert counts["default"] == 0
    assert counts["prism"] == 5
    assert counts["graphify"] == 2


def test_empty_project_reports_zero(isolated_projects_root):
    """A project with a tasks.db but no rows reports 0 (not missing)."""
    _seed_project("default", 0)
    _seed_project("empty-proj", 0)

    out = projects_api.list_projects()

    assert out["task_counts"]["empty-proj"] == 0


def test_task_counts_covers_every_listed_project(isolated_projects_root):
    """Every project in the flat list has an entry in task_counts.

    The cold-start resolver iterates task_counts to find the busiest
    non-default project; a missing key would crash or silently skip a
    candidate, regressing to the empty 'default' blank state.
    """
    _seed_project("default", 0)
    _seed_project("prism", 7)
    _seed_project("graphify", 0)

    out = projects_api.list_projects()

    for name in out["projects"]:
        assert name in out["task_counts"], f"{name} absent from task_counts"


def test_busiest_non_default_is_resolvable(isolated_projects_root):
    """Caller-facing acceptance: the resolver's pick (max non-default
    task_count) is computable from the response alone — proves the field
    carries the signal the SPA cold-start needs."""
    _seed_project("default", 99)  # default is excluded even if busy
    _seed_project("prism", 5)
    _seed_project("graphify", 2)

    out = projects_api.list_projects()
    counts = out["task_counts"]
    candidates = {n: c for n, c in counts.items() if n != "default" and c > 0}
    busiest = max(candidates, key=candidates.get)

    assert busiest == "prism"
