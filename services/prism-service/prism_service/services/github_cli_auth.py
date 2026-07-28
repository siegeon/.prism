"""GitHub credentials from the CLI already logged in on this machine.

Owner decision 2026-07-28: an instance running on your own machine should not
make you register an OAuth app to read your own issues. The credential is
already here.

This is a CREDENTIAL ORIGIN, not a replacement for the GitHub App path. It
exposes ``installation_token(installation)`` so it drops straight into
``GithubWorkAdapter`` (github_work.py:82-85), which only ever wanted a token
string. github_app.py keeps serving server and multi-user installs.

Honesty rule: ``connected`` is EARNED. ``gh auth status`` performs a real API
call and reports whether the token is still valid, so a token the CLI cannot
validate reads ``needs_attention`` rather than looking healthy until the first
sync 401s. An absent or logged-out CLI is a calm, healthy state, never an
error: connecting is optional (mx-639efa).

The token is held here and handed to the adapter. It is never placed in a
response body.
"""

from __future__ import annotations

import re
import subprocess
from typing import Callable, Optional

# (argv) -> (returncode, stdout, stderr). Injected in tests so the suite never
# shells out; this is the ONLY boundary that gets stubbed.
Runner = Callable[[list], tuple]

_ACCOUNT_RE = re.compile(r"account\s+([A-Za-z0-9][\w.-]*)")


def _default_runner(args: list) -> tuple:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


class GithubCliCredentials:
    """Reads the token the GitHub CLI holds for github.com."""

    def __init__(self, runner: Optional[Runner] = None) -> None:
        self._run: Runner = runner or _default_runner

    # ── raw reads ─────────────────────────────────────────────────────

    def _status(self) -> tuple:
        """(returncode, text) from ``gh auth status``. gh writes its human
        report to stderr on some versions and stdout on others, so read both.
        Raises FileNotFoundError when gh is not installed."""
        rc, out, err = self._run(["gh", "auth", "status"])
        return rc, f"{out}\n{err}"

    def token(self) -> str:
        try:
            rc, out, _ = self._run(["gh", "auth", "token"])
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return ""
        return out.strip() if rc == 0 else ""

    def account(self) -> str:
        try:
            rc, text = self._status()
        except (FileNotFoundError, OSError, subprocess.SubprocessError):
            return ""
        if rc != 0:
            return ""
        m = _ACCOUNT_RE.search(text)
        return m.group(1) if m else ""

    # ── the interface GithubWorkAdapter already calls ─────────────────

    def installation_token(self, installation=None) -> str:
        """Same one-method shape as GitHubAppCredentials, so the adapter takes
        this without github_work.py changing at all. The installation argument
        is meaningless for a CLI token and is accepted only to honour that
        seam."""
        return self.token()

    # ── what the Connectors card renders ──────────────────────────────

    def status(self) -> dict:
        """ONE honest state, decided here rather than by the caller."""
        try:
            rc, text = self._status()
        except (FileNotFoundError, OSError):
            return {"state": "not_configured",
                    "detail": "The GitHub CLI (gh) is not installed on this "
                              "machine. Install it and run `gh auth login`, "
                              "or register an OAuth app instead.",
                    "account": ""}
        except subprocess.SubprocessError:
            return {"state": "needs_attention",
                    "detail": "The GitHub CLI did not respond. Try "
                              "`gh auth status` in a terminal.",
                    "account": ""}

        if rc == 0:
            account = self.account()
            return {"state": "connected",
                    "detail": f"Using the GitHub CLI login on this machine"
                              f"{f' ({account})' if account else ''}.",
                    "account": account}

        lowered = text.lower()
        if "not logged in" in lowered or "not logged into" in lowered:
            return {"state": "not_connected",
                    "detail": "Run `gh auth login` to use the GitHub CLI "
                              "login, or register an OAuth app.",
                    "account": ""}
        # gh ran and refused: the token exists but is expired, revoked or
        # missing scopes. Never report that as connected.
        return {"state": "needs_attention",
                "detail": "The GitHub CLI login was rejected. Run "
                          "`gh auth login` again to re-authorize.",
                "account": ""}


# ── module singleton, so the API reads one configured instance ────────

_credentials: Optional[GithubCliCredentials] = None


def set_cli_credentials(creds: Optional[GithubCliCredentials]) -> None:
    global _credentials
    _credentials = creds


def get_cli_credentials() -> GithubCliCredentials:
    global _credentials
    if _credentials is None:
        _credentials = GithubCliCredentials()
    return _credentials
