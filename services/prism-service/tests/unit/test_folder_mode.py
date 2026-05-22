"""Folder-mode projects — v5.2.0.

Validates that a project can be configured via `source_path` (bind-mounted
folder) instead of `remote_url` (server-side clone). Isolated via
tmp_path + monkeypatched PROJECTS_DIR so nothing writes to /data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app import config
from app.api import projects as projects_api
from app.api import understand as understand_api
from app.engines import understand_engine as ue
from app.services import source_service as ss


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(projects_api, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(projects_api, "DEFAULT_PROJECT", "default")
    ss._LOCKS.clear()
    return tmp_path / "projects"


@pytest.fixture
def code_folder(tmp_path: Path) -> Path:
    """A bind-mount-style folder with a tiny git repo inside."""
    code = tmp_path / "code" / "demo"
    code.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "--initial-branch=main"],
                   cwd=str(code), check=True)
    subprocess.run(["git", "config", "user.email", "x@x"], cwd=str(code), check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=str(code), check=True)
    (code / "main.py").write_text("print('hi')\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(code), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"],
                   cwd=str(code), check=True)
    return code


def test_set_source_path_records_state(isolated_projects_root, code_folder):
    config.project_data_dir("folder-proj")  # seed state.json
    out = ss.set_source_path("folder-proj", str(code_folder))

    assert out["mode"] == "folder"
    assert Path(out["source_path"]).resolve() == code_folder.resolve()
    assert out["head_sha"]  # git repo, so head_sha is populated

    state = ue._read_state("folder-proj")
    assert state["source_path"] == str(code_folder.resolve())
    assert state["mode"] == "folder"


def test_set_source_path_rejects_missing_dir(isolated_projects_root, tmp_path):
    config.project_data_dir("missing-proj")
    with pytest.raises(ss.SourceUnavailable):
        ss.set_source_path("missing-proj", str(tmp_path / "does-not-exist"))


def test_source_dir_for_returns_bind_mount(isolated_projects_root, code_folder):
    config.project_data_dir("p")
    ss.set_source_path("p", str(code_folder))
    assert ss.source_dir_for("p").resolve() == code_folder.resolve()


def test_source_dir_for_falls_back_to_clone_path_when_no_source_path(
    isolated_projects_root,
):
    config.project_data_dir("clone-proj")
    expected = isolated_projects_root / "clone-proj" / "source"
    assert ss.source_dir_for("clone-proj").resolve() == expected.resolve()


def test_is_cloned_true_for_folder_mode(isolated_projects_root, code_folder):
    config.project_data_dir("p")
    ss.set_source_path("p", str(code_folder))
    assert ss.is_cloned("p") is True


def test_current_sha_returns_git_head_in_folder_mode(
    isolated_projects_root, code_folder,
):
    config.project_data_dir("p")
    ss.set_source_path("p", str(code_folder))
    sha = ss.current_sha("p")
    # git rev-parse HEAD output is 40 hex chars
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_current_sha_synthesizes_fingerprint_for_non_git_folder(
    isolated_projects_root, tmp_path,
):
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()
    (plain_dir / "a.py").write_text("# a", encoding="utf-8")
    (plain_dir / "b.py").write_text("# b", encoding="utf-8")

    config.project_data_dir("p")
    ss.set_source_path("p", str(plain_dir))
    sha = ss.current_sha("p")
    assert len(sha) == 64  # sha256 hex
    # Same fingerprint when called twice with no changes.
    assert ss.current_sha("p") == sha


def test_post_projects_with_source_path_skips_clone(
    isolated_projects_root, code_folder, monkeypatch,
):
    """The /api/projects POST with source_path must NOT call ensure_cloned."""
    called = []
    monkeypatch.setattr(
        ss, "ensure_cloned",
        lambda *a, **kw: called.append(("ensure_cloned", a, kw)) or None,
    )
    monkeypatch.setattr(
        ss, "bootstrap_after_clone",
        lambda *a, **kw: None,
    )

    body = projects_api.CreateBody(name="folder-via-api", source_path=str(code_folder))
    out = projects_api.create_project(body)

    assert out["mode"] == "folder"
    assert out["source_path"] == str(code_folder)
    assert out["bootstrap"] == "started"
    assert called == []  # never tried to clone


def test_post_projects_rejects_both_source_path_and_remote_url(
    isolated_projects_root, code_folder,
):
    from fastapi import HTTPException
    body = projects_api.CreateBody(
        name="conflicted",
        source_path=str(code_folder),
        remote_url="https://github.com/x/y",
    )
    with pytest.raises(HTTPException) as ei:
        projects_api.create_project(body)
    assert ei.value.status_code == 400
    assert "either" in str(ei.value.detail).lower()


def test_understand_configure_folder_mode(
    isolated_projects_root, code_folder, monkeypatch,
):
    """POST /api/understand/configure with source_path takes the folder path."""
    monkeypatch.setattr(ss, "bootstrap_after_clone", lambda *a, **kw: None)
    config.project_data_dir("cfg-proj")

    body = understand_api.ConfigureBody(source_path=str(code_folder))
    out = understand_api.configure(body, project="cfg-proj")

    assert out["configured"] is True
    assert out["mode"] == "folder"
    assert Path(out["source_path"]).resolve() == code_folder.resolve()


def test_status_surfaces_mode_and_source_path(
    isolated_projects_root, code_folder,
):
    config.project_data_dir("status-proj")
    ss.set_source_path("status-proj", str(code_folder))

    status = ue.UnderstandEngine("status-proj").status()
    assert status["mode"] == "folder"
    assert Path(status["source_path"]).resolve() == code_folder.resolve()
    assert status["remote_url"] is None
    assert status["current_sha"]
