"""signal_parse — the prototype's own python-regex intake process, brought
into PRISM's ontology (task 31b737fb, epic 3a652b3b, owner: "make sure you
are not overlooking anything in the world of the python regex process when
bringing in items off the queue and adding them to the rules and knowledge
in the ontology").

pydantic v2 layer (ontology-SKILL.md "The pydantic layer"): SignalIn and
Enrichment are ``extra="allow"`` -- a poller adding a field must never take
the queue down, so an unknown key is reported as drift, never rejected.
str Enums (Channel/AskKind/Bucket/SignalState) are the same source
vocab.py reads to regenerate vocab.json for the JS/PowerShell planes.

parse(signal) runs the actual extraction: Jira ticket keys (accepted only
against a known-project set -- '10:00 AM-10:30' must yield nothing),
GitHub PR/issue refs, RFC-5322-ish addresses, permalinks (Slack archive
links, Outlook message ids, generic https URLs), and deadlines resolved
relative to arrived_at. signal_resolver persists the result into a
signal's matches['extraction'] so the ontology graph projects what was
decided, never a second re-parse (ontology-SKILL.md "the failure mode to
watch for": one concept, two representations).

gate_enrichment(raw) is the same idea pointed at a classifier's output:
any value outside the declared vocabulary is HELD BACK with a reason
rather than silently invented as a fourth bucket.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, ValidationError

from prism_service.models.signal import SIGNAL_STATES
from prism_service.models.task import CHANNELS

# ── vocabularies (str Enums -- vocab.py's own source of truth) ──────────

Channel = Enum(  # type: ignore[misc]
    "Channel",
    [(c.upper(), c) for c in CHANNELS] + [("NONE", "")],
    type=str,
)
SignalState = Enum(  # type: ignore[misc]
    "SignalState", [(s.upper(), s) for s in SIGNAL_STATES], type=str,
)
# signal_resolver._classify_ask's own priority-ordered kind set.
AskKind = Enum(  # type: ignore[misc]
    "AskKind",
    [("DECISION", "decision"), ("REVIEW", "review"),
     ("DELIVERABLE", "deliverable"), ("REPLY", "reply"),
     ("FYI", "fyi"), ("UNKNOWN", "unknown")],
    type=str,
)
# The prototype's three triage buckets.
Bucket = Enum(  # type: ignore[misc]
    "Bucket",
    [("NEEDS_ATTENTION", "needs_attention"),
     ("TEAM_UPDATES", "team_updates"),
     ("LOW_PRIORITY", "low_priority")],
    type=str,
)


class SignalIn(BaseModel):
    """Mirrors models.signal.Signal for validating a raw signal payload
    before it reaches the graph. extra='allow': an unknown key (a poller
    adding a field, a webhook's own extra payload) never crashes intake --
    it is reported back as drift on the Extraction instead."""

    model_config = ConfigDict(extra="allow")

    id: str = ""
    project: str = ""
    channel: str = ""
    channel_ref: str = ""
    subject: str = ""
    body: str = ""
    sender: str = ""
    arrived_at: str = ""
    state: SignalState = SignalState.OPEN  # type: ignore[valid-type]
    task_id: str = ""
    matches: dict = {}
    drop_reason: str = ""


class Enrichment(BaseModel):
    """A classifier's validated output -- gate_enrichment's accepted
    shape. extra='allow' for the same reason as SignalIn."""

    model_config = ConfigDict(extra="allow")

    ask_kind: AskKind  # type: ignore[valid-type]
    bucket: Bucket  # type: ignore[valid-type]
    channel: Channel  # type: ignore[valid-type]


class HeldBack(BaseModel):
    """gate_enrichment's refusal shape -- one entry per field whose raw
    value sat outside its declared vocabulary, persisted verbatim onto
    signal.matches['held_back'] (SignalStore.update persists it)."""

    held: list[dict[str, str]]

    @property
    def reason(self) -> str:
        return "; ".join(
            f"{h['field']}={h['value']!r}: {h['reason']}" for h in self.held)


class TicketRef(BaseModel):
    key: str
    project: str
    known: bool = True


class CodeRef(BaseModel):
    kind: str  # "pr" | "issue"
    number: int
    repo: str | None = None
    url: str | None = None


class Extraction(BaseModel):
    """parse()'s output -- the joins signal_resolver persists and
    OntologyGraph._emit_signals projects onto the graph."""

    model_config = ConfigDict(extra="allow")

    tickets: list[TicketRef] = []
    code_refs: list[CodeRef] = []
    addresses: list[str] = []
    permalinks: list[str] = []
    deadlines: list[str] = []
    drift: list[str] = []


# ── known Jira project keys ──────────────────────────────────────────────

def _known_jira_projects() -> set[str]:
    """Union of PRISM_KNOWN_JIRA_PROJECTS (comma list) and the Jira
    connector's own tracked containers (services/integration_store.py) --
    wrapped so an absent/unwired integration store never breaks parsing.
    Never workspace-scoped: signal_parse.parse() only ever sees a Signal,
    never a workspace_id, so this reads every jira_project container the
    connector has ensure_container()'d anywhere."""
    keys: set[str] = set()
    env = os.environ.get("PRISM_KNOWN_JIRA_PROJECTS", "")
    if env:
        keys.update(k.strip().upper() for k in env.split(",") if k.strip())
    try:
        from prism_service.services.integration_store import get_integration_store

        store = get_integration_store()
        rows = store._db.execute(
            "SELECT c.remote_id, c.display_key FROM external_containers c "
            "JOIN integration_connections conn ON conn.id = c.connection_id "
            "WHERE conn.provider = 'jira' AND c.kind = 'jira_project'"
        ).fetchall()
        for row in rows:
            key = (row["display_key"] or row["remote_id"] or "").strip().upper()
            if key:
                keys.add(key)
    except Exception:
        pass
    return keys


# ── regexes ───────────────────────────────────────────────────────────────

# Word-bounded, and the prefix must not be preceded by a digit or colon --
# '10:00 AM-10:30' must not read AM-10 as a ticket key (the known-project
# check alone already rejects it; this guards the pattern itself).
_JIRA_RE = re.compile(r"(?<![:\d])\b([A-Z][A-Z0-9]{1,9})-(\d+)\b")

_PR_WORD_RE = re.compile(r"\b(?:PR|pull request)\s*#(\d+)\b", re.IGNORECASE)
_OWNER_REPO_HASH_RE = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
_BARE_HASH_RE = re.compile(r"(?<![\w/])#(\d+)\b")
_GH_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")

_EMAIL_RE = re.compile(
    r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+"
)

_SLACK_PERMALINK_RE = re.compile(
    r"https?://[\w.-]+\.slack\.com/archives/[A-Za-z0-9]+/p\d+")
_OUTLOOK_ID_RE = re.compile(r"\bAAMk[\w+/=-]{10,}\b")
_GENERIC_URL_RE = re.compile(r"https?://[^\s<>\"']+")

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"]
_WEEKDAY_RE = re.compile(
    r"\b(?:before|by)\s+(monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b", re.IGNORECASE)
_EOD_TOMORROW_RE = re.compile(r"\beod\s+tomorrow\b", re.IGNORECASE)
_ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _extract_tickets(text: str, known_projects: set[str]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for m in _JIRA_RE.finditer(text):
        project, number = m.group(1), m.group(2)
        if project not in known_projects:
            continue
        key = f"{project}-{number}"
        if key in seen:
            continue
        seen.add(key)
        out.append({"key": key, "project": project, "known": True})
    return out


def _extract_code_refs(text: str) -> list[dict]:
    refs: list[dict] = []
    seen: set[tuple] = set()

    def _add(kind: str, number: int, repo: str | None = None,
             url: str | None = None) -> None:
        key = (kind, number, repo)
        if key in seen:
            return
        seen.add(key)
        item: dict[str, Any] = {"kind": kind, "number": number}
        if repo:
            item["repo"] = repo
        if url:
            item["url"] = url
        refs.append(item)

    for m in _GH_URL_RE.finditer(text):
        repo, kind_word, num = m.group(1), m.group(2), int(m.group(3))
        _add("pr" if kind_word == "pull" else "issue", num, repo=repo,
             url=m.group(0))

    for m in _OWNER_REPO_HASH_RE.finditer(text):
        _add("issue", int(m.group(2)), repo=m.group(1))

    for m in _PR_WORD_RE.finditer(text):
        _add("pr", int(m.group(1)))

    for m in _BARE_HASH_RE.finditer(text):
        num = int(m.group(1))
        if not any(r["number"] == num for r in refs):
            _add("issue", num)

    return refs


def _extract_addresses(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for m in _EMAIL_RE.finditer(text):
        addr = m.group(0)
        if addr not in seen:
            seen.add(addr)
            out.append(addr)
    return out


def _extract_permalinks(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _add(url: str) -> None:
        if url not in seen:
            seen.add(url)
            out.append(url)

    for m in _SLACK_PERMALINK_RE.finditer(text):
        _add(m.group(0))
    for m in _OUTLOOK_ID_RE.finditer(text):
        _add(m.group(0))
    for m in _GENERIC_URL_RE.finditer(text):
        _add(m.group(0).rstrip(").,;\"'"))
    return out


def _parse_arrived_at(value: str) -> datetime:
    """Naive UTC datetime, zoneinfo-free: an offset-aware ISO timestamp is
    normalized by subtracting its own offset, never via zoneinfo."""
    text = (value or "").strip().replace("Z", "+00:00")
    if not text:
        return datetime.utcnow()
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


def _extract_deadlines(text: str, arrived_at: str) -> list[str]:
    try:
        base = _parse_arrived_at(arrived_at)
    except Exception:
        base = datetime.utcnow()

    out: list[str] = []
    seen: set[str] = set()

    def _add(iso_date: str) -> None:
        if iso_date not in seen:
            seen.add(iso_date)
            out.append(iso_date)

    for m in _WEEKDAY_RE.finditer(text):
        target = _WEEKDAYS.index(m.group(1).lower())
        days_ahead = (target - base.weekday() + 7) % 7
        if days_ahead == 0:
            days_ahead = 7
        _add((base + timedelta(days=days_ahead)).date().isoformat())

    if _EOD_TOMORROW_RE.search(text):
        _add((base + timedelta(days=1)).date().isoformat())

    for m in _ISO_DATE_RE.finditer(text):
        try:
            datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            continue
        _add(m.group(1))

    return out


def _as_dict(signal: Any) -> dict:
    if isinstance(signal, dict):
        return dict(signal)
    if hasattr(signal, "__dict__"):
        return dict(vars(signal))
    return {}


def parse(signal: Any) -> Extraction:
    """Extract tickets/code_refs/addresses/permalinks/deadlines/drift from
    a signal (a models.signal.Signal, or any raw mapping -- never raises;
    a validation failure just means an empty drift list)."""
    data = _as_dict(signal)
    drift: list[str] = []
    try:
        signal_in = SignalIn.model_validate(data)
        if signal_in.model_extra:
            drift = sorted(signal_in.model_extra.keys())
    except ValidationError:
        pass

    subject = str(data.get("subject") or "")
    body = str(data.get("body") or "")
    text = f"{subject}\n{body}"
    arrived_at = str(data.get("arrived_at") or "")

    known_projects = _known_jira_projects()

    return Extraction(
        tickets=_extract_tickets(text, known_projects),
        code_refs=_extract_code_refs(text),
        addresses=_extract_addresses(text),
        permalinks=_extract_permalinks(text),
        deadlines=_extract_deadlines(text, arrived_at),
        drift=drift,
    )


# ── enrichment gate ───────────────────────────────────────────────────────

_ENRICHMENT_FIELDS: dict[str, type[Enum]] = {
    "ask_kind": AskKind, "bucket": Bucket, "channel": Channel,
}


def gate_enrichment(raw: dict) -> Union[Enrichment, HeldBack]:
    """Validate a classifier's raw output against the declared vocabulary.
    Any field whose value sits outside its enum is held back with a
    reason rather than silently accepted as a new value -- a typo cannot
    invent a fourth priority bucket."""
    held: list[dict[str, str]] = []
    for field, enum_cls in _ENRICHMENT_FIELDS.items():
        if field not in raw:
            continue
        value = raw[field]
        try:
            enum_cls(value)
        except ValueError:
            held.append({
                "field": field, "value": str(value),
                "reason": f"{value!r} is not a known {field} value",
            })
    if held:
        return HeldBack(held=held)
    try:
        return Enrichment(**raw)
    except ValidationError as exc:
        return HeldBack(held=[{"field": "_", "value": str(raw),
                                "reason": str(exc)}])
