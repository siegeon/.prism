"""/api/claude-auth — Claude CLI authentication status for the SPA.

The container ships with `@anthropic-ai/claude-code` installed and
CLAUDE_CONFIG_DIR pointing into the data volume. Auth itself is a
one-time `claude /login` (or `claude setup-token`) the operator runs
inside the container; PRISM can't drive the OAuth browser redirect
itself without a full PTY-backed terminal in the SPA.

What this endpoint DOES provide:
  * GET /status — is .credentials.json present in CLAUDE_CONFIG_DIR?
    Returns the docker exec command line the operator should run when
    not authenticated, so the SPA can render a clear instruction.

A future Task #14 will add a PTY-backed in-browser login flow that
mirrors Auto-Claude's terminal/pty-manager pattern; until then, the
operator completes auth in their own terminal and watches the SPA
flip to "authenticated" on the next poll.
"""

from __future__ import annotations

import os
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from fastapi import APIRouter

from prism_service.api.security import redact_host_paths, team_mode_active
from prism_service.config import DATA_DIR
from prism_service.data_dir import resolve_claude_home

router = APIRouter()

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_USAGE_BETA = "oauth-2025-04-20"


def _config_dir() -> Path:
    return resolve_claude_home()


def _is_docker() -> bool:
    return str(DATA_DIR) == "/data" and Path("/etc/hostname").is_file()


def _container_name() -> str:
    """Best-effort container identifier for the docker-exec hint shown in the UI.

    Prefers an operator-supplied PRISM_CONTAINER_NAME env var (set this
    in your compose file to e.g. `prism-service-v51` or `prism-consumer`
    so the SPA shows a friendly name). Falls back to /etc/hostname which
    is the short container ID - docker accepts either as the target of
    `docker exec`, so the command works regardless.
    """
    explicit = os.environ.get("PRISM_CONTAINER_NAME", "").strip()
    if explicit:
        return explicit
    try:
        return Path("/etc/hostname").read_text(encoding="utf-8").strip()
    except OSError:
        return "prism-service"


@router.get("/status")
def status() -> dict:
    cfg = _config_dir()
    cred = cfg / ".credentials.json"
    authenticated = cred.is_file()
    name = _container_name()
    docker = _is_docker()
    body = {
        "authenticated": authenticated,
        "config_dir": str(cfg),
        "credentials_path": str(cred),
        "container": name,
        "runtime": "docker" if docker else "native",
        "login_command": (
            f"docker exec -it {name} claude /login" if docker
            else "claude /login"
        ),
        "instructions": (
            "Run the command above on your host to complete a one-time OAuth "
            "flow. Tokens land in CLAUDE_CONFIG_DIR (volume-backed in docker, "
            "~/.claude on native) and the claude CLI auto-refreshes them - you "
            "should never need to log in again unless you revoke the session."
        ),
    }
    # Team mode: any authenticated member of any workspace can reach this
    # route (security._AUTH_ONLY_PREFIXES), so it must not hand out the
    # host's filesystem layout or (via Path.home()) the host OS account name.
    return redact_host_paths(body) if team_mode_active() else body


def _oauth_access_token(cred: Path) -> str:
    """Read the Claude CLI token without ever returning or logging it."""
    try:
        body = json.loads(cred.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return ""
    oauth = body.get("claudeAiOauth") if isinstance(body, dict) else None
    return str(oauth.get("accessToken") or "") if isinstance(oauth, dict) else ""


def _fetch_usage(token: str) -> dict:
    request = Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": _USAGE_BETA,
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=8) as response:  # noqa: S310 - fixed HTTPS URL
        return json.loads(response.read().decode("utf-8"))


@router.get("/usage")
def usage() -> dict:
    """Live Claude.ai subscription windows, using the CLI's OAuth session.

    This intentionally returns only utilization and reset timestamps. The
    bearer token stays in ``.credentials.json`` and upstream response details
    cannot accidentally widen this API's public shape.
    """
    token = _oauth_access_token(_config_dir() / ".credentials.json")
    if not token:
        return {"available": False, "reason": "not_authenticated", "windows": {}}
    try:
        raw = _fetch_usage(token)
    except (HTTPError, URLError, OSError, ValueError, TypeError):
        return {"available": False, "reason": "upstream_unavailable", "windows": {}}

    windows = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if not (key == "five_hour" or key.startswith("seven_day")):
                continue
            if not isinstance(value, dict) or value.get("utilization") is None:
                continue
            try:
                utilization = max(0.0, min(100.0, float(value["utilization"])))
            except (TypeError, ValueError):
                continue
            windows[key] = {
                "utilization": utilization,
                "resets_at": str(value.get("resets_at") or ""),
            }
    return {"available": bool(windows), "reason": "" if windows else "no_usage_data", "windows": windows}
