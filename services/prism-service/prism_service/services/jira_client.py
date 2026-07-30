"""Jira REST client over an injectable transport (task fbe9f26c).

Reads issues via the enhanced-JQL endpoint (Atlassian retired the old
``/rest/api/3/search``). The ACCESS token is the Bearer credential; errors are
sanitized and never carry a token. The transport (``.get(url, headers) -> dict``)
is injected so imports are deterministic and network-free in tests.
"""

from __future__ import annotations

import urllib.parse


class JiraClientError(RuntimeError):
    """A Jira request failed. The message is sanitized — never a token."""


class JiraClient:
    def __init__(self, transport) -> None:
        self._transport = transport

    def _api_base(self, cloud_id: str) -> str:
        return f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"

    def search_jql(
        self, cloud_id: str, access_token: str, jql: str,
        page_token: str | None = None, max_results: int = 50,
    ) -> dict:
        params = {"jql": jql, "maxResults": max_results,
                  "fields": "summary,status,assignee,updated"}
        if page_token:
            params["nextPageToken"] = page_token
        url = f"{self._api_base(cloud_id)}/search/jql?{urllib.parse.urlencode(params)}"
        try:
            data = self._transport.get(
                url, headers={"Authorization": f"Bearer {access_token}",
                              "Accept": "application/json"})
        except Exception as exc:  # noqa: BLE001 — sanitize, never leak the token
            raise JiraClientError(f"jira request failed: {type(exc).__name__}") from None
        return data if isinstance(data, dict) else {"issues": [], "nextPageToken": None}
