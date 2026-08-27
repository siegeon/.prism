"""Signal — an intake row for the Queue (task a6858911).

Owner's model (mx-0889e4): the Queue is where SIGNALS arrive over their
channel (slack, outlook, github, jira, mcp collectors...) and get resolved
against the ontology. A signal is NOT a task -- it becomes one only when
the owner acts in the app. This dataclass + its store (signal_store.py)
are the walking skeleton: intake + list, nothing else.

aligned_subject/aligned_body/style (task ed034701): SignalStore.create()
runs the deterministic STE pipeline (services/ste.py) over subject/body
and stores the result here. subject/body stay exactly as they arrived --
the raw record never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

# A signal's lifecycle: open (just arrived, undecided) -> became_task (the
# owner acted on it, task_id names the resulting task) or dropped (the
# owner dismissed it, drop_reason says why). A rule signal (Rules tab,
# services/rule_decisions.py) also ends as resolved (accept, codify, or
# exempt reached zero) or promoted (fix, task_id names the fix task).
SIGNAL_STATES: tuple[str, ...] = (
    "open", "became_task", "dropped", "resolved", "promoted",
)


@dataclass
class Signal:
    id: str = field(default_factory=lambda: str(uuid4()))
    project: str = ""
    channel: str = ""
    channel_ref: str = ""
    subject: str = ""
    body: str = ""
    sender: str = ""
    arrived_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    state: str = "open"
    task_id: str = ""
    # Candidate ontology matches this signal resolved against (person,
    # team, company...) -- shape is the resolver's, this store just
    # round-trips it as JSON. Empty until a resolver runs (out of scope
    # for this slice).
    matches: dict = field(default_factory=dict)
    drop_reason: str = ""
    # Aligned text (task ed034701): the STE pipeline's output for
    # subject/body. Empty until SignalStore.create() runs, or when the
    # normaliser itself fails -- a normaliser bug must never drop a
    # signal.
    aligned_subject: str = ""
    aligned_body: str = ""
    # style_block() report for subject/body: {"fixed", "findings",
    # "aligned"}. Round-trips as JSON text in the store.
    style: dict = field(default_factory=dict)
