"""Signal — an intake row for the Queue (task a6858911).

Owner's model (mx-0889e4): the Queue is where SIGNALS arrive over their
channel (slack, outlook, github, jira, mcp collectors...) and get resolved
against the ontology. A signal is NOT a task -- it becomes one only when
the owner acts in the app. This dataclass + its store (signal_store.py)
are the walking skeleton: intake + list, nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

# A signal's lifecycle: open (just arrived, undecided) -> became_task (the
# owner acted on it, task_id names the resulting task) or dropped (the
# owner dismissed it, drop_reason says why).
SIGNAL_STATES: tuple[str, ...] = ("open", "became_task", "dropped")


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
