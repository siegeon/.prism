"""ontology_terms — the Terms tab (task 7dbb242f, epic e39027d3, owner:
"not quite there with ontology"). The seven vocabularies PRISM's real code
declares (models.task.CHANNELS, models.signal.SIGNAL_STATES, task status,
models.workflow.WORKFLOWS keys, proof_type, signal_resolver's ask kinds,
gate_state), each with in_use/count computed from REAL rows (TaskService,
SignalStore) — never sqlite. held_back names values seen in the data that
sit outside a vocabulary's declared list, so nothing gets filed wrongly.
"""

from __future__ import annotations

from collections import Counter

from prism_service.models.signal import SIGNAL_STATES
from prism_service.models.task import CHANNELS, PROOF_TYPES, STATUSES
from prism_service.models.workflow import WORKFLOWS
from prism_service.project_context import get_project
from prism_service.services import lexicon
from prism_service.services import ontology_graph
from prism_service.services.signal_store import SignalStore

# Declared elsewhere as inline comments, not as a named tuple (task.py's
# `status` field, task.py's `proof_type` field, task.py's `gate_state`
# field) — named here once so this module is the one place that reads them
# back as a vocabulary, per ontology-SKILL.md's "never a second hand-kept
# list that can drift from what actually validates".
# ONE vocabulary, one place (the prototype's drift rule: never a literal
# re-declared beside the generated one). Status and proof_type come from
# models.task -- the same tuples validate_status/validate_proof_type enforce
# on write (task f5352fa1) -- so the Terms tab can never disagree with what
# the API accepts. Live 2026-08-25 this file's own copy lacked deleted/
# archived (30 + 2 real rows) and source_backed_answer.
TASK_STATUSES: tuple[str, ...] = STATUSES
GATE_STATES: tuple[str, ...] = ("none", "pending", "passed", "failed")
# signal_resolver._classify_ask's own priority-ordered kind set.
ASK_KINDS: tuple[str, ...] = ("decision", "review", "deliverable", "reply", "fyi", "unknown")

_COMMENTS = {
    "channel": "Where a task or signal arrived through.",
    "signal_state": "A signal's lifecycle stage — open, became a task, or dropped.",
    "task_status": "Where a task sits in the conductor's workflow.",
    "workflow": "Which named step list drives a task.",
    "proof_type": "How a task's oracle gets proven true.",
    "ask": "What kind of request a signal is asking for.",
    "gate_state": "Whether a task's current gate has been decided.",
}


def _lexicon_vocabulary(project: str) -> dict:
    """The lexicon vocabulary (task 2ee65e14): one row per canonical
    term, carrying the synonyms it replaces and the count of real
    instances of the class it denotes — 0 when the term denotes no
    class, or when this project has no ontology graph yet (a pure read
    must never create one, per ontology_graph.open_if_exists)."""
    og = ontology_graph.open_if_exists(project)

    def _count(denotes: str) -> int:
        if not denotes or og is None:
            return 0
        return og._count(denotes)

    rows = []
    for term in lexicon.load_lexicon():
        count = _count(term.denotes)
        rows.append({
            "value": term.label,
            "count": count,
            "in_use": count > 0,
            "comment": term.definition,
            "synonyms": list(term.alt_labels),
            "denotes": term.denotes,
        })
    return {
        "name": "lexicon",
        "comment": "Canonical terms and the synonyms they replace.",
        "terms": rows,
    }


def _rows(declared: tuple[str, ...], counts: Counter) -> list[dict]:
    return [{"value": v, "in_use": counts.get(v, 0) > 0, "count": counts.get(v, 0)}
            for v in declared]


def _held_back(vocabulary: str, declared: tuple[str, ...], counts: Counter) -> list[dict]:
    return [{"vocabulary": vocabulary, "value": v, "count": n}
            for v, n in counts.items() if v and v not in declared]


def terms(project: str) -> dict:
    """GET /api/okf/ontology/terms — the seven real vocabularies, each with
    in_use/count from real rows, plus held_back for anything seen in the
    data outside a declared list."""
    from prism_service.services.signal_resolver import _classify_ask

    ctx = get_project(project)
    tasks = ctx.task_svc.list()
    store = SignalStore(project)
    try:
        signals = store.list(limit=5000)
    finally:
        store.close()

    channel_counts: Counter = Counter()
    for t in tasks:
        if t.channel:
            channel_counts[t.channel] += 1
    for s in signals:
        if s.channel:
            channel_counts[s.channel] += 1

    signal_state_counts = Counter(s.state for s in signals if s.state)
    task_status_counts = Counter(t.status for t in tasks if t.status)
    workflow_counts = Counter(t.workflow for t in tasks if t.workflow)
    proof_type_counts = Counter(t.proof_type for t in tasks if t.proof_type)
    gate_state_counts = Counter(t.gate_state for t in tasks if t.gate_state)
    ask_counts = Counter(_classify_ask(s.subject, s.body)["kind"] for s in signals)

    vocab_specs = [
        ("channel", CHANNELS, channel_counts),
        ("signal_state", SIGNAL_STATES, signal_state_counts),
        ("task_status", TASK_STATUSES, task_status_counts),
        ("workflow", tuple(WORKFLOWS.keys()), workflow_counts),
        ("proof_type", PROOF_TYPES, proof_type_counts),
        ("ask", ASK_KINDS, ask_counts),
        ("gate_state", GATE_STATES, gate_state_counts),
    ]

    vocabularies = [
        {"name": name, "comment": _COMMENTS[name], "terms": _rows(declared, counts)}
        for name, declared, counts in vocab_specs
    ]
    # The lexicon vocabulary (task 2ee65e14) is a separate shape (definition
    # + synonyms + denoted class per term) — real code vocabularies above
    # never carry those, so it is appended rather than folded into
    # vocab_specs's declared/counts machinery.
    vocabularies.append(_lexicon_vocabulary(project))
    held_back: list[dict] = []
    for name, declared, counts in vocab_specs:
        held_back.extend(_held_back(name, declared, counts))

    return {"vocabularies": vocabularies, "held_back": held_back}
