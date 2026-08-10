"""Jira issue import adapter (task fbe9f26c).

A ``WorkItemAdapter`` over the provider-neutral core (fddfd75a). Identity is the
STABLE Jira issue id (``issue["id"]``) within connection/site scope — never the
mutable issue key, so identical keys under two different cloudId connections
never collide. The raw remote status is preserved and normalized for filtering;
it never enters the conductor. The access token is resolved per pull and never
persisted.
"""

from __future__ import annotations

import re
from typing import Callable, Optional

from prism_service.models.integration import (
    ADAPTER_ERROR,
    ExternalEntityInput,
    normalize_status_category,
)
from prism_service.services.jira_client import JiraClientError
from prism_service.services.work_item_sync import AdapterError, PulledPage

# A Jira project KEY, validated BEFORE interpolation into JQL — closes the
# recorded JQL-injection defect mx-8f4666 (task 64ba4755, FR-5).
_PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _assignees(fields: dict) -> tuple[str, ...]:
    assignee = fields.get("assignee")
    if not assignee:
        return ()
    name = assignee.get("displayName") or assignee.get("accountId") or ""
    return (name,) if name else ()


def _issue_input(issue: dict, site_url: str = "") -> ExternalEntityInput:
    """``site_url`` is the connection's own stored site (task 64ba4755,
    FR-6) — the browse URL is built from it, never hardcoded empty; with no
    known site yet it stays "" rather than a broken partial URL."""
    fields = issue.get("fields") or {}
    status = (fields.get("status") or {}).get("name", "")
    key = str(issue.get("key", ""))
    url = f"{site_url.rstrip('/')}/browse/{key}" if site_url and key else ""
    return ExternalEntityInput(
        entity_kind="jira_issue",
        remote_id=str(issue["id"]),                 # stable identity
        display_key=key,                             # mutable, display-only
        title=fields.get("summary", "") or "",
        url=url,
        remote_status=status,
        status_category=normalize_status_category(status),
        assignees=_assignees(fields),
        revision=str(fields.get("updated", "") or ""),
        remote_updated_at=fields.get("updated", "") or "",
    )


class JiraWorkAdapter:
    """Pull-only Jira adapter. ``client.search_jql(cloud_id, access_token, jql,
    page_token)`` returns a raw enhanced-JQL response."""

    provider = "jira"

    def __init__(
        self, client, access_token_provider: Callable,
        site_url_provider: Optional[Callable] = None,
    ) -> None:
        self._client = client
        self._token = access_token_provider
        # Optional (task 64ba4755, FR-6): production wires this to
        # JiraAuthStore.site_url; tests that don't care about the browse
        # URL simply omit it and get "" (never a broken partial URL).
        self._site_url = site_url_provider or (lambda connection: "")

    def pull_page(self, connection, container, cursor, page_token) -> PulledPage:
        cloud_id = connection.remote_scope
        project_key = container.remote_id or container.display_key
        # Validate the project key BEFORE it ever reaches JQL interpolation
        # (task 64ba4755, FR-5, mx-8f4666) — an invalid key must never call
        # the token provider or the transport at all.
        if not _PROJECT_KEY_RE.fullmatch(project_key or ""):
            raise AdapterError(ADAPTER_ERROR, "invalid jira project key")

        # A token or network failure must degrade to a canonical, sanitized
        # AdapterError (never a raw exception through pull_container) - task
        # 33798164. The token callable can raise anything (expired refresh,
        # no store row); JiraClientError already sanitizes transport errors,
        # but its message could still be provider text, so it is re-wrapped
        # too rather than left to surface verbatim.
        try:
            access_token = self._token(connection)
        except Exception as exc:  # noqa: BLE001 - sanitize, never leak the cause
            raise AdapterError(
                ADAPTER_ERROR, f"jira token unavailable: {type(exc).__name__}") from None

        # FR-5: scoped to the connected user's own in-flow work, not the
        # whole project.
        jql = (f'project = "{project_key}" AND assignee = currentUser() '
               f'AND statusCategory != Done')
        try:
            resp = self._client.search_jql(cloud_id, access_token, jql, page_token)
        except JiraClientError as exc:
            raise AdapterError(ADAPTER_ERROR, str(exc)) from None

        try:
            site_url = self._site_url(connection) or ""
        except Exception:  # noqa: BLE001 - a site-url lookup must never fail the pull
            site_url = ""

        issues = resp.get("issues") or []
        entities = [_issue_input(i, site_url) for i in issues]
        next_cursor = entities[-1].remote_updated_at if entities else None
        return PulledPage(
            entities=entities,
            next_page_token=resp.get("nextPageToken"),
            next_cursor=next_cursor,
        )
