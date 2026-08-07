"""GitHub-comment collaboration adapter (epic 0784729f).

Publishes a DecisionCard as a comment on the task's mirrored GitHub issue
(the mirror `github_work.py` already creates); reads comments back and turns
ones carrying an explicit decision token into DecisionSignals a caller can
apply to the gate they name.

NEVER A SOURCE OF WORK. `poll()` only ever returns decisions it read off the
thread. It never creates a task and never advances anything itself - the
caller (whatever drives the conductor) is the one that applies each
DecisionSignal to the gate it names. This mirrors the read-only discipline
`github_work.py`'s `pull_page` already follows for issue import: this module
performs no side effect beyond returning data, except the one explicit write
in `publish`.

ECHO GUARD. A comment PRISM itself wrote (the card `publish()` just posted)
must never come back out of `poll()` as a decision - a caller and a gate
would otherwise chase their own echo forever. The adapter records the id of
every comment it publishes, keyed by target, and `poll()` skips those ids.
"""

from __future__ import annotations

from dataclasses import dataclass

from prism_service.services.collaboration import DecisionCard, DecisionSignal

_APPROVE_TOKEN = "/approve"
_REJECT_TOKEN = "/reject"


@dataclass(frozen=True)
class GitHubIssueTarget:
    """Where a DecisionCard lands: the mirrored issue for one task's gate.
    ``connection``/``container`` are the same objects github_rest.py's
    client already takes; ``task_id``/``gate_step`` ride along so a signal
    read back out of GitHub already knows which gate it decides."""

    connection: object
    container: object
    number: int
    task_id: str = ""
    gate_step: str = ""


class GitHubCommentAdapter:
    """Talks to GitHub via GithubRestClient. ``credentials``/``installation``
    follow the exact shape GitHubWorkAdapter uses (github_work.py:77-85):
    ``credentials.installation_token(installation)`` -> a bearer token."""

    surface = "github"

    def __init__(self, client, credentials=None, installation=None) -> None:
        self._client = client
        self._credentials = credentials
        self._installation = installation
        # target key -> set of comment ids PRISM itself published there.
        self._published_ids: dict[tuple, set] = {}

    def _token(self) -> str:
        if self._credentials is not None and self._installation is not None:
            return self._credentials.installation_token(self._installation)
        return ""

    @staticmethod
    def _key(target: GitHubIssueTarget) -> tuple:
        return (getattr(target.connection, "id", target.connection),
                getattr(target.container, "id", target.container),
                target.number)

    def publish(self, card: DecisionCard, target: GitHubIssueTarget) -> str:
        body = (f"**{card.gate_step}** - {card.summary}\n\n"
                f"[{card.task_title}]({card.url})\n\n"
                f"Reply `{_APPROVE_TOKEN}` or `{_REJECT_TOKEN}` to decide "
                f"this gate.")
        raw = self._client.create_comment(
            target.connection, target.container, target.number, body,
            self._token())
        comment_id = str(raw.get("id", ""))
        self._published_ids.setdefault(self._key(target), set()).add(comment_id)
        return comment_id

    def poll(self, target: GitHubIssueTarget) -> list[DecisionSignal]:
        rows = self._client.issue_comments(
            target.connection, target.container, target.number,
            self._token())
        published = self._published_ids.get(self._key(target), set())
        signals: list[DecisionSignal] = []
        for row in rows or []:
            comment_id = str(row.get("id", ""))
            if comment_id in published:
                continue  # echo guard: never our own published card
            action = _decision_token((row.get("body") or "").strip())
            if action is None:
                continue
            login = (row.get("user") or {}).get("login", "")
            signals.append(DecisionSignal(
                task_id=target.task_id, gate_step=target.gate_step,
                action=action, actor=f"github:{login}",
                external_ref=comment_id))
        return signals


def _decision_token(text: str) -> "str | None":
    if text == _APPROVE_TOKEN:
        return "approve"
    if text == _REJECT_TOKEN:
        return "reject"
    return None
