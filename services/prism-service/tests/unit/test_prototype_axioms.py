"""Prototype rules become axioms (task c1d0ee70).

"A rule that cannot fail is decoration" — four evaluable axioms, each with a
real violation case in PRISM data: task-names-its-channel, no-artifacts-in-
the-root, dated-folder-uses-one-format, skill-description-says-when. Pure
data + pure evaluate_axioms(context) in arc_governance.py (unit-testable
without a live daemon); ontology_prototype_projection persists the
EVALUATED state so the Understand ontology view (OntologyPanel.tsx) lights
the violated ones (--alarm, index.css .ont-axiom[data-state="violated"]).
"""

from __future__ import annotations

import uuid

import pytest

COMPLIANT_CONTEXT = {
    "tasks": [{"id": "t1", "title": "channelled task", "channel": "ui"}],
    "document_paths": [
        "engineering/weekly-reports/2026-08-18/report.md",
        "engineering/weekly-reports/2026-08-25/report.md",
    ],
    "catalog_entries": [
        {"id": "c1", "description": "Use when the user asks to review a PR."},
    ],
}

VIOLATION_CONTEXT = {
    "tasks": [{"id": "t1", "title": "legacy task", "channel": ""}],
    "document_paths": [
        "README.md",
        "engineering/weekly-reports/2026-Q1/report.md",
        "engineering/weekly-reports/2026-08-18/report.md",
    ],
    "catalog_entries": [
        {"id": "skill-x", "description": "Reviews PRs and leaves comments."},
    ],
}


# ---------------------------------------------------------------------------
# evaluate_axioms is pure — quiet on compliant seed data
# ---------------------------------------------------------------------------

def test_axioms_quiet_on_compliant_seed_data():
    from prism_service.services.arc_governance import evaluate_axioms

    axioms = {a["name"]: a for a in evaluate_axioms(COMPLIANT_CONTEXT)}
    assert set(axioms) == {
        "task-names-its-channel", "no-artifacts-in-the-root",
        "dated-folder-uses-one-format", "skill-description-says-when",
    }
    for name, a in axioms.items():
        assert a["state"] == "quiet", (name, a)
        assert a["detail"] == ""


# ---------------------------------------------------------------------------
# evaluate_axioms names the offending row on violation seed data
# ---------------------------------------------------------------------------

def test_axioms_violated_on_violation_seed_data_names_the_row():
    from prism_service.services.arc_governance import evaluate_axioms

    axioms = {a["name"]: a for a in evaluate_axioms(VIOLATION_CONTEXT)}

    assert axioms["task-names-its-channel"]["state"] == "violated"
    assert "legacy task" in axioms["task-names-its-channel"]["detail"]

    assert axioms["no-artifacts-in-the-root"]["state"] == "violated"
    assert "README.md" in axioms["no-artifacts-in-the-root"]["detail"]

    assert axioms["dated-folder-uses-one-format"]["state"] == "violated"
    assert "engineering/weekly-reports/2026-Q1" in \
        axioms["dated-folder-uses-one-format"]["detail"]

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
    siblings, and no catalog entry describing WHEN to use it (true today
    of every real WORKFLOW_STEPS/workflows-catalog entry — see arc_governance
    module docstring)."""
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

    assert axioms["skill-description-says-when"]["state"] == "violated"


# ---------------------------------------------------------------------------
# GET /api/okf/ontology serves the axiom rows (understand read path)
# ---------------------------------------------------------------------------

def test_axiom_rows_served_on_get_ontology(axiom_project):
    from prism_service.api import okf

    okf._HOSTS.clear()
    out = okf.ontology(project=axiom_project)  # empty store -> auto-rebuilds
    axioms = {a["name"]: a for a in out["axioms"]}

    assert axioms["task-names-its-channel"]["state"] == "violated"
    assert axioms["no-artifacts-in-the-root"]["state"] == "violated"
    assert axioms["dated-folder-uses-one-format"]["state"] == "violated"
    assert axioms["skill-description-says-when"]["state"] == "violated"
