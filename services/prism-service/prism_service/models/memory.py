"""Memory / expertise data models for PRISM mulch layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExpertiseEntry:
    """A single expertise record stored in mulch JSONL files."""

    id: str = ""  # mx-XXXXXX format
    type: str = ""  # pattern | convention | failure | decision
    name: str = ""
    description: str = ""
    # Plain-English 1-2 sentence rephrase of `description`, generated
    # asynchronously by the SummaryGeneratorWorker so the MemoryPage tile
    # face is human-skimmable. Empty string until the worker fills it.
    summary: str = ""
    classification: str = ""  # tactical | foundational | strategic
    recorded_at: str = ""  # ISO datetime
    outcomes: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    domain: str = ""
    recall_count: int = 0
    last_recalled: str = ""
    status: str = "active"  # active | archived | needs_review

    # Temporal validity (Graphiti-inspired)
    valid_at: str = ""  # ISO datetime — when this fact became true
    invalid_at: str = ""  # ISO datetime — when superseded (empty = still valid)

    # Quality signals
    importance: int = 5  # 1-10 scale, caller provides at write time
    memory_type: str = "semantic"  # semantic | episodic | procedural

    # Learning loop (MetaClaw-inspired)
    generation: int = 1  # increments when entry supersedes another
    effectiveness: float = 0.0  # -1.0 (hurts) to +1.0 (helps), from task outcome correlation

    # ADR structure on decision memories (task 8579d49e, a2). Queryable
    # via the memory tools; both default empty for non-ADR entries.
    adr_status: str = ""  # proposed | accepted | superseded | "" (non-ADR)
    supersedes: str = ""  # mx-XXXXXX id of the ADR this one supersedes


@dataclass
class RecallLogEntry:
    """A record of a memory entry being recalled during a task."""

    id: int = 0
    entry_id: str = ""
    entry_domain: str = ""
    query: str = ""
    recalled_at: str = ""
    task_id: str = ""  # in_progress task at time of recall
    outcome: str = ""  # positive | negative | "" (pending)


@dataclass
class HealthReport:
    """Result of a governance cycle run."""

    stale_brain_docs: int = 0
    flagged_conflicts: int = 0
    archived_this_cycle: int = 0
    stuck_tasks: int = 0
    domains_near_cap: list[str] = field(default_factory=list)
    last_governance_run: str = ""

    # Learning loop stats
    ineffective_flagged: int = 0
    effective_boosted: int = 0
