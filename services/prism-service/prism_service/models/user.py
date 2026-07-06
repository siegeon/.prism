"""User data model for PRISM (Slice C — first-class identity).

A PRISM User is a person who spans PROJECTS (scoping today is project-only
via ?project=; a User is orthogonal to that). Dependency-free by design:
models/ must not import services/ or api/ so this dataclass stays a plain
value type the store hydrates rows into.

`jira_account_id` is the LINK to an authenticated Jira account — the
account id / handle only, NEVER a token (tokens live in jira_auth.py's
chmod-600 store and never leave the process).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4


@dataclass
class User:
    """A PRISM person — spans projects, optionally linked to a Jira account."""

    id: str = ""
    name: str = ""
    handle: str = ""
    # The linked Jira account's id/handle (e.g. an accountId). '' = not
    # linked. NEVER a token — only the identity of the account.
    jira_account_id: str = ""
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            # Stable, prefixed id: 'usr_' + a short hex hash.
            self.id = "usr_" + uuid4().hex[:8]
        if not self.created_at:
            self.created_at = datetime.now(timezone.utc).isoformat()
