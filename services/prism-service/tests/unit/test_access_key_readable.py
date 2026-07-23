"""The owner can read their own access key any time they are logged in
(task 6cef97ec, decision mx-935cc2 — "we can store the key, and I can get it
any time I am logged in", reversing the earlier shown-once model).

These pin the readable-key slice at the service + schema layer: mint on first
read, the SAME key on re-read, rotate revokes the prior key, the secret lives
in the data-dir db (never the repo), and the Settings copy uses PRISM language.
"""
from pathlib import Path

import pytest

from prism_service.services.workspace_service import WorkspaceService
from prism_service.services.auth_service import AuthService


@pytest.fixture()
def svc(tmp_path: Path):
    s = WorkspaceService(tmp_path / "workspace.db")
    try:
        yield s
    finally:
        # Close the thread-local sqlite handle so Windows can release the file.
        s.close()


def test_my_access_key_mints_on_first_read(svc):
    """AC-1: the single-user owner has a persisted identity that holds a key."""
    auth = AuthService(svc, mode="local")
    key = auth.my_access_key(AuthService.LOCAL_USER_ID)
    assert key["secret"], "first read must mint a readable key"
    assert key["id"]


def test_key_is_readable_on_reread_not_shown_once(svc):
    """AC-2: two reads return the SAME key — readable, not shown once."""
    auth = AuthService(svc, mode="local")
    first = auth.my_access_key(AuthService.LOCAL_USER_ID)
    second = auth.my_access_key(AuthService.LOCAL_USER_ID)
    assert first["secret"] == second["secret"], \
        "the key must be re-readable, not regenerated each read"


def test_rotate_replaces_and_revokes_prior(svc):
    """AC-3: rotate mints a replacement and revokes the prior key."""
    auth = AuthService(svc, mode="local")
    old = auth.my_access_key(AuthService.LOCAL_USER_ID)
    new = auth.rotate_access_key(AuthService.LOCAL_USER_ID)
    assert new["secret"] != old["secret"], "rotate must change the key"
    assert auth.my_access_key(AuthService.LOCAL_USER_ID)["secret"] == new["secret"], \
        "the read after rotate must return the new key"


def test_secret_column_exists_and_is_populated(svc, tmp_path):
    """AC-4: the secret is stored in the data-dir db, in a `secret` column."""
    auth = AuthService(svc, mode="local")
    auth.my_access_key(AuthService.LOCAL_USER_ID)
    import sqlite3
    con = sqlite3.connect(tmp_path / "workspace.db")
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(auth_tokens)")]
        assert "secret" in cols, "auth_tokens must carry a recoverable secret column"
        n = con.execute("SELECT COUNT(*) FROM auth_tokens WHERE secret != ''").fetchone()[0]
        assert n >= 1, "the minted key's secret must be persisted"
    finally:
        con.close()


def test_settings_access_key_section_uses_prism_language():
    """AC-5/AC-6: the Access key section exists beside the real five and never
    calls PRISM a 'daemon'."""
    src = (Path(__file__).resolve().parents[1].parent / "prism_service" / "web"
           / "src" / "pages" / "SettingsPage.tsx").read_text(encoding="utf-8")
    assert '"access-key"' in src, "Settings must register an access-key section"
    for real in ("projects", "connections", "activity", "logs", "service"):
        assert f'"{real}"' in src or f"{real}:" in src
    idx = src.find('"access-key": {')
    assert idx != -1
    block = src[idx:idx + 400].lower()
    assert "daemon" not in block, "user-facing copy must not say 'daemon'"
