"""Jira credential store for PRISM's issue-tracker integration.

Mirrors github_auth.py: credentials land in a chmod-600 JSON file under
the data dir (`.jira-auth.json`, atomic os.replace write) and are read
server-side. The raw secret NEVER leaves the process — only a masked
`oauth:•••last4` / `<email>:•••last4` fingerprint is ever exposed.

Two credential shapes share one store:

1.  **OAuth 3LO** (primary) — the browser dance lands a refresh token via
    set_oauth_token(); `oauth:•••last4` fingerprints it.
2.  **email + API token** (fallback) — basic auth via
    set_basic_credentials(); `<email>:•••last4` fingerprints it.

Env overrides (PRISM_JIRA_*) win over the stored file so ops can inject a
credential without writing to disk.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from prism_service.config import DATA_DIR

CREDS_FILENAME = ".jira-auth.json"
CREDS_PATH = DATA_DIR / CREDS_FILENAME


def credentials_path() -> Path:
    return CREDS_PATH


def _env_cred() -> dict | None:
    """A credential injected purely through the environment, if present.

    PRISM_JIRA_REFRESH_TOKEN / _OAUTH_TOKEN → oauth; else
    PRISM_JIRA_API_TOKEN → basic. Env wins over the on-disk file."""
    email = os.environ.get("PRISM_JIRA_EMAIL", "").strip()
    base_url = os.environ.get("PRISM_JIRA_BASE_URL", "").strip()
    oauth = (
        os.environ.get("PRISM_JIRA_REFRESH_TOKEN", "").strip()
        or os.environ.get("PRISM_JIRA_OAUTH_TOKEN", "").strip()
    )
    if oauth:
        return {"auth_type": "oauth", "token": oauth, "email": email,
                "base_url": base_url, "source": "env"}
    api_token = os.environ.get("PRISM_JIRA_API_TOKEN", "").strip()
    if api_token:
        return {"auth_type": "basic", "token": api_token, "email": email,
                "base_url": base_url, "source": "env"}
    return None


def _file_cred() -> dict | None:
    if not CREDS_PATH.is_file():
        return None
    try:
        data = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    token = (data.get("token") or "").strip()
    if not token:
        return None
    return {"auth_type": data.get("auth_type", "oauth"), "token": token,
            "email": data.get("email", ""), "base_url": data.get("base_url", ""),
            "source": "file"}


def _effective() -> dict | None:
    return _env_cred() or _file_cred()


def is_authenticated() -> bool:
    return _effective() is not None


def is_from_env() -> bool:
    return _env_cred() is not None


def source() -> str:
    cred = _effective()
    return cred["source"] if cred else "none"


def current_meta() -> dict:
    """UI-safe view of the active credential — NEVER the token."""
    cred = _effective()
    if not cred:
        return {}
    return {"auth_type": cred["auth_type"], "email": cred.get("email", ""),
            "base_url": cred.get("base_url", ""), "source": cred["source"]}


def token_fingerprint() -> str:
    """UI-safe identifier — never the token itself.

    oauth  → `oauth:•••<last4>`
    basic  → `<email>:•••<last4>`"""
    cred = _effective()
    if not cred:
        return ""
    token = cred["token"]
    tail = token[-4:] if len(token) >= 4 else token
    if cred["auth_type"] == "oauth":
        return f"oauth:•••{tail}"
    return f"{cred.get('email', '')}:•••{tail}"


def _write(payload: dict) -> None:
    """Atomic chmod-600 write, mirroring github_auth.set_token."""
    CREDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDS_PATH.with_suffix(CREDS_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Best-effort: Windows / restricted filesystems may not chmod.
        pass
    os.replace(tmp, CREDS_PATH)


def set_oauth_token(token: str, email: str = "", base_url: str = "") -> None:
    """Persist an OAuth refresh (or access) token — the primary path."""
    token = (token or "").strip()
    if not token:
        raise ValueError("token is required")
    _write({"auth_type": "oauth", "token": token,
            "email": (email or "").strip(), "base_url": (base_url or "").strip(),
            "connected_at": int(time.time())})


def set_basic_credentials(email: str, api_token: str, base_url: str = "") -> None:
    """Persist an email + API-token basic-auth credential — the fallback."""
    email = (email or "").strip()
    api_token = (api_token or "").strip()
    if not email:
        raise ValueError("email is required")
    if not api_token:
        raise ValueError("api_token is required")
    _write({"auth_type": "basic", "token": api_token, "email": email,
            "base_url": (base_url or "").strip(),
            "connected_at": int(time.time())})


def clear_token() -> bool:
    """Disconnect the OAuth credential. Returns True if one was removed.

    Scoped to OAuth on purpose: `/clear` is "disconnect the Jira OAuth
    connection", not "wipe a configured email+API-token". A basic-auth
    credential (set via set_basic_credentials) is a persistent config that
    this call leaves in place — mirrors the Slice A test contract where a
    connect(oauth)+clear reverts to unauthenticated but a configure(basic)
    survives a clear."""
    cred = _file_cred()
    if not cred or cred["auth_type"] != "oauth":
        return False
    try:
        CREDS_PATH.unlink()
    except OSError:
        return False
    return True
