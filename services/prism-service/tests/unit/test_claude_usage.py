"""Claude connector usage is live subscription data (task 990ff893)."""

from __future__ import annotations

import json
from pathlib import Path


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._body


def _credential(home: Path, token: str = "secret-oauth-token") -> None:
    home.mkdir(parents=True)
    (home / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": token}}), encoding="utf-8")


def test_usage_returns_live_windows_without_the_token(tmp_path, monkeypatch):
    from prism_service.api import claude_auth

    home = tmp_path / "claude"
    _credential(home)
    monkeypatch.setattr(claude_auth, "_config_dir", lambda: home)
    seen = {}

    def fetch(request, timeout):
        seen["url"] = request.full_url
        seen["auth"] = request.get_header("Authorization")
        seen["beta"] = request.get_header("Anthropic-beta")
        seen["timeout"] = timeout
        return _Response({
            "five_hour": {"utilization": 42.5, "resets_at": "2026-08-19T23:00:00Z"},
            "seven_day": {"utilization": 100, "resets_at": "2026-08-20T23:00:00Z"},
            "seven_day_fable": {"utilization": 94, "resets_at": "2026-08-20T23:00:00Z"},
            "extra_usage": {"utilization": 12, "secret_detail": "not public"},
        })

    monkeypatch.setattr(claude_auth, "urlopen", fetch)
    body = claude_auth.usage()

    assert body == {
        "available": True,
        "reason": "",
        "windows": {
            "five_hour": {"utilization": 42.5, "resets_at": "2026-08-19T23:00:00Z"},
            "seven_day": {"utilization": 100.0, "resets_at": "2026-08-20T23:00:00Z"},
            "seven_day_fable": {"utilization": 94.0, "resets_at": "2026-08-20T23:00:00Z"},
        },
    }
    assert seen["url"] == "https://api.anthropic.com/api/oauth/usage"
    assert seen["auth"] == "Bearer secret-oauth-token"
    assert seen["beta"] == "oauth-2025-04-20"
    assert "secret-oauth-token" not in json.dumps(body)
    assert "secret_detail" not in json.dumps(body)


def test_usage_is_honest_when_not_authenticated(tmp_path, monkeypatch):
    from prism_service.api import claude_auth

    monkeypatch.setattr(claude_auth, "_config_dir", lambda: tmp_path)
    monkeypatch.setattr(claude_auth, "urlopen", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("must not call upstream without a credential")))
    assert claude_auth.usage() == {
        "available": False, "reason": "not_authenticated", "windows": {}}


def test_usage_does_not_invent_values_on_upstream_failure(tmp_path, monkeypatch):
    from prism_service.api import claude_auth

    home = tmp_path / "claude"
    _credential(home)
    monkeypatch.setattr(claude_auth, "_config_dir", lambda: home)
    monkeypatch.setattr(claude_auth, "_fetch_usage", lambda _token: (_ for _ in ()).throw(OSError("offline")))
    assert claude_auth.usage() == {
        "available": False, "reason": "upstream_unavailable", "windows": {}}
