"""Domain models for team workspaces and authentication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


Role = Literal["owner", "admin", "member", "viewer"]

# Keep the hierarchy deliberately closed.  Unknown roles must never inherit
# permissions merely because their spelling happens to sort above another
# value.
ROLE_RANKS: dict[str, int] = {
    "viewer": 0,
    "member": 1,
    "admin": 2,
    "owner": 3,
}
VALID_ROLES = frozenset(ROLE_RANKS)


def role_allows(role: str, minimum_role: str) -> bool:
    """Return whether ``role`` satisfies the explicit role hierarchy.

    Invalid roles fail closed instead of raising or being compared
    lexicographically.
    """

    role_rank = ROLE_RANKS.get(role)
    minimum_rank = ROLE_RANKS.get(minimum_role)
    return (
        role_rank is not None
        and minimum_rank is not None
        and role_rank >= minimum_rank
    )


@dataclass(frozen=True)
class User:
    id: str
    email: str
    display_name: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class Workspace:
    id: str
    name: str
    owner_user_id: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class Membership:
    workspace_id: str
    user_id: str
    role: str
    created_at: str = ""


@dataclass(frozen=True)
class ProjectOwnership:
    project_id: str
    workspace_id: str
    created_at: str = ""


# A friendlier synonym for callers that describe the row as a binding.
ProjectBinding = ProjectOwnership


@dataclass(frozen=True)
class AuthToken:
    """Persisted token metadata; the opaque secret is intentionally absent."""

    id: str
    user_id: str
    label: str = ""
    created_at: str = ""
    revoked_at: str = ""


@dataclass(frozen=True)
class IssuedToken:
    """One-time token material returned only when a token is issued."""

    id: str
    secret: str
    label: str = ""


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    display_name: str
    mode: str
    role: str
