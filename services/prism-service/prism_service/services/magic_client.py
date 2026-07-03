"""HTTP admin client for a headless Magic Cloud backend.

PRISM's power-user channel to Magic (github.com/polterguy/magic):
authenticate → JWT, bootstrap a fresh instance (the root/root window →
config/setup, which PRISM must own — it closes the window permanently),
execute Hyperlambda, list endpoints, crudify, SQL. Stdlib urllib only
(house style — no httpx dep). The connection persists as a DATA_DIR
dotfile with atomic replace; only a user:•••last4 fingerprint ever
leaves the server.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from prism_service import config

CONN_PATH = config.DATA_DIR / ".magic-connection.json"

_TIMEOUT = 15


class MagicError(Exception):
    """Typed failure from the Magic backend (network or HTTP)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- HTTP core (stdlib, no httpx) ----------------------------------------


def _base(url: str) -> str:
    return url.rstrip("/")


def _request(url: str, method: str = "GET", token: str | None = None,
             payload: dict | None = None):
    headers = {"Accept": "application/json", "User-Agent": "prism-service"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if e.fp else ""
        raise MagicError(f"magic http {e.code}: {body[:200]}", status=e.code) from e
    except urllib.error.URLError as e:
        raise MagicError(f"magic network error: {e.reason}") from e
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise MagicError(f"magic returned non-json: {raw[:200]}") from e


# --- auth ------------------------------------------------------------------


def authenticate(url: str, user: str, password: str) -> str:
    """GET magic/system/auth/authenticate → JWT ticket."""
    q = urllib.parse.urlencode({"username": user, "password": password})
    out = _request(f"{_base(url)}/magic/system/auth/authenticate?{q}")
    ticket = out.get("ticket") if isinstance(out, dict) else None
    if not ticket:
        raise MagicError("magic authenticate returned no ticket")
    return ticket


def verify_ticket(url: str, token: str) -> bool:
    try:
        _request(f"{_base(url)}/magic/system/auth/verify-ticket", token=token)
        return True
    except MagicError:
        return False


# --- connection store (dotfile + env overrides) -----------------------------


def load_connection() -> dict | None:
    env_url = os.environ.get("PRISM_MAGIC_URL", "")
    if env_url:
        return {"url": _base(env_url),
                "user": os.environ.get("PRISM_MAGIC_USER", "root"),
                "password": os.environ.get("PRISM_MAGIC_PASSWORD", "")}
    if not CONN_PATH.is_file():
        return None
    try:
        return json.loads(CONN_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_connection(url: str, user: str, password: str) -> None:
    if not url.strip() or not password.strip():
        raise ValueError("url and password are required")
    payload = {"url": _base(url), "user": user or "root", "password": password}
    tmp = CONN_PATH.with_name(CONN_PATH.name + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, CONN_PATH)


def clear_connection() -> bool:
    try:
        CONN_PATH.unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def is_configured() -> bool:
    return load_connection() is not None


def _fingerprint(user: str, password: str) -> str:
    tail = password[-4:] if len(password) >= 4 else password
    return f"{user}:•••{tail}"


def status() -> dict:
    conn = load_connection()
    if not conn:
        return {"configured": False, "url": "", "user": "", "fingerprint": ""}
    return {
        "configured": True,
        "url": conn.get("url", ""),
        "user": conn.get("user", ""),
        "fingerprint": _fingerprint(conn.get("user", ""), conn.get("password", "")),
    }


# --- authenticated admin operations -----------------------------------------


def _conn_or_raise() -> dict:
    conn = load_connection()
    if not conn:
        raise MagicError("no Magic connection configured")
    return conn


def _system(path: str, method: str = "GET", payload: dict | None = None):
    conn = _conn_or_raise()
    token = authenticate(conn["url"], conn.get("user", "root"),
                         conn.get("password", ""))
    url = f"{_base(conn['url'])}/magic/system/{path}"
    return _request(url, method=method, token=token, payload=payload)


def execute(hyperlambda: str):
    """Run raw Hyperlambda via the root-only evaluator — the headless
    admin shell."""
    return _system("evaluator/evaluate", method="POST",
                   payload={"hyperlambda": hyperlambda})


def endpoints():
    return _system("endpoints/list")


def openapi():
    return _system("endpoints/openapi")


def crudify(payload: dict):
    return _system("crudifier/crudify", method="POST", payload=payload)


def sql(payload: dict):
    return _system("sql/evaluate", method="POST", payload=payload)


# --- bootstrap ---------------------------------------------------------------


def bootstrap(url: str, password: str, name: str = "", email: str = "") -> dict:
    """Own the fresh-instance setup window. root/root only authenticates
    while the backend's auth secret is unset; config/setup generates the
    real secret and closes that window permanently, so PRISM must be the
    one to call it. Idempotent: on an already-configured instance the
    stored credential is verified instead of bricking auth."""
    base = _base(url)
    try:
        boot = authenticate(base, "root", "root")
    except MagicError:
        conn = load_connection()
        if conn and conn.get("url") == base:
            authenticate(base, conn.get("user", "root"), conn.get("password", ""))
            return {"configured": True, "already_configured": True}
        raise
    _request(f"{base}/magic/system/config/setup", method="POST", token=boot,
             payload={"username": "root", "password": password,
                      "name": name, "email": email, "subscribe": False})
    save_connection(base, "root", password)
    return {"configured": True, "already_configured": False}
