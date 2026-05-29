"""Issue #66 — the SPA-missing 503 must tell the user a remediation they
can actually run. A pipx user has no prism_service/web/ source tree, so
the old hardcoded `npm run build` message was unreachable for them.
"""

from __future__ import annotations

from prism_service import main


def test_pipx_install_mode_suggests_prism_update(monkeypatch):
    monkeypatch.setattr(main, "_install_mode", lambda: "pipx")
    payload = main._spa_missing_payload()
    assert payload["install_mode"] == "pipx"
    assert "prism update" in payload["remediation"]
    assert "npm" not in payload["remediation"]
    assert payload["detail"].startswith("SPA build missing")


def test_source_install_mode_suggests_npm(monkeypatch):
    monkeypatch.setattr(main, "_install_mode", lambda: "source")
    payload = main._spa_missing_payload()
    assert payload["install_mode"] == "source"
    assert "npm run build" in payload["remediation"]


def test_docker_install_mode_suggests_rebuild(monkeypatch):
    monkeypatch.setattr(main, "_install_mode", lambda: "docker")
    payload = main._spa_missing_payload()
    assert payload["install_mode"] == "docker"
    assert "rebuild" in payload["remediation"].lower()


def test_install_mode_classifies_source_when_web_dir_present(monkeypatch, tmp_path):
    # This repo checkout has prism_service/web/, so the running source
    # tree must classify as 'source'.
    mode = main._install_mode()
    assert mode in ("source", "pipx", "docker")
    # In the editable/source checkout used for tests the sibling web/
    # dir exists, so we expect 'source' (unless we're somehow in docker).
    if (main.Path(main.__file__).parent / "web").exists():
        assert mode in ("source", "docker")
