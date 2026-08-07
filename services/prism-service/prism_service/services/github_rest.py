"""The REAL GitHub REST client.

Until this file existed there was none. ``GithubWorkAdapter`` (github_work.py)
calls ``client.issues(connection, container, token)`` and
``client.pulls(...)``, and EVERY test passed a scripted object in — so the
interface was exercised constantly while nothing in production could answer it.
That is precisely how the 0784729f epic shipped five green slices over a
feature that could not run.

Task ae67ed5c adds the FIRST write: ``close_issue``, a PATCH that only ever
sends ``{"state": "closed"}``. This supersedes the "read-only in this slice"
pin that used to live here (and the matching source-text assertion in
tests/integration/test_pull_issues_into_tasks.py, which is outside this
task's allowed_files and could not be updated alongside this change — see
the task's friction log). Title, body and labels are still never touched.

Task 7cf6a2e5 adds the SECOND write: ``create_issue``, a POST carrying only
title/body/assignees — the outbound half of the mirror that lets a PRISM
task appear on GitHub in the first place.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

API_ROOT = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"
_PER_PAGE = 100


class GithubApiError(RuntimeError):
    """A refusal from GitHub, carrying the status so callers can tell an
    expired credential (401) from a missing repo (404)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"github api {status}: {message}")
        self.status = status


def _repo_path(connection, container) -> str:
    """owner/repo for a container. ``remote_id`` is the canonical value the
    integration store holds; display_key is a human label and is only a
    fallback."""
    repo = (getattr(container, "remote_id", "")
            or getattr(container, "display_key", "") or "").strip("/")
    if not repo or repo.count("/") != 1:
        raise ValueError(
            f"container remote_id must be 'owner/repo', got {repo!r}")
    owner, name = repo.split("/")
    return f"{urllib.parse.quote(owner)}/{urllib.parse.quote(name)}"


class GithubRestClient:
    """Talks to api.github.com. The transport is injectable so a test can
    exercise request shaping without a network, but the DEFAULT is real."""

    def __init__(self, transport=None, write_transport=None,
                 api_root: str = API_ROOT) -> None:
        self._transport = transport
        # A SEPARATE injection point from the read transport, so every
        # existing read-only test (and its 2-arg ``transport(url, token)``
        # double) keeps working unchanged; a write test injects its own
        # ``write_transport(url, token, body)`` double instead.
        self._write_transport = write_transport
        self._api_root = api_root.rstrip("/")

    # ── transport ─────────────────────────────────────────────────────

    def _get(self, path: str, token: str, params: Optional[dict] = None):
        url = f"{self._api_root}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        if self._transport is not None:
            return self._transport(url, token)

        req = urllib.request.Request(url, method="GET")
        req.add_header("Accept", _ACCEPT)
        req.add_header("X-GitHub-Api-Version", _API_VERSION)
        req.add_header("User-Agent", "prism-service")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "[]")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise GithubApiError(exc.code, body) from exc
        except urllib.error.URLError as exc:
            raise GithubApiError(0, str(exc.reason)) from exc

    def _patch(self, path: str, token: str, body: dict):
        return self._write(path, token, body, "PATCH")

    def _post(self, path: str, token: str, body: dict):
        return self._write(path, token, body, "POST")

    def _write(self, path: str, token: str, body: dict, method: str):
        url = f"{self._api_root}{path}"
        if self._write_transport is not None:
            return self._write_transport(url, token, body)

        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", _ACCEPT)
        req.add_header("Content-Type", "application/json")
        req.add_header("X-GitHub-Api-Version", _API_VERSION)
        req.add_header("User-Agent", "prism-service")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            resp_body = ""
            try:
                resp_body = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise GithubApiError(exc.code, resp_body) from exc
        except urllib.error.URLError as exc:
            raise GithubApiError(0, str(exc.reason)) from exc

    # ── the interface github_work.py calls ────────────────────────────

    def issues(self, connection, container, token: str) -> list:
        """Open issues for the container. GitHub returns pull requests from
        this endpoint too, so they are filtered out — pulls() owns those, and
        leaving them in would import every PR twice."""
        rows = self._get(f"/repos/{_repo_path(connection, container)}/issues",
                         token, {"state": "open", "per_page": _PER_PAGE})
        return [r for r in (rows or []) if not r.get("pull_request")]

    def pulls(self, connection, container, token: str) -> list:
        return self._get(f"/repos/{_repo_path(connection, container)}/pulls",
                         token, {"state": "open", "per_page": _PER_PAGE}) or []

    def close_issue(self, connection, container, number: int, token: str) -> dict:
        """PATCH the issue closed. The ONLY field ever sent is
        ``state: closed`` — title, body and labels are a later slice's
        problem, not this one's (task ae67ed5c)."""
        return self._patch(
            f"/repos/{_repo_path(connection, container)}/issues/{number}",
            token, {"state": "closed"})

    def create_issue(self, connection, container, title: str, body: str,
                     assignee: str, token: str) -> dict:
        """POST a new issue. ``assignees`` is a single-element list when
        ``assignee`` is given, else empty — GitHub's create endpoint takes a
        list, but this instance only ever has ONE connected account to name
        (task 7cf6a2e5: do not invent a richer identity model)."""
        payload = {"title": title, "body": body,
                  "assignees": [assignee] if assignee else []}
        return self._post(
            f"/repos/{_repo_path(connection, container)}/issues",
            token, payload)

    def create_comment(self, connection, container, number: int, body: str,
                       token: str) -> dict:
        """POST a comment onto an issue. GitHub serves PR comments through
        this same issues path, so this also works for mirrored pull requests.
        Used by the collaboration adapter to publish a decision card (epic
        0784729f: github_collab.py)."""
        return self._post(
            f"/repos/{_repo_path(connection, container)}/issues/{number}/comments",
            token, {"body": body})

    def issue_comments(self, connection, container, number: int,
                       token: str) -> list:
        """GET the comments on an issue, oldest first (GitHub's default
        order), so the collaboration adapter can read decisions back."""
        return self._get(
            f"/repos/{_repo_path(connection, container)}/issues/{number}/comments",
            token, {"per_page": _PER_PAGE}) or []
