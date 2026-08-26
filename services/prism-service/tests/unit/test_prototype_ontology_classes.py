"""Prototype ontology classes join the Subsume model (task 15c06516).

Owner's top priority: "i dont see the ontology stuff". Walking skeleton per
owner directive 2026-08-25 (ontology first, no roll-up) — persisted classes/
instances/properties/axioms in a real sqlite ontology.db, populated from REAL
PRISM rows (never computed at request time), served by /api/okf/ontology*,
and rendered in /understand with Subsume's shape-carries-kind primitives
copied near-verbatim from the fe62a2ee prototype, on PRISM's own fonts/tokens.

The SPA has no JS test runner, so the UI clause is pinned by reading the
ACTUAL TSX/CSS source (see test_tasks_page_unified_queue.py for the pattern).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_INDEX_CSS = _SRC / "index.css"
# Re-anchored to OntologyPage.tsx (task eca23a10-2922-4b4d-b092-83b1d1d4c082):
# the Ontology entry point moved off UnderstandPage's Concepts/Ontology
# toggle onto its own /ontology page.
_ONTOLOGY_PAGE = _SRC / "pages" / "OntologyPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


@pytest.fixture
def project(tmp_path_factory):
    """A throwaway project under the suite-pinned PRISM_DATA_DIR (see
    tests/unit/test_data_dir_isolation.py) — unique per test so parallel
    runs never collide."""
    from prism_service.project_context import get_project
    import uuid

    pid = f"ontology-test-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    ctx.task_svc.create(title="ui task", channel="ui")
    ctx.task_svc.create(title="mcp task", channel="mcp")
    ctx.task_svc.create(title="legacy task")  # blank channel

    # QueueItem now projects from signals, not tasks (task 785bb4ce) --
    # the tasks above still seed Channel/Task/the axiom evaluation.
    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    sig_store = SignalStore(pid)
    sig_store.create(Signal(project=pid, channel="ui", subject="first signal"))
    sig_store.create(Signal(project=pid, channel="slack", subject="second signal"))

    from prism_service.config import project_data_dir

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1','services/foo.py')")
    conn.execute("INSERT INTO docs VALUES ('d2','services/bar.py')")
    conn.commit()
    conn.close()

    graph_db = project_data_dir(pid) / "graph.db"
    conn = sqlite3.connect(str(graph_db))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE relationships (relation TEXT)")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('foo', 'function')")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('Bar', 'class')")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('baz', 'function')")
    conn.execute("INSERT INTO relationships VALUES ('calls')")
    conn.execute("INSERT INTO relationships VALUES ('imports')")
    conn.commit()
    conn.close()

    return pid


# ---------------------------------------------------------------------------
# AC-1: OntologyStore persists real tables (never request-time computation)
# ---------------------------------------------------------------------------

def test_ontology_store_persists_real_sqlite_tables(project):
    from prism_service.services.ontology_store import OntologyStore
    from prism_service.config import project_data_dir

    store = OntologyStore(project)
    store.replace_all(
        classes=[{"id": "C1", "name": "C1", "kind": "class", "source": "x",
                  "instance_count": 1}],
        instances=[{"id": "I1", "class_id": "C1", "label": "one"}],
        properties=[], axioms=[],
    )
    store.close()

    db_path = project_data_dir(project) / "ontology.db"
    assert db_path.exists()
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT name FROM ontology_classes").fetchall()
    conn.close()
    assert rows == [("C1",)]


# ---------------------------------------------------------------------------
# AC-2: the projection populates from REAL rows
# ---------------------------------------------------------------------------

def test_projection_populates_from_real_rows(project):
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services.ontology_store import OntologyStore
    from prism_service.models.task import CHANNELS

    summary = proj.rebuild(project)
    assert summary["classes"] >= 7

    store = OntologyStore(project)
    classes = {c["id"]: c for c in store.list_classes()}
    assert classes["Channel"]["source"] == "tasks"
    channel_labels = {i["label"] for i in store.list_instances("Channel", limit=50)}
    assert set(CHANNELS) <= channel_labels
    assert "ui" in channel_labels and "mcp" in channel_labels

    # Re-anchored by task 785bb4ce ("a signal is resolved against the
    # ontology on arrival"): QueueItem now projects from SIGNALS, not tasks
    # -- the Queue is where signals arrive. Task keeps the old tasks-based
    # projection as its own class (test_signal_resolves_against_ontology.py
    # pins the fuller signals-vs-tasks contract).
    assert classes["QueueItem"]["source"] == "signals"
    qi_instances = store.list_instances("QueueItem", limit=50)
    assert {i["label"] for i in qi_instances} == {
        "first signal · open", "second signal · open",
    }

    assert classes["Task"]["source"] == "tasks"
    task_instances = store.list_instances("Task", limit=50)
    assert {i["label"] for i in task_instances} == {
        "ui task", "mcp task", "legacy task",
    }

    doc_labels = {i["label"] for i in store.list_instances("Document", limit=50)}
    assert doc_labels == {"services/foo.py", "services/bar.py"}

    code_classes = [c for c in classes.values() if c["source"] == "graph"]
    assert any(c["name"].lower() == "function" and c["instance_count"] == 2
               for c in code_classes)
    assert any(c["name"].lower() == "class" and c["instance_count"] == 1
               for c in code_classes)

    props = {p["name"] for p in store.list_properties()}
    assert {"calls", "imports"} <= props

    axioms = {a["name"]: a for a in store.list_axioms()}
    # Superseded by task c1d0ee70: the four PROTOTYPE_AXIOMS are now
    # EVALUATED against real rows, not seeded quiet, so this fixture's own
    # blank-channel "legacy task" and catalog entries with no "when"
    # phrasing legitimately read as violated. Only the arc_governance
    # PRINCIPLE-name axioms (pre-c1d0ee70, still seeded quiet by
    # construction) are asserted quiet here.
    assert len(axioms) >= 5
    principle_axioms = [a for n, a in axioms.items() if n.startswith("ARC-")]
    assert principle_axioms
    assert all(a["state"] == "quiet" for a in principle_axioms)
    assert axioms["task-names-its-channel"]["state"] == "violated"
    store.close()


# ---------------------------------------------------------------------------
# AC-3: GET /api/okf/ontology* serves the persisted rows; rebuild re-runs it
#
# Re-anchored by task 495d3a69 ("the ontology is an RDF graph you can query
# with SPARQL"): api/okf.py's ontology routes now answer from
# services/ontology_graph.OntologyGraph (a pyoxigraph SPARQL store), not
# OntologyStore's sqlite cache — so class_id is "Task" (the TBox class real
# task rows are typed with, per model.ttl) rather than the old flat
# "QueueItem" catalog id, and POST /rebuild returns the graph's own
# triple-counts-per-class shape, not the old {classes,instances,...} counts.
# ---------------------------------------------------------------------------

def test_api_serves_persisted_rows_and_rebuilds(project):
    from prism_service.api import okf

    okf._HOSTS.clear()
    out = okf.ontology(project=project)  # empty graph -> auto-runs once (AC-5)
    assert out["classes"]
    assert any(c["id"] == "Channel" for c in out["classes"])
    assert out["properties"]
    assert out["axioms"]

    # Re-anchored by task 785bb4ce: QueueItem instances come from the 2
    # seeded signals now, not the 3 seeded tasks (see the `project` fixture);
    # Task is its own class (task 495d3a69's graph read path).
    inst = okf.ontology_instances(project=project, class_id="QueueItem", limit=10)
    assert len(inst["instances"]) == 2
    inst = okf.ontology_instances(project=project, class_id="Task", limit=10)
    assert len(inst["instances"]) == 3

    summary = okf.ontology_rebuild(project=project)
    assert summary["total_triples"] > 0
    assert len(summary["per_class"]) >= 7


# ---------------------------------------------------------------------------
# AC-4: OntologyPage renders an Ontology entry point with Subsume's
# shape-carries-kind primitives, on additive tokens, PRISM fonts untouched.
# Re-anchored from UnderstandPage to OntologyPage (task eca23a10).
# ---------------------------------------------------------------------------

def test_ontology_page_renders_ontology_entry_point():
    page = _read(_ONTOLOGY_PAGE)
    assert "OntologyPanel" in page
    assert '"Ontology"' in page or "'Ontology'" in page


def test_ontology_panel_carries_shape_by_kind_primitives():
    panel = _read(_SRC / "components" / "ontology" / "OntologyPanel.tsx")
    assert 'data-kind="class"' in panel
    assert 'data-kind="instance"' in panel
    assert 'data-kind="property"' in panel
    assert 'data-kind="abstract"' in panel
    assert "more" in panel.lower()  # "+N more" rollup


def test_ontology_tokens_are_additive_and_prism_fonts_are_untouched():
    css = _read(_INDEX_CSS)
    assert "--social:" in css
    assert "--formal:" in css
    assert "--alarm:" in css
    # Byte-identical PRISM font stack — the whole point of "additive only".
    assert ("  --font-sans: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', "
            "Roboto, 'Helvetica Neue', Arial, sans-serif;") in css
    assert ("  --font-mono: ui-monospace, 'Cascadia Code', 'Segoe UI Mono', "
            "'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace;") in css
