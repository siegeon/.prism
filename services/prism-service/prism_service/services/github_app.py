"""Workspace-scoped GitHub App installation identity + credentials (task 1f181b49).

A team imports GitHub work through a GitHub App INSTALLATION it owns, not the
global device-flow clone credential in ``github_oauth``/``github_auth``. The
installation id is the integration connection's opaque ``remote_scope``; an
installation belongs to exactly one workspace and is scoped to a set of
repository node ids.

The App private key and the minted installation access token stay SERVER-SIDE:
they are resolved on demand and never persisted to ``integrations.db`` nor
returned in a sync receipt or a serialized external entity.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Optional


class InstallationScopeError(PermissionError):
    """Raised when an installation is used outside its workspace/repo scope."""


@dataclass(frozen=True)
class GitHubInstallation:
    workspace_id: str
    installation_id: str
    account_login: str = ""
    # The repository node ids this installation may read. Empty tuple means
    # "scope not enumerated" — callers that require an explicit allowlist should
    # treat an empty scope as "nothing in scope" for a specific repo check.
    repository_node_ids: tuple[str, ...] = field(default_factory=tuple)


def assert_repo_in_scope(
    installation: GitHubInstallation,
    repo_node_id: str,
    workspace_id: Optional[str] = None,
) -> None:
    """Refuse a repo/workspace outside the installation's scope.

    ``workspace_id`` (when supplied) must match the installation's workspace —
    an installation bound to workspace A can never be exercised under B.
    """
    if workspace_id is not None and workspace_id != installation.workspace_id:
        raise InstallationScopeError(
            f"installation {installation.installation_id!r} belongs to workspace "
            f"{installation.workspace_id!r}, not {workspace_id!r}")
    if repo_node_id not in installation.repository_node_ids:
        raise InstallationScopeError(
            f"repository {repo_node_id!r} is not in the scope of installation "
            f"{installation.installation_id!r}")


def _default_token_resolver(installation: GitHubInstallation) -> str:
    """Mint an installation access token from the server-side App private key.

    Reads ``PRISM_GITHUB_APP_PRIVATE_KEY`` / ``PRISM_GITHUB_APP_ID`` from the
    environment (never the repo, never integrations.db). The real network mint
    is out of scope for this pull-only slice; without a configured key this
    raises so a caller cannot silently fall back to the global clone credential.
    """
    key = os.environ.get("PRISM_GITHUB_APP_PRIVATE_KEY", "").strip()
    if not key:
        raise InstallationScopeError(
            "no GitHub App private key configured for installation "
            f"{installation.installation_id!r}")
    # A real implementation signs a JWT with `key`, then POSTs
    # /app/installations/{id}/access_tokens. Kept server-side and out of the
    # fixture-driven tests; the token is returned to the caller, never stored.
    raise InstallationScopeError(
        "network installation-token minting is not available in this context")


class GitHubAppCredentials:
    """Resolves an installation access token. The token is handed to the REST
    client for the duration of a pull and is never persisted."""

    def __init__(self, token_resolver: Optional[Callable[[GitHubInstallation], str]] = None) -> None:
        self._resolver = token_resolver or _default_token_resolver

    def installation_token(self, installation: GitHubInstallation) -> str:
        return self._resolver(installation)
