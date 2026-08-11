"""The REAL Jira write client (task 88a7da0b).

Mirrors github_rest.py's minimal shape: exactly the verbs the outbound mirror
needs, no update/delete/comment. ``jira_client.py`` stays read-only (pull);
this is the write half, injectable the same way -- a test hands in
``transport``/``write_transport`` callables, production gets a real
``urllib`` transport by default.

Jira has no simple "close" verb the way GitHub does: an issue moves by
POSTing a transition id, and the id is per-project-workflow, so the caller
must GET /transitions first and choose one (jira_work.py:JiraWorkAdapter.close
does that choosing; this module only carries the two raw calls).
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

DEFAULT_ISSUE_TYPE = "Task"
ISSUE_TYPE_ENV = "PRISM_JIRA_ISSUE_TYPE"


class JiraRestError(RuntimeError):
    """A refusal from the Jira write API. The message is sanitized -- never
    a token, never raw provider response text (NFR-1)."""


def _issue_type() -> str:
    return os.environ.get(ISSUE_TYPE_ENV, "").strip() or DEFAULT_ISSUE_TYPE


def _adf_text(body: str) -> dict:
    """Wrap plain text as a single-paragraph Atlassian Document Format doc.

    An empty ``body`` yields a doc with NO paragraph node (FR-3) -- Jira's
    ADF validator rejects a paragraph whose own content list is empty, so
    omitting the node entirely is the only valid way to say "no description".
    """
    content = []
    if body:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": body}],
        })
    return {"type": "doc", "version": 1, "content": content}


def _basic_auth_header(credential) -> str:
    raw = f"{credential.email}:{credential.secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


class JiraRestClient:
    """Talks to ``{site_url}/rest/api/3``. The transport is injectable so a
    test can exercise request shaping without a network; the DEFAULT is
    real. Every method takes the resolved ``JiraCredential`` directly (Basic
    auth, FR-2) rather than a bare token string."""

    def __init__(self, transport=None, write_transport=None) -> None:
        self._transport = transport
        self._write_transport = write_transport

    # ── transport ─────────────────────────────────────────────────────

    def _get(self, url: str, credential):
        if self._transport is not None:
            return self._transport(url, credential)
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", _basic_auth_header(credential))
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise JiraRestError(
                f"jira request failed: {type(exc).__name__}") from None

    def _post(self, url: str, credential, body: dict):
        if self._write_transport is not None:
            return self._write_transport(url, credential, body)
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Authorization", _basic_auth_header(credential))
        req.add_header("Accept", "application/json")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            raise JiraRestError(
                f"jira request failed: {type(exc).__name__}") from None

    # ── the interface jira_work.py calls ────────────────────────────────

    def create_issue(self, site_url: str, credential, project_key: str,
                     summary: str, description: str = "",
                     issue_type: str = "") -> dict:
        """POST a new issue (FR-2). ``issue_type`` defaults to
        ``DEFAULT_ISSUE_TYPE``/``PRISM_JIRA_ISSUE_TYPE`` (FR-4); an
        unsupported type is never guessed at or retried with a different
        value -- the provider's refusal surfaces sanitized, as-is."""
        url = f"{site_url.rstrip('/')}/rest/api/3/issue"
        body = {
            "fields": {
                "project": {"key": project_key},
                "summary": summary,
                "description": _adf_text(description),
                "issuetype": {"name": issue_type or _issue_type()},
            }
        }
        try:
            return self._post(url, credential, body)
        except JiraRestError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize, never leak the cause
            raise JiraRestError(
                f"jira create issue failed: {type(exc).__name__}") from None

    def get_transitions(self, site_url: str, credential, issue_key: str) -> dict:
        """GET the transitions available on ``issue_key`` right now -- the
        set is per-project-workflow and cannot be assumed (FR-7)."""
        url = f"{site_url.rstrip('/')}/rest/api/3/issue/{issue_key}/transitions"
        try:
            return self._get(url, credential)
        except JiraRestError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize, never leak the cause
            raise JiraRestError(
                f"jira get transitions failed: {type(exc).__name__}") from None

    def transition_issue(self, site_url: str, credential, issue_key: str,
                         transition_id: str) -> dict:
        """POST the chosen transition. The caller (JiraWorkAdapter.close)
        already picked ``transition_id`` from ``get_transitions``; this never
        chooses on its own."""
        url = f"{site_url.rstrip('/')}/rest/api/3/issue/{issue_key}/transitions"
        try:
            return self._post(url, credential, {"transition": {"id": transition_id}})
        except JiraRestError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize, never leak the cause
            raise JiraRestError(
                f"jira transition issue failed: {type(exc).__name__}") from None
