"""DELETE /api/projects/{name} — wipes the project's data dir + cached services.

Uses tmp_path + monkeypatched PROJECTS_DIR so nothing escapes the test
into the live /data volume.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prism_service import config
from prism_service.api import projects as projects_api
from prism_service import project_context
from prism_service.services import source_service as ss


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    from prism_service.services import trash as trash_svc
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    # The endpoint reads config.DEFAULT_PROJECT (not module-imported) so
    # patch happens through config; also patch projects_api's local imports.
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(projects_api, "DEFAULT_PROJECT", "default")
    # trash.sweep_once reads its own module-level PROJECTS_DIR (imported at
    # load time), so the test sweep needs that pointer redirected too.
    monkeypatch.setattr(trash_svc, "PROJECTS_DIR", tmp_path / "projects")
    ss._LOCKS.clear()
    project_context._contexts.clear()
    return tmp_path / "projects"


def _seed_project(name: str) -> Path:
    pdir = config.project_data_dir(name)
    (pdir / "marker.txt").write_text("hello", encoding="utf-8")
    return pdir


def test_delete_removes_dir(isolated_projects_root):
    pdir = _seed_project("trash-me")
    assert pdir.is_dir()

    out = projects_api.delete_project("trash-me")

    assert out["deleted"] is True
    assert out["name"] == "trash-me"
    assert out["freed_bytes"] >= len("hello")
    # v5.3 sweep model: delete drops a `.deleted` marker; the trash
    # sweeper rmtrees the dir on its next pass (sweep_pending=True).
    assert out["sweep_pending"] is True
    from prism_service.services import trash as trash_svc
    assert trash_svc.is_deleted(pdir)


def test_delete_refuses_default(isolated_projects_root):
    from fastapi import HTTPException
    _seed_project("default")
    with pytest.raises(HTTPException) as ei:
        projects_api.delete_project("default")
    assert ei.value.status_code == 409


def test_delete_404_for_unknown(isolated_projects_root):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        projects_api.delete_project("never-created")
    assert ei.value.status_code == 404


def test_delete_rejects_invalid_names(isolated_projects_root):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        projects_api.delete_project("bad name with spaces")
    assert ei.value.status_code == 400


def test_delete_releases_project_context(isolated_projects_root):
    """After deletion the cached ProjectContext must be gone — otherwise
    a future create with the same name would reuse a context bound to a
    stale (now-deleted) data directory.
    """
    _seed_project("cached-proj")
    # Force a context cache entry.
    project_context.get_project("cached-proj")
    # _contexts is keyed by (resolved_data_dir, project_id) (33a1397b), so
    # membership is checked on the project-id half of the composite key.
    assert any(k[1] == "cached-proj" for k in project_context._contexts)

    projects_api.delete_project("cached-proj")

    assert not any(k[1] == "cached-proj" for k in project_context._contexts)


def test_delete_then_recreate_is_clean_slate(isolated_projects_root):
    _seed_project("roundtrip")
    projects_api.delete_project("roundtrip")
    # v5.3 sweep model: the trash sweeper rmtrees the marked dir on its
    # next pass. Drive it synchronously so recreate sees a clean slot.
    from prism_service.services import trash as trash_svc
    trash_svc.sweep_once()
    # Recreate with the same name — should land in a freshly seeded dir
    # with no leftover marker file.
    out = projects_api.create_project(
        projects_api.CreateBody(name="roundtrip"),
    )
    assert out["created"] is True
    new_dir = Path(out["path"])
    assert new_dir.is_dir()
    assert not (new_dir / "marker.txt").exists()
