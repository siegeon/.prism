"""Authentication policy for local and team PRISM deployments."""

from __future__ import annotations

import hashlib
import os
import secrets
from typing import Optional
from uuid import uuid4

from prism_service.models.workspace import IssuedToken, Principal, User, Workspace
from prism_service.services.workspace_service import WorkspaceService


class AuthenticationRequired(PermissionError):
    """Raised when a request cannot produce an authenticated principal."""


class BootstrapSecretNotConfigured(RuntimeError):
    """Raised when team bootstrap has no server-side setup secret."""


class BootstrapSecretMismatch(PermissionError):
    """Raised when a bootstrap request does not prove the setup secret."""


class AuthService:
    """Resolve stable principals without weakening team-mode authentication.

    If ``mode`` is omitted, ``PRISM_AUTH_MODE`` is read for every resolution.
    That makes environment changes visible at call time and, importantly,
    prevents a service constructed in local mode from remaining permissive
    after the process is switched to team mode.
    """

    LOCAL_USER_ID = "local-user"
    LOCAL_EMAIL = "local@prism.local"
    LOCAL_DISPLAY_NAME = "Local User"

    def __init__(
        self,
        service: WorkspaceService,
        mode: Optional[str] = None,
    ) -> None:
        self._service = service
        self._configured_mode = mode

    @property
    def mode(self) -> str:
        raw_mode = (
            self._configured_mode
            if self._configured_mode is not None
            else os.getenv("PRISM_AUTH_MODE", "local")
        )
        resolved = str(raw_mode).strip().lower()
        if resolved not in {"local", "team"}:
            raise AuthenticationRequired(
                f"unsupported PRISM_AUTH_MODE {raw_mode!r}; authentication denied"
            )
        return resolved

    def issue_token(self, user_id: str, label: str = "") -> IssuedToken:
        """Issue an opaque bearer token and persist only its SHA-256 digest."""

        token_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        self._service.store_auth_token(
            token_id=token_id,
            user_id=user_id,
            token_hash=token_hash,
            label=label,
        )
        return IssuedToken(id=token_id, secret=secret, label=label.strip())

    @staticmethod
    def require_bootstrap_secret(supplied_secret: Optional[str]) -> None:
        """Require the configured one-time setup secret in constant time."""

        configured_secret = os.getenv("PRISM_BOOTSTRAP_SECRET")
        if not configured_secret:
            raise BootstrapSecretNotConfigured(
                "team bootstrap requires PRISM_BOOTSTRAP_SECRET"
            )
        candidate = supplied_secret if isinstance(supplied_secret, str) else ""
        if not secrets.compare_digest(configured_secret, candidate):
            raise BootstrapSecretMismatch("invalid bootstrap secret")

    def bootstrap_owner(
        self,
        email: str,
        workspace_name: str,
        supplied_secret: Optional[str],
        display_name: str = "",
        token_label: str = "bootstrap",
    ) -> tuple[User, Workspace, IssuedToken]:
        """Create the first team owner and its bearer as one durable unit.

        The raw bearer exists only in this call frame and the returned
        ``IssuedToken``.  ``WorkspaceService.bootstrap_owner`` persists its
        digest in the same transaction as the user, workspace, and owner
        membership.
        """

        self.require_bootstrap_secret(supplied_secret)
        user_id = str(uuid4())
        workspace_id = str(uuid4())
        token_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        label = str(token_label or "").strip()
        user, workspace, _token = self._service.bootstrap_owner(
            email=email,
            display_name=display_name,
            workspace_name=workspace_name,
            user_id=user_id,
            workspace_id=workspace_id,
            token_id=token_id,
            token_hash=token_hash,
            token_label=label,
        )
        return user, workspace, IssuedToken(
            id=token_id,
            secret=secret,
            label=label,
        )

    def revoke_token(self, token_id: str, user_id: Optional[str] = None) -> bool:
        return self._service.revoke_token(token_id, user_id=user_id)

    def resolve_principal(self, authorization: Optional[str]) -> Principal:
        mode = self.mode
        if mode == "local":
            return Principal(
                user_id=self.LOCAL_USER_ID,
                email=self.LOCAL_EMAIL,
                display_name=self.LOCAL_DISPLAY_NAME,
                mode="local",
                role="owner",
            )

        secret = self._bearer_secret(authorization)
        if secret is None:
            raise AuthenticationRequired("a valid bearer token is required")
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        user = self._service.user_for_token_hash(token_hash)
        if user is None:
            raise AuthenticationRequired("a valid bearer token is required")
        return Principal(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            mode="team",
            role="member",
        )

    @staticmethod
    def _bearer_secret(authorization: Optional[str]) -> Optional[str]:
        if not authorization:
            return None
        parts = authorization.strip().split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
            return None
        return parts[1]
