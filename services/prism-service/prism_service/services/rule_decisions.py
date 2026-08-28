"""rule_decisions -- a firing rule becomes a decision on the Queue (task
b1971944, epic 61821448, owner model mx-ed329d: "a rule that fires
becomes a decision on the Queue and, on the owner's word, a task or a
memory").

Registers itself as an ontology_rules.on_validated listener at import
time (the same posture services.language_alignment uses for
services.ste.on_apply -- see ste.py's own note on this). Importing this
module anywhere -- api/okf.py and api/signals.py both do, at module top
-- is enough: the listener list lives on ontology_rules itself, so one
import into the process is enough for every later validate() call.

For every rule that fires, one Queue signal (channel="ontology",
channel_ref="rule:<name>") states the rule, the count, up to five focus
labels, and the four things the owner can do about it:

  accept  -- mark this decided; the rule keeps firing but nobody is
             asked about it again on this exact evidence.
  exempt  -- name focus records that do not have to comply; they drop
             out of both the Queue signal's count and GET
             /ontology/rules' own violations/focus (read-time filter,
             the SHACL runner itself never changes).
  fix     -- open a task titled "Fix: <rule title>" to bring the
             violating records into line.
  codify  -- write the owner's reasoning into Understand as a memory,
             evidenced by the rule.

All four are persisted to <project data dir>/ontology/decisions.json::

    {"<rule name>": {"accepted": {"at": iso, "reason": str},
                      "exempt": [iri, ...]}}

A signal is deduplicated on channel_ref: while an open signal (state not
in _CLOSED_STATES) already carries "rule:<name>", a re-validation updates
its body in place rather than posting a second one. A rule whose focus
count reaches zero (every violator either fixed for real or exempted)
resolves its own open signal and posts nothing new.

A DROPPED signal is excluded from that dedup lookup on purpose (task
2d315628): once the owner drops a rule's signal, a re-validation at the
SAME count must not nag again -- the drop stands, and nothing new posts.
Only a CHANGED count opens a fresh signal (a new id, the same
"rule:<name>" channel_ref -- SignalStore.create() is a plain INSERT with
no upsert on channel_ref, so nothing there needed to change), whose body
names the move ("Count moved from 8 to 9 since you dropped this.") and
which then becomes the dedup target for later re-validations. The old
dropped row is never touched again.

signal.state values this module writes -- "resolved" (accept, codify, or
exempt reaching zero) and "promoted" (fix) -- are deliberately NOT in
models.signal.SIGNAL_STATES (out of this task's allowed_files): nothing
in the store enforces membership in that tuple, and the dedup check here
tests state membership directly rather than trusting it.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from prism_service.config import project_data_dir
from prism_service.models.signal import Signal
from prism_service.services.signal_store import SignalStore

logger = logging.getLogger(__name__)

# A signal in one of these states is decided -- the dedup check and the
# "how many are still open" scoreboard count both skip it. Distinct from
# SIGNAL_STATES (models/signal.py, out of allowed_files): "dropped" is
# that tuple's own value (the owner's plain Queue drop), "resolved" and
# "promoted" are this module's own decision states.
_CLOSED_STATES = frozenset({"resolved", "dropped", "promoted"})

_FOCUS_LABEL_CAP = 5
_DECISIONS_FILE = Path("ontology") / "decisions.json"


def _rule_channel_ref(rule_name: str) -> str:
    return f"rule:{rule_name}"


def _rule_name_from_channel_ref(channel_ref: str) -> str:
    return channel_ref[len("rule:"):] if channel_ref.startswith("rule:") else channel_ref


# ---------------------------------------------------------------------
# decisions.json persistence
# ---------------------------------------------------------------------

def _decisions_path(project: str) -> Path:
    return project_data_dir(project) / _DECISIONS_FILE


def _load_decisions(project: str) -> dict:
    path = _decisions_path(project)
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("could not read decisions.json for %s", project,
                        exc_info=True)
    return {}


def _save_decisions(project: str, data: dict) -> None:
    path = _decisions_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def exempt_focus(project: str, rule_name: str) -> set[str]:
    """The focus IRIs this project has exempted from `rule_name` --
    read by both the Queue-signal writer below and GET /ontology/rules'
    decorated_report()."""
    return set((_load_decisions(project).get(rule_name) or {}).get("exempt") or [])


def _record_accept(project: str, rule_name: str, reason: str) -> None:
    data = _load_decisions(project)
    entry = data.setdefault(rule_name, {})
    entry["accepted"] = {"at": datetime.now(timezone.utc).isoformat(),
                          "reason": reason}
    _save_decisions(project, data)


def _record_exempt(project: str, rule_name: str, iris: list[str]) -> None:
    data = _load_decisions(project)
    entry = data.setdefault(rule_name, {})
    existing = set(entry.get("exempt") or [])
    existing.update(i for i in iris if i)
    entry["exempt"] = sorted(existing)
    _save_decisions(project, data)


# ---------------------------------------------------------------------
# GET /ontology/rules read-time filter (task b1971944: exempted focus
# nodes are hidden from focus and subtracted from violations WITHOUT
# touching the SHACL runner or ontology_rules.py's persisted report).
# ---------------------------------------------------------------------

def decorated_report(project: str) -> dict:
    """The same shape ontology_rules.full_report() returns, with every
    rule's exempted focus IRIs removed from both "focus" and
    "violations", and an "decision" key added when the rule has been
    accepted. Auto-validates once when no report is on file yet, same
    fallback full_report() itself uses."""
    from prism_service.services import ontology_rules
    from prism_service.services.ontology_graph import OntologyGraph

    rows = ontology_rules._read_report(project)
    if not rows:
        ontology_rules.validate(project)
        rows = ontology_rules._read_report(project)

    decisions = _load_decisions(project)
    graph = OntologyGraph(project)
    catalog = ontology_rules.rule_catalog(project)
    derived_by_name = {r["name"]: r.get("derived_from", "") for r in catalog}
    verified_by_name = {r["name"]: r.get("verified_by", "") for r in catalog}

    rules = []
    need_decision = 0
    validated_at = ""
    for r in rows:
        exempt = set((decisions.get(r["name"]) or {}).get("exempt") or [])
        remaining = [f for f in r["focus"] if f not in exempt]
        n_violations = len(remaining)
        if n_violations:
            need_decision += 1
        validated_at = r.get("validated_at", "") or validated_at
        entry = {
            "name": r["name"], "title": r.get("title", ""),
            "description": r["description"], "message": r["message"],
            "looked_at": r["looked_at"], "violations": n_violations,
            "focus": [{"iri": iri, "label": graph.label_of(iri)}
                      for iri in remaining[:20]],
            "validated_at": r["validated_at"],
            "derived_from": derived_by_name.get(r["name"], ""),
            "verified_by": verified_by_name.get(r["name"], ""),
        }
        accepted = (decisions.get(r["name"]) or {}).get("accepted")
        if accepted:
            entry["decision"] = accepted
        rules.append(entry)
    return {"rules": rules, "need_decision": need_decision,
            "total": len(rules), "validated_at": validated_at}


def _remaining_focus_now(project: str, rule_name: str) -> list[str]:
    """The rule's CURRENT persisted focus, minus every exempted IRI --
    used right after an exempt decision to decide whether the signal it
    answers can resolve immediately."""
    from prism_service.services import ontology_rules

    rows = ontology_rules._read_report(project)
    row = next((r for r in rows if r["name"] == rule_name), None)
    if row is None:
        return []
    exempt = exempt_focus(project, rule_name)
    return [f for f in row["focus"] if f not in exempt]


def _rule_title_now(project: str, rule_name: str) -> str:
    from prism_service.services import ontology_rules

    for r in ontology_rules.rule_catalog(project):
        if r["name"] == rule_name:
            return r.get("title") or rule_name
    return rule_name


# ---------------------------------------------------------------------
# The on_validated listener: one signal per firing rule
# ---------------------------------------------------------------------

def _iri_local_name(iri: str) -> str:
    """Text after the LAST '#' or '/' in `iri`, whichever comes later --
    Tries ontology_rules._local_name() FIRST (strips THIS project's own
    urn:prism:onto: prefix, which carries no trailing '#' or '/' for the
    generic split below to find) -- only when that leaves the IRI
    unchanged (it did not start with urn:prism:onto:) does this fall
    back to the generic split, so a rule that targets borrowed RDFS
    vocabulary directly -- the twin-classes rule's own sh:targetClass is
    rdfs:Class -- never leaks the full IRI into a Queue signal (task
    aa7fab99)."""
    from prism_service.services import ontology_rules

    local = ontology_rules._local_name(iri)
    if local != iri:
        return local
    if not iri:
        return iri
    idx = max(iri.rfind("#"), iri.rfind("/"))
    return iri[idx + 1:] if idx != -1 else iri


def _target_class_label(project: str, rule_name: str) -> str:
    from prism_service.services import ontology_rules

    for r in ontology_rules.rule_catalog(project):
        if r["name"] == rule_name:
            cls = r.get("target_class") or ""
            return _iri_local_name(cls) if cls else "record"
    return "record"


def _focus_labels(project: str, focus: list[str]) -> list[str]:
    from prism_service.services.ontology_graph import OntologyGraph

    graph = OntologyGraph(project)
    labels: list[str] = []
    for iri in focus[:_FOCUS_LABEL_CAP]:
        try:
            label = graph.label_of(iri)
        except Exception:
            label = ""
        labels.append(label or iri.rsplit("/", 1)[-1])
    return labels


def _rule_signal_body(project: str, row: dict, remaining: list[str],
                       dropped_count: Optional[int] = None) -> str:
    """Simplified Technical English: short sentences, one idea each.
    `dropped_count`, when given, is the focus count the owner's last drop
    of this rule's signal recorded -- names the move from that count to
    the current one (task 2d315628), so a re-opened signal says why it
    came back instead of looking like a silent duplicate."""
    n = len(remaining)
    cls = _target_class_label(project, row["name"])
    noun = f"{cls} record" if n == 1 else f"{cls} records"
    labels = _focus_labels(project, remaining)
    lines = [row.get("message") or "This rule failed."]
    if dropped_count is not None:
        lines.append(
            f"Count moved from {dropped_count} to {n} since you dropped this.")
    lines.append(f"It found {n} {noun} that fail this rule.")
    if labels:
        lines.append(f"Examples: {', '.join(labels)}.")
    derived = row.get("derived_from") or ""
    if derived:
        lines.append(f"From memory {derived} (Understand).")
    lines.append(
        "Accept: mark this decided. Exempt: leave out named records. "
        "Fix: open a task to fix this. Codify: write this as a rule "
        "the team follows.")
    return "\n".join(lines)


def _find_open_signal(project: str, channel_ref: str) -> Optional[Signal]:
    store = SignalStore(project)
    for s in store.list(limit=500):
        if s.channel_ref == channel_ref and s.state not in _CLOSED_STATES:
            return s
    return None


def _dropped_count(store: SignalStore, channel_ref: str) -> Optional[int]:
    """The focus count recorded on the most recently DROPPED signal for
    `channel_ref`, or None when there is no dropped row to compare
    against (task 2d315628). store.list() already orders newest-arrived
    first, so the first dropped match is the owner's most recent drop --
    that row's `matches.focus` is never touched by drop_signal() itself
    (it only writes state/drop_reason), so it still holds the count as it
    stood at the moment of the drop."""
    for s in store.list(limit=500):
        if s.channel_ref == channel_ref and s.state == "dropped":
            return len((s.matches or {}).get("focus") or [])
    return None


def _update_signal_body(store: SignalStore, signal_id: str, subject: str,
                         body: str, focus: list[str], rule_name: str) -> None:
    """A dedup re-post (task b1971944): route the refresh through
    SignalStore.update() only -- signal_store.py is out of this task's
    allowed_files, and every other write to a signal's own free-text
    columns is policed to live in task_service.py/memory_service.py/
    signal_store.py alone (test_every_ingestion_path_aligns.py), so this
    never reaches into the row with a second, raw SQL write of its own.
    SignalStore.update() (task aa7fab99) now re-aligns aligned_subject/
    aligned_body itself whenever subject or body changes, so this refresh
    keeps the Queue showing CURRENT aligned text rather than what the
    signal's first post produced."""
    store.update(signal_id, subject=subject, body=body,
                 matches={"rule": rule_name, "focus": focus})


def on_rules_validated(project: str, report_rows: list[dict]) -> None:
    """ontology_rules.on_validated listener: one Queue signal per firing
    rule, deduplicated on channel_ref, its body refreshed in place while
    it stays open, resolved once every focus node it names is exempted.
    Never raises -- ontology_rules._validate_impl already wraps every
    listener call, this is a second, redundant belt."""
    store = SignalStore(project)
    for row in report_rows:
        name = row.get("name")
        if not name:
            continue
        exempt = exempt_focus(project, name)
        remaining = [f for f in (row.get("focus") or []) if f not in exempt]
        channel_ref = _rule_channel_ref(name)
        existing = _find_open_signal(project, channel_ref)

        if not remaining:
            if existing is not None:
                store.update(existing.id, state="resolved")
            continue

        title = row.get("title") or name
        cls = _target_class_label(project, name)
        subject = f"Rule {title} fires on {len(remaining)} {cls}"

        if existing is not None:
            body = _rule_signal_body(project, row, remaining)
            _update_signal_body(store, existing.id, subject, body, remaining, name)
            continue

        # No open signal. If the owner dropped this rule's signal, the
        # drop stands as long as the count has not moved -- post nothing
        # (task 2d315628: no nag on an unchanged rebuild). A resolved or
        # promoted row falls straight through to a fresh post, unchanged
        # from before this task.
        dropped_count = _dropped_count(store, channel_ref)
        if dropped_count is not None and dropped_count == len(remaining):
            continue

        body = _rule_signal_body(project, row, remaining, dropped_count)
        store.create(Signal(
            project=project, channel="ontology", channel_ref=channel_ref,
            subject=subject, body=body,
            matches={"rule": name, "focus": remaining},
        ))


# ---------------------------------------------------------------------
# The owner's four answers (POST /api/signals/{id}/decide)
# ---------------------------------------------------------------------

def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:40] or "decision"


def decide(project: str, signal: Signal, action: str, reason: str,
           focus: Optional[list[str]] = None) -> dict:
    """Apply one of accept/exempt/fix/codify to an ontology rule signal.
    Raises ValueError for a signal that is not an ontology rule signal, or
    an unknown action -- the caller (api/signals.py) turns that into a 400."""
    if signal.channel != "ontology" or not signal.channel_ref.startswith("rule:"):
        raise ValueError("this signal does not name an ontology rule")
    if action not in ("accept", "exempt", "fix", "codify"):
        raise ValueError(f"unknown decision action: {action!r}")

    rule_name = _rule_name_from_channel_ref(signal.channel_ref)
    store = SignalStore(project)

    if action == "accept":
        _record_accept(project, rule_name, reason)
        store.update(signal.id, state="resolved")
        return {"action": "accept", "rule": rule_name}

    if action == "exempt":
        iris = [i for i in (focus or []) if i]
        _record_exempt(project, rule_name, iris)
        remaining = _remaining_focus_now(project, rule_name)
        if not remaining:
            store.update(signal.id, state="resolved")
        return {"action": "exempt", "rule": rule_name, "exempted": iris}

    if action == "fix":
        from prism_service.project_context import get_project

        title = f"Fix: {_rule_title_now(project, rule_name)}"
        description = signal.aligned_body or signal.body
        # channel is deliberately left blank, never signal.channel="ontology"
        # -- models.task.CHANNELS (out of this task's allowed_files) has no
        # "ontology" entry, so passing it through would 400 on every fix.
        task = get_project(project).task_svc.create(
            title=title, description=description,
            channel_ref=signal.channel_ref, workflow="triage",
            tags=["queue", "ontology"],
        )
        store.update(signal.id, state="promoted", task_id=task.id)
        return {"action": "fix", "rule": rule_name, "task": task.__dict__}

    # codify
    from prism_service.project_context import get_project

    name = f"rule-{rule_name}-{_slug(reason)}"
    description = f"{reason}\n\n{signal.aligned_body or signal.body}"
    entry = get_project(project).memory_svc.store(
        domain="ontology", name=name, description=description,
        type="decision", classification="tactical",
        evidence={"rule": rule_name},
    )
    store.update(signal.id, state="resolved")
    return {"action": "codify", "rule": rule_name, "memory_id": entry.id}


# Register at import time -- the SAME posture services.language_alignment
# uses for services.ste.on_apply. Importing this module anywhere (api/okf.py
# and api/signals.py both do, at module top, task b1971944) is enough:
# ontology_rules keeps its own listener list, so one import into the
# process registers it for every later validate() call.
from prism_service.services import ontology_rules as _ontology_rules  # noqa: E402

_ontology_rules.on_validated(on_rules_validated)
