"""Prototype rules become axioms (task c1d0ee70).

"A rule that cannot fail is decoration" — four evaluable axioms, each with a
real violation case in PRISM data: task-names-its-channel, no-artifacts-in-
the-root, dated-folder-uses-one-format, skill-description-says-when.

RE-ANCHORED at task 8eeb3e65 "the rules are SHACL shapes that can fail":
these four are now SHACL shapes over the o: RDF model (prism_service/
ontology/shapes.ttl, services/ontology_rules.py) — GET /api/okf/ontology's
axioms() no longer reads arc_governance.evaluate_axioms directly (see that
module's own comment on the section). The two PURE behavioural tests below
now drive ontology_rules.evaluate() against a hand-built ABox instead of
calling evaluate_axioms(context) directly. arc_governance.PROTOTYPE_AXIOMS/
evaluate_axioms are UNCHANGED and still exercised by the sqlite-cache tests
further down (ontology_prototype_projection.rebuild's OntologyStore cache
sits outside 8eeb3e65's allowed_files).
"""

from __future__ import annotations

import uuid

import pytest

COMPLIANT_ROWS = {
    "channels": ["ui"], "agents": ["c1"], "providers": [],
    "tasks": [{"id": "t1", "title": "channelled task", "channel": "ui"}],
    "signals": [],
    "documents": [
        "engineering/weekly-reports/2026-08-18/report.md",
        "engineering/weekly-reports/2026-08-25/report.md",
    ],
    "code_kinds": [],
}
COMPLIANT_DESCRIPTIONS = {"c1": "Use when the user asks to review a PR."}

VIOLATION_ROWS = {
    "channels": [], "agents": ["skill-x"], "providers": [],
    "tasks": [{"id": "t1", "title": "legacy task", "channel": ""}],
    "signals": [],
    "documents": [
        "README.md",
        "engineering/weekly-reports/2026-Q1/report.md",
        "engineering/weekly-reports/2026-08-18/report.md",
    ],
    "code_kinds": [],
}
VIOLATION_DESCRIPTIONS = {"skill-x": "Reviews PRs and leaves comments."}


def _rebuild_and_evaluate(rows: dict, agent_descriptions: dict) -> dict:
    from prism_service.services import ontology_rules
    from prism_service.services.ontology_graph import OntologyGraph

    pid = f"shacl-axioms-{uuid.uuid4().hex[:8]}"
    OntologyGraph(pid).rebuild(rows=rows, agent_descriptions=agent_descriptions,
                                signal_arrived_at={})
    return {a["name"]: a for a in ontology_rules.evaluate(pid)}


# ---------------------------------------------------------------------------
# ontology_rules.evaluate is quiet on compliant seed data
# ---------------------------------------------------------------------------

def test_axioms_quiet_on_compliant_seed_data():
    axioms = _rebuild_and_evaluate(COMPLIANT_ROWS, COMPLIANT_DESCRIPTIONS)
    for name in ("task-names-its-channel", "no-artifacts-in-the-root",
                 "dated-folder-uses-one-format", "skill-description-says-when"):
        assert axioms[name]["state"] == "quiet", (name, axioms[name])
        assert axioms[name]["detail"] == ""


# ---------------------------------------------------------------------------
# ontology_rules.evaluate names the offending row on violation seed data
# ---------------------------------------------------------------------------

def test_axioms_violated_on_violation_seed_data_names_the_row():
    axioms = _rebuild_and_evaluate(VIOLATION_ROWS, VIOLATION_DESCRIPTIONS)

    assert axioms["task-names-its-channel"]["state"] == "violated"
    assert "t1" in axioms["task-names-its-channel"]["detail"]

    assert axioms["no-artifacts-in-the-root"]["state"] == "violated"
    assert "README.md" in axioms["no-artifacts-in-the-root"]["detail"]

    assert axioms["dated-folder-uses-one-format"]["state"] == "violated"
    assert "2026-Q1" in axioms["dated-folder-uses-one-format"]["detail"]

    assert axioms["skill-description-says-when"]["state"] == "violated"
    assert "skill-x" in axioms["skill-description-says-when"]["detail"]


# ---------------------------------------------------------------------------
# The projection persists the EVALUATED state (real violation seed data)
# ---------------------------------------------------------------------------

@pytest.fixture
def axiom_project():
    """A throwaway project (see test_prototype_ontology_classes.py's
    'project' fixture pattern) seeded with a REAL violation for each of the
    four axioms: a blank-channel task, a document loose in the project
    root, a dated folder that breaks the YYYY-MM-DD format among compliant
    siblings; the skill rule is seeded with its own violating agent in
    the first test (every real catalog entry says when it runs since task
    408138e8)."""
    from prism_service.project_context import get_project

    pid = f"axiom-test-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    ctx.task_svc.create(title="legacy task")  # blank channel

    import sqlite3
    from prism_service.config import project_data_dir

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1','README.md')")
    conn.execute(
        "INSERT INTO docs VALUES "
        "('d2','engineering/weekly-reports/2026-Q1/report.md')")
    conn.execute(
        "INSERT INTO docs VALUES "
        "('d3','engineering/weekly-reports/2026-08-18/report.md')")
    conn.commit()
    conn.close()

    return pid


def test_projection_persists_evaluated_axiom_states(axiom_project):
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services.ontology_store import OntologyStore

    proj.rebuild(axiom_project)
    store = OntologyStore(axiom_project)
    axioms = {a["name"]: a for a in store.list_axioms()}
    store.close()

    assert axioms["task-names-its-channel"]["state"] == "violated"
    assert "legacy task" in axioms["task-names-its-channel"]["detail"]

    assert axioms["no-artifacts-in-the-root"]["state"] == "violated"
    assert "README.md" in axioms["no-artifacts-in-the-root"]["detail"]

    assert axioms["dated-folder-uses-one-format"]["state"] == "violated"
    assert "2026-Q1" in axioms["dated-folder-uses-one-format"]["detail"]

    # quiet since task 408138e8: every real catalog / step description now
    # says when it runs; the rule still fires on the seeded skill-x agent
    # in test_axioms_violated_on_violation_seed_data_names_the_row.
    assert axioms["skill-description-says-when"]["state"] == "quiet"


# ---------------------------------------------------------------------------
# GET /api/okf/ontology serves the axiom rows (understand read path) — now
# sourced from ontology_rules.evaluate (SHACL), not arc_governance directly
# (task 8eeb3e65); the seeded task/document rows still trip the same rules.
# ---------------------------------------------------------------------------

def test_axiom_rows_served_on_get_ontology(axiom_project):
    from prism_service.api import okf

    okf._HOSTS.clear()
    out = okf.ontology(project=axiom_project)  # empty store -> auto-rebuilds
    axioms = {a["name"]: a for a in out["axioms"]}

    assert axioms["task-names-its-channel"]["state"] == "violated"
    assert axioms["no-artifacts-in-the-root"]["state"] == "violated"
    assert axioms["dated-folder-uses-one-format"]["state"] == "violated"
    # quiet since task 408138e8: every real catalog / step description now
    # says when it runs; the rule still fires on the seeded skill-x agent
    # in test_axioms_violated_on_violation_seed_data_names_the_row.
    assert axioms["skill-description-says-when"]["state"] == "quiet"
