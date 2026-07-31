"""Authentication policy for local and team PRISM deployments."""

from __future__ import annotations

import hashlib
import ipaddress
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
        """Issue an opaque bearer token, persisting its digest AND the readable
        secret. The owner reversed the shown-once model (mx-935cc2): the key is
        readable whenever you are logged in, so the secret is stored in the
        data dir (never the repo). The digest stays for the fast bearer lookup.
        """

        token_id = str(uuid4())
        secret = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
        self._service.store_auth_token(
            token_id=token_id,
            user_id=user_id,
            token_hash=token_hash,
            label=label,
            secret=secret,
        )
        return IssuedToken(id=token_id, secret=secret, label=label.strip())

    def ensure_local_owner(self) -> User:
        """The single-user identity, persisted so it can own a key (task
        6cef97ec). Local mode's principal was synthetic (not in `users`), so
        it could not hold a token. Idempotent: created once, returned
        thereafter. This is 'you are already you because it is your
        instance' (mx-935cc2) made real."""
        user = self._service.get_user(self.LOCAL_USER_ID)
        if user is None:
            user = self._service.create_user(
                email=self.LOCAL_EMAIL,
                display_name=self.LOCAL_DISPLAY_NAME,
                user_id=self.LOCAL_USER_ID,
            )
        return user

    def is_claimed(self) -> bool:
        """True once the existing owner has claimed this instance (task
        fa52ba9e). A credential-free instance that just auto-updated to the
        identity build starts UNCLAIMED — the owner claims it once."""
        return self._service.instance_owner_id() is not None

    def claimed_owner(self) -> Optional[User]:
        owner_id = self._service.instance_owner_id()
        return self._service.get_user(owner_id) if owner_id else None

    def claim_instance(self, name: str, email: str) -> dict:
        """One-time claim: the existing owner names themselves and is handed
        their readable access key. No setup secret, no db edit (mx-935cc2 /
        mx-30fc0c). Additive only — it records who the owner is and mints a
        key; it never touches existing tasks or memory. Refuses if already
        claimed, so a second caller can never take the instance over."""
        # Refuse BEFORE touching anything — a second caller must never be able
        # to rename the owner or mint a key on the way to being rejected.
        if self.is_claimed():
            from prism_service.services.workspace_service import (
                InstanceAlreadyClaimed)
            raise InstanceAlreadyClaimed("this instance already has an owner")
        display = str(name or "").strip()
        addr = str(email or "").strip()
        if not display or not addr:
            raise ValueError("name and email are required to claim")
        # The single-user owner is the existing local principal — reuse its id
        # so anything already attributed to the local owner stays theirs.
        user = self._service.get_user(self.LOCAL_USER_ID)
        if user is None:
            self._service.create_user(
                email=addr, display_name=display, user_id=self.LOCAL_USER_ID)
        else:
            self._service.update_user(
                self.LOCAL_USER_ID, email=addr, display_name=display)
        self._service.mark_instance_claimed(self.LOCAL_USER_ID)
        key = self.my_access_key(self.LOCAL_USER_ID)
        return {
            "key": key.get("secret", ""),
            "id": key.get("id", ""),
            "label": key.get("label", ""),
            "created_at": key.get("created_at", ""),
        }

    def my_access_key(self, user_id: str) -> dict:
        """The caller's readable access key (task 6cef97ec) — mint one on first
        read so the owner always has a key to hand to agents/MCP. Returns
        {secret, label, created_at, id}. Never lists another user's key."""
        if user_id == self.LOCAL_USER_ID:
            self.ensure_local_owner()
        existing = self._service.active_key_for_user(user_id)
        if existing is not None:
            return existing
        issued = self.issue_token(user_id, label="default")
        minted = self._service.active_key_for_user(user_id)
        # active_key_for_user re-reads the stored row; fall back to the issued
        # secret if the read races (it will not in practice — same thread).
        return minted or {
            "id": issued.id, "secret": issued.secret,
            "label": issued.label, "created_at": "",
        }

    def rotate_access_key(self, user_id: str) -> dict:
        """Mint a replacement and revoke the prior key (owner: Rotate is the
        recovery path). Returns the new readable key."""
        prior = self._service.active_key_for_user(user_id)
        issued = self.issue_token(user_id, label="default")
        if prior is not None:
            self._service.revoke_token(prior["id"], user_id=user_id)
        minted = self._service.active_key_for_user(user_id)
        return minted or {
            "id": issued.id, "secret": issued.secret,
            "label": issued.label, "created_at": "",
        }

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

    LOOPBACK_ALIASES = frozenset({"localhost", "ip6-localhost"})
    # Starlette's in-process TestClient reports its peer as the literal
    # "testclient" — never a real socket, so it cannot be a remote caller.
    # Named explicitly rather than trusting every unparseable host, so the
    # rule below can fail CLOSED on anything unexpected.
    NON_SOCKET_HOSTS = frozenset({"testclient"})

    @classmethod
    def is_loopback(cls, client_host: Optional[str]) -> bool:
        """True when the caller is on the machine running PRISM.

        ``None``/empty means "no peer information" — a direct in-process call,
        not a socket — and stays trusted so local-mode callers keep working.
        Anything that is neither loopback nor a known in-process harness is
        treated as REMOTE, including a hostname we cannot parse: a security
        rule that guesses "probably local" is not a rule. In production
        ``request.client.host`` comes straight off the socket and is always an
        IP address, so the parse only fails under a test harness.
        """
        if client_host is None:
            return True
        host = client_host.strip().lower()
        if not host:
            return True
        if host in cls.LOOPBACK_ALIASES or host in cls.NON_SOCKET_HOSTS:
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False
        mapped = getattr(addr, "ipv4_mapped", None)
        if mapped is not None:
            addr = mapped
        return addr.is_loopback

    def _local_owner_principal(self) -> Principal:
        return Principal(
            user_id=self.LOCAL_USER_ID,
            email=self.LOCAL_EMAIL,
            display_name=self.LOCAL_DISPLAY_NAME,
            mode="local",
            role="owner",
        )

    def resolve_principal(
        self,
        authorization: Optional[str],
        client_host: Optional[str] = None,
    ) -> Principal:
        mode = self.mode
        if mode == "local":
            # The machine running PRISM is unchanged: no credential needed.
            if self.is_loopback(client_host):
                return self._local_owner_principal()
            # Anywhere else IS a remote caller. The daemon binds 0.0.0.0, so
            # without this every peer on the network resolved to the owner.
            # The credential is the key already surfaced at /api/auth/my-key.
            secret = self._bearer_secret(authorization)
            if secret is None:
                raise AuthenticationRequired(
                    "this PRISM is being reached over the network, so it needs "
                    "your access key. Send it as 'Authorization: Bearer <key>' "
                    "— copy the key from Settings on the machine running PRISM."
                )
            token_hash = hashlib.sha256(secret.encode("utf-8")).hexdigest()
            user = self._service.user_for_token_hash(token_hash)
            if user is None or user.id != self.LOCAL_USER_ID:
                raise AuthenticationRequired(
                    "that access key is not valid for this PRISM. Copy a fresh "
                    "one from Settings on the machine running PRISM, or rotate "
                    "it there if it may have leaked."
                )
            return self._local_owner_principal()

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
