"""GitHub Issues/PR import adapter (task 1f181b49).

A ``WorkItemAdapter`` over the provider-neutral core (fddfd75a). Identity is the
GitHub ``node_id`` within installation/repository scope — never the mutable
issue number or repo name. GitHub's issues endpoint also returns pull requests
(each carrying a ``pull_request`` key); those are skipped by the issue path and
imported only via the pulls path as ``entity_kind="pull_request"``. Pull
requests may link to a local task via a validated single-known-key parse.

The REST client and the installation token are injected; this module performs
no network I/O of its own and never persists a credential.
"""

from __future__ import annotations

import re
from typing import Optional

from prism_service.models.integration import (
    ExternalEntityInput,
    normalize_status_category,
)
from prism_service.services.work_item_sync import PulledPage


# Convenience PR->task linkage. Matches the repo's canonical trailer spelling
# `[task:<8 hex>]`; anything else is ignored (no fuzzy matching).
_TASK_KEY_RE = re.compile(r"\[task:([0-9a-fA-F]{8})\]")


def _assignees(item: dict) -> tuple[str, ...]:
    return tuple(a.get("login", "") for a in (item.get("assignees") or []) if a.get("login"))


def _issue_input(item: dict) -> ExternalEntityInput:
    state = item.get("state", "")
    return ExternalEntityInput(
        entity_kind="issue",
        remote_id=item["node_id"],
        display_key=f"#{item.get('number', '')}",
        title=item.get("title", "") or "",
        body=item.get("body", "") or "",
        url=item.get("html_url", "") or "",
        remote_status=state,
        status_category=normalize_status_category(state),
        assignees=_assignees(item),
        revision=str(item.get("updated_at", "") or ""),
        remote_updated_at=item.get("updated_at", "") or "",
    )


def _pull_input(pr: dict) -> ExternalEntityInput:
    # A merged PR reads as done even though GitHub's raw `state` is "closed".
    raw_state = "merged" if pr.get("merged_at") else pr.get("state", "")
    return ExternalEntityInput(
        entity_kind="pull_request",
        remote_id=pr["node_id"],
        display_key=f"#{pr.get('number', '')}",
        title=pr.get("title", "") or "",
        body=pr.get("body", "") or "",
        url=pr.get("html_url", "") or "",
        remote_status=pr.get("state", "") or "",
        status_category=normalize_status_category(raw_state),
        assignees=_assignees(pr),
        revision=str(pr.get("updated_at", "") or ""),
        remote_updated_at=pr.get("updated_at", "") or "",
    )


class GitHubWorkAdapter:
    """Pull-only GitHub adapter. ``client`` exposes ``issues(connection,
    container, token)`` and ``pulls(connection, container, token)`` returning
    raw GitHub JSON lists."""

    provider = "github"

    def __init__(self, client, credentials=None, installation=None) -> None:
        self._client = client
        self._credentials = credentials
        self._installation = installation

    def _token(self) -> str:
        if self._credentials is not None and self._installation is not None:
            return self._credentials.installation_token(self._installation)
        return ""

    def pull_page(self, connection, container, cursor, page_token) -> PulledPage:
        token = self._token()
        raw_issues = self._client.issues(connection, container, token)
        raw_pulls = self._client.pulls(connection, container, token)

        entities: list[ExternalEntityInput] = []
        for item in raw_issues:
            # GitHub returns PRs from the issues endpoint too — a PR carries a
            # `pull_request` object. Never import those as issues.
            if item.get("pull_request"):
                continue
            entities.append(_issue_input(item))
        for pr in raw_pulls:
            entities.append(_pull_input(pr))

        # Single fixture/page in this slice; the core still checkpoints.
        return PulledPage(entities=entities, next_page_token=None,
                          next_cursor=str(cursor or "") or None)


def parse_task_keys(text: str) -> set[str]:
    return {m.lower() for m in _TASK_KEY_RE.findall(text or "")}


def link_pull_request(store, workspace_id, task_svc, pr_entity, text: Optional[str] = None):
    """Link a PR external entity to a local task iff its text carries EXACTLY
    one task key that resolves to EXACTLY one existing task. Returns the link
    or ``None`` (no key, unknown key, or ambiguous).
    """
    if text is None:
        text = f"{getattr(pr_entity, 'title', '')} {getattr(pr_entity, 'body', '')}"
    keys = parse_task_keys(text)
    if len(keys) != 1:
        return None
    (key,) = tuple(keys)
    matches = [t for t in task_svc.list() if t.id.lower().startswith(key)]
    if len(matches) != 1:
        return None
    task = matches[0]
    link = store.claim_import_link(workspace_id, pr_entity.id, task.id)
    store.activate_link(workspace_id, link.id)
    return link
