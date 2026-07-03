"""RED suite for prism_service.services.magic_client (task 0a5607a4).

Pins the HTTP admin channel to a headless Magic backend: request
shaping (URL join + Bearer header + JSON bodies), typed-error mapping,
credential dotfile round-trip with fingerprint redaction, env
overrides, and the two bootstrap paths (fresh instance vs already
configured). urllib is monkeypatched — no real sockets, so this file
stays OUT of daemon_exclusive.
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

from prism_service import config


@pytest.fixture
def isolated_data_dir(tmp_path: Path, monkeypatch):
    """Point config.DATA_DIR at a tmp dir and reload magic_client so its
    CONN_PATH re-resolves (same pattern as test_github_auth)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.delenv("PRISM_MAGIC_URL", raising=False)
    monkeypatch.delenv("PRISM_MAGIC_USER", raising=False)
    monkeypatch.delenv("PRISM_MAGIC_PASSWORD", raising=False)
    import importlib
    from prism_service.services import magic_client as mc_mod
    importlib.reload(mc_mod)
    yield tmp_path
    importlib.reload(mc_mod)


class _Resp:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _scripted_urlopen(monkeypatch, script, calls):
    """Replace urllib.request.urlopen with a scripted fake. Each script
    entry is either a payload (returned as JSON) or an exception to
    raise. Requests are recorded into `calls`."""
    import urllib.request as _ur

    def fake(req, timeout=None):
        assert timeout is not None, "every call must set an explicit timeout"
        calls.append(req)
        action = script.pop(0)
        if isinstance(action, Exception):
            raise action
        return _Resp(action)

    monkeypatch.setattr(_ur, "urlopen", fake)


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", None, None)


def _configure_env(monkeypatch):
    monkeypatch.setenv("PRISM_MAGIC_URL", "http://magic:4444")
    monkeypatch.setenv("PRISM_MAGIC_USER", "root")
    monkeypatch.setenv("PRISM_MAGIC_PASSWORD", "secretpw99")


# --- authenticate --------------------------------------------------------


def test_authenticate_builds_url_and_returns_ticket(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    calls, script = [], [{"ticket": "jwt-abc"}]
    _scripted_urlopen(monkeypatch, script, calls)
    ticket = mc.authenticate("http://magic:4444/", "alice", "pw1")
    assert ticket == "jwt-abc"
    url = calls[0].full_url
    assert url.startswith("http://magic:4444/magic/system/auth/authenticate")
    assert "username=alice" in url and "password=pw1" in url


def test_authenticate_http_error_maps_to_typed_error(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    calls, script = [], [_http_error(401)]
    _scripted_urlopen(monkeypatch, script, calls)
    with pytest.raises(mc.MagicError) as ei:
        mc.authenticate("http://magic:4444", "root", "bad")
    assert ei.value.status == 401


# --- authenticated operations --------------------------------------------


def test_execute_posts_hyperlambda_with_bearer(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    _configure_env(monkeypatch)
    calls = []
    script = [{"ticket": "jwt-1"}, {"result": "howdy"}]
    _scripted_urlopen(monkeypatch, script, calls)
    out = mc.execute('log.info:"x"')
    assert out == {"result": "howdy"}
    req = calls[1]
    assert req.full_url.endswith("/magic/system/evaluator/evaluate")
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer jwt-1"
    assert json.loads(req.data.decode()) == {"hyperlambda": 'log.info:"x"'}


def test_endpoints_lists_with_bearer(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    _configure_env(monkeypatch)
    calls = []
    script = [{"ticket": "jwt-1"}, [{"path": "magic/system/version", "verb": "get"}]]
    _scripted_urlopen(monkeypatch, script, calls)
    out = mc.endpoints()
    assert out[0]["path"] == "magic/system/version"
    req = calls[1]
    assert req.full_url.endswith("/magic/system/endpoints/list")
    assert req.get_header("Authorization") == "Bearer jwt-1"


def test_crudify_posts_payload(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    _configure_env(monkeypatch)
    calls = []
    script = [{"ticket": "jwt-1"}, {"result": "success"}]
    _scripted_urlopen(monkeypatch, script, calls)
    payload = {"databaseType": "sqlite", "database": "crm", "table": "customers"}
    out = mc.crudify(payload)
    assert out == {"result": "success"}
    req = calls[1]
    assert req.full_url.endswith("/magic/system/crudifier/crudify")
    assert json.loads(req.data.decode()) == payload


# --- connection store -----------------------------------------------------


def test_connection_roundtrip_and_fingerprint(isolated_data_dir):
    from prism_service.services import magic_client as mc
    assert mc.load_connection() is None
    mc.save_connection("http://magic:4444", "root", "secretpw99")
    conn = mc.load_connection()
    assert conn["url"] == "http://magic:4444"
    assert conn["password"] == "secretpw99"
    st = mc.status()
    assert st["configured"] is True
    assert "•••" in st["fingerprint"] and "pw99" in st["fingerprint"]
    assert "secretpw99" not in json.dumps(st)
    assert not list(isolated_data_dir.glob("*.tmp"))
    assert mc.clear_connection() is True
    assert mc.load_connection() is None


def test_env_overrides_beat_dotfile(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    mc.save_connection("http://file:1", "fileuser", "filepw")
    _configure_env(monkeypatch)
    conn = mc.load_connection()
    assert conn["url"] == "http://magic:4444"
    assert conn["user"] == "root"


# --- bootstrap --------------------------------------------------------------


def test_bootstrap_fresh_instance_runs_setup(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    calls = []
    script = [{"ticket": "boot-jwt"}, {"ticket": "real-jwt"}]
    _scripted_urlopen(monkeypatch, script, calls)
    out = mc.bootstrap("http://magic:4444", "newpw1234", name="op", email="op@x.io")
    assert out["configured"] is True and out["already_configured"] is False
    setup = calls[1]
    assert setup.full_url.endswith("/magic/system/config/setup")
    body = json.loads(setup.data.decode())
    assert body["password"] == "newpw1234"
    assert body.get("subscribe") in (False, None)
    assert setup.get_header("Authorization") == "Bearer boot-jwt"
    conn = mc.load_connection()
    assert conn["password"] == "newpw1234" and conn["user"] == "root"


def test_bootstrap_already_configured_uses_stored_credential(isolated_data_dir, monkeypatch):
    from prism_service.services import magic_client as mc
    mc.save_connection("http://magic:4444", "root", "storedpw")
    calls = []
    script = [_http_error(401), {"ticket": "jwt-stored"}]
    _scripted_urlopen(monkeypatch, script, calls)
    out = mc.bootstrap("http://magic:4444", "whatever")
    assert out["already_configured"] is True
    assert all("config/setup" not in c.full_url for c in calls)
