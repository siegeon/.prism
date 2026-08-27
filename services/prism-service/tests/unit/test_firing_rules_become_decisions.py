"""A firing rule becomes a decision on the Queue (task b1971944, epic
61821448, owner model mx-ed329d: "a rule that fires becomes a decision
on the Queue and, on the owner's word, a task or a memory").

Pins:

  1. ontology_rules.validate() (via a rebuild) posts one Queue signal per
     firing rule; a second validation refreshes it in place rather than
     duplicating it.
  2. accept persists {"accepted": {"at","reason"}} to decisions.json and
     resolves the signal.
  3. exempt hides the named focus IRI from GET /ontology/rules (focus and
     violations both), resolves the signal once it is the rule's only
     violator, and the next validation posts nothing new for that rule.
  4. fix promotes the signal into a real "Fix: <rule title>" task.
  5. codify writes an Understand memory carrying evidence.rule.
  6. services.knowledge_health.metrics() computes from seeded brain.db/
     graph.db/memory stores, cached across a second call.
  7. QueuePage/WorkflowsPage source carries the decision affordances (no
     JS test runner in this repo -- UI ACs are pinned against the real
     TSX source, per test_conductor_page_animated_cleanup_ui.py's
     convention).
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _project_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed_two_firing_rules(pid: str) -> None:
    """A task with no channel (fires task-names-its-channel) and a doc
    loose in the project root (fires no-artifacts-in-the-root) -- the
    same fixture shape test_rules_are_shacl_shapes.py's own
    seeded_project fixture uses."""
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_project

    ctx = get_project(pid)
    ctx.task_svc.create(title="legacy task")  # blank channel

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1', 'README.md')")  # loose in root
    conn.commit()
    conn.close()


def _rebuild(pid: str) -> dict:
    from prism_service.services import ontology_prototype_projection as proj

    return proj.rebuild(pid)


def _open_ontology_signals(pid: str):
    from prism_service.services.signal_store import SignalStore

    return [s for s in SignalStore(pid).list(limit=500) if s.channel == "ontology"]


# ---------------------------------------------------------------------------
# One Queue signal per firing rule; a second validation does not duplicate
# ---------------------------------------------------------------------------

def test_two_firing_rules_post_two_signals_no_duplicate_on_revalidate():
    from collections import Counter

    from prism_service.services import ontology_rules
    from prism_service.services import rule_decisions  # noqa: F401 (registers the listener)

    pid = _project_id("rule-decisions")
    _seed_two_firing_rules(pid)
    _rebuild(pid)

    # The two rules THIS fixture deliberately trips (task-names-its-channel
    # via the channel-less task, no-artifacts-in-the-root via the loose
    # doc) each get their own open signal -- other structural rules the
    # project's own baseline scaffolding may also trip are not this
    # test's concern, so this checks presence + shape, not the exact set.
    watched = ("rule:task-names-its-channel", "rule:no-artifacts-in-the-root")
    by_ref = {s.channel_ref: s for s in _open_ontology_signals(pid)}
    for ref in watched:
        assert ref in by_ref, (ref, sorted(by_ref))
        signal = by_ref[ref]
        assert signal.state == "open"
        for word in ("Accept", "Exempt", "Fix", "Codify"):
            assert word in signal.body, (ref, signal.body)

    # A second validate() must not post a second signal for either rule --
    # it refreshes the existing one's body in place.
    ontology_rules.validate(pid)
    counts = Counter(s.channel_ref for s in _open_ontology_signals(pid))
    for ref in watched:
        assert counts[ref] == 1, (ref, counts)


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------

def test_accept_resolves_the_signal_and_records_the_decision():
    from prism_service.services import rule_decisions
    from prism_service.services.signal_store import SignalStore

    pid = _project_id("rule-accept")
    _seed_two_firing_rules(pid)
    _rebuild(pid)

    store = SignalStore(pid)
    signal = next(s for s in _open_ontology_signals(pid)
                  if s.channel_ref == "rule:task-names-its-channel")

    result = rule_decisions.decide(pid, signal, "accept",
                                    "We know about this. Leave it.")
    assert result["action"] == "accept"

    updated = store.get(signal.id)
    assert updated.state == "resolved"

    decisions = rule_decisions._load_decisions(pid)
    accepted = decisions["task-names-its-channel"]["accepted"]
    assert accepted["reason"] == "We know about this. Leave it."
    assert accepted["at"]


# ---------------------------------------------------------------------------
# exempt
# ---------------------------------------------------------------------------

def test_exempt_hides_focus_from_rules_route_and_next_post_counts_it_out():
    from prism_service.api import okf
    from prism_service.services import ontology_rules
    from prism_service.services import rule_decisions
    from prism_service.services.signal_store import SignalStore

    pid = _project_id("rule-exempt")
    _seed_two_firing_rules(pid)
    _rebuild(pid)

    okf._HOSTS.clear()
    before = okf.ontology_rules(project=pid)
    row = next(r for r in before["rules"] if r["name"] == "task-names-its-channel")
    assert row["violations"] >= 1
    focus_iri = row["focus"][0]["iri"]

    store = SignalStore(pid)
    signal = next(s for s in _open_ontology_signals(pid)
                  if s.channel_ref == "rule:task-names-its-channel")

    result = rule_decisions.decide(pid, signal, "exempt", "not applicable",
                                    [focus_iri])
    assert result["action"] == "exempt"
    assert focus_iri in result["exempted"]

    after = okf.ontology_rules(project=pid)
    row_after = next(r for r in after["rules"] if r["name"] == "task-names-its-channel")
    assert row_after["violations"] == 0
    assert all(f["iri"] != focus_iri for f in row_after["focus"])

    # The signal's only violator is now exempted -- it resolves.
    updated = store.get(signal.id)
    assert updated.state == "resolved"

    # A fresh validate() counts the exempted focus node out and posts
    # nothing new for this rule -- no signal in a non-closed state.
    ontology_rules.validate(pid)
    still_open = [s for s in _open_ontology_signals(pid)
                  if s.channel_ref == "rule:task-names-its-channel"
                  and s.state not in rule_decisions._CLOSED_STATES]
    assert still_open == []


# ---------------------------------------------------------------------------
# fix
# ---------------------------------------------------------------------------

def test_fix_promotes_the_signal_into_a_real_task():
    from prism_service.project_context import get_project
    from prism_service.services import rule_decisions
    from prism_service.services.signal_store import SignalStore

    pid = _project_id("rule-fix")
    _seed_two_firing_rules(pid)
    _rebuild(pid)

    store = SignalStore(pid)
    signal = next(s for s in _open_ontology_signals(pid)
                  if s.channel_ref == "rule:no-artifacts-in-the-root")

    result = rule_decisions.decide(pid, signal, "fix", "please clean this up")
    assert result["action"] == "fix"
    task = result["task"]
    assert task["title"].startswith("Fix: ")

    real_task = get_project(pid).task_svc.get(task["id"])
    assert real_task is not None
    assert real_task.title == task["title"]

    updated = store.get(signal.id)
    assert updated.state == "promoted"
    assert updated.task_id == task["id"]


# ---------------------------------------------------------------------------
# codify
# ---------------------------------------------------------------------------

def test_codify_stores_a_memory_carrying_evidence_rule():
    from prism_service.project_context import get_project
    from prism_service.services import rule_decisions
    from prism_service.services.signal_store import SignalStore

    pid = _project_id("rule-codify")
    _seed_two_firing_rules(pid)
    _rebuild(pid)

    store = SignalStore(pid)
    signal = next(s for s in _open_ontology_signals(pid)
                  if s.channel_ref == "rule:task-names-its-channel")

    result = rule_decisions.decide(
        pid, signal, "codify", "Every task must always name its channel.")
    assert result["action"] == "codify"
    memory_id = result["memory_id"]

    entry = get_project(pid).memory_svc.get_entry(memory_id)
    assert entry is not None
    assert entry.domain == "ontology"
    assert entry.evidence.get("rule") == "task-names-its-channel"
    assert "Every task must always name its channel." in entry.description

    updated = store.get(signal.id)
    assert updated.state == "resolved"


# ---------------------------------------------------------------------------
# knowledge_health.metrics()
# ---------------------------------------------------------------------------

def test_metrics_compute_from_seeded_stores_and_cache():
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_project
    from prism_service.services import knowledge_health

    pid = _project_id("knowledge-health")
    ctx = get_project(pid)
    ctx.memory_svc.store(
        domain="patterns", name="use-x", description="Use X for Y.",
        type="pattern", classification="tactical",
        evidence={"file_paths": ["services/foo.py"]})
    ctx.memory_svc.store(
        domain="patterns", name="use-z", description="Use Z with care. It runs slowly.",
        type="pattern", classification="tactical", evidence={})

    # IF NOT EXISTS throughout: get_project()/memory_svc.store() above may
    # already have lazily initialized the real Brain engine schema for
    # this project (both brain.db and graph.db), same real columns this
    # test inserts into either way.
    graph_db = project_data_dir(pid) / "graph.db"
    conn = sqlite3.connect(str(graph_db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS entities (id INTEGER PRIMARY KEY, "
        "name TEXT, kind TEXT, file TEXT, line INTEGER)")
    conn.execute(
        "INSERT INTO entities (name, kind, file) VALUES ('foo', 'function', "
        "'services/foo.py')")
    conn.commit()
    conn.close()

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, domain TEXT, domains TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS search_feedback (id INTEGER PRIMARY KEY "
        "AUTOINCREMENT, search_id INTEGER, signal TEXT)")
    memory_row_id = conn.execute(
        "INSERT INTO searches (query, domain, domains) "
        "VALUES ('use x', 'memory', '') RETURNING id").fetchone()[0]
    conn.execute(
        "INSERT INTO searches (query, domain, domains) "
        "VALUES ('use z', 'code', '')")
    conn.execute(
        "INSERT INTO search_feedback (search_id, doc_id, signal) "
        "VALUES (?, 'services/foo.py', 'used')",
        (memory_row_id,))
    conn.commit()
    conn.close()

    result = knowledge_health.metrics(pid)
    assert result["search_feedback_rate"] == 0.5
    assert result["recall_to_use_rate"] == 1.0
    assert result["evidence_ratio"] == 0.5
    assert result["concepts_grounded_in_code"] == 1
    assert result["modules_with_knowledge"] == 1
    assert result["median_memory_chars"] > 0
    assert result["computed_at"]

    # Cached for 60s: a second call within the window returns the exact
    # same dict, not a fresh sqlite scan.
    again = knowledge_health.metrics(pid)
    assert again is result


# ---------------------------------------------------------------------------
# QueuePage / WorkflowsPage source (no JS test runner in this repo)
# ---------------------------------------------------------------------------

def _queue_page_src() -> str:
    return (_WEB / "pages" / "QueuePage.tsx").read_text(encoding="utf-8")


def test_queue_page_renders_the_four_decision_affordances_for_ontology_signals():
    src = _queue_page_src()
    assert 'signal.channel === "ontology"' in src
    for marker in (
        "data-signal-decide-accept", "data-signal-decide-exempt",
        "data-signal-decide-fix", "data-signal-decide-codify",
        "data-signal-decide-reason",
    ):
        assert marker in src, marker


def _workflows_page_src() -> str:
    return (_WEB / "pages" / "WorkflowsPage.tsx").read_text(encoding="utf-8")


def test_workflows_page_renders_the_knowledge_health_metrics_table():
    src = _workflows_page_src()
    assert "workflow.metrics" in src
    assert "Knowledge health" in src
