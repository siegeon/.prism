"""The ontology API serves structure, records, terms and titled rules
(task 7dbb242f, epic e39027d3, owner: "not quite there with ontology").

Four tabs, all read from OntologyGraph (SPARQL) + ontology_rules (the
persisted SHACL report) — nothing computed from sqlite. Seeds a throwaway
project the same way test_ontology_is_an_rdf_graph.py does, plus signals
(SignalStore) and one task carrying an UNDECLARED status, to prove
held_back is real data, not a fabricated example.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def project():
    from prism_service.project_context import get_project

    pid = f"ontology-tabs-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    ctx.task_svc.create(title="ui task", channel="ui")
    mcp_task = ctx.task_svc.create(title="mcp task", channel="mcp")
    # Undeclared status (not in pending/in_progress/blocked/done/cancelled)
    # — proves terms().held_back reports REAL data, never a fabricated row.
    # "urgent" is genuinely outside models.task.STATUSES; "archived" (the
    # original seed) became a DECLARED status when task f5352fa1 aligned the
    # vocabulary to what conductor_service.py really uses (30 deleted + 2
    # archived live rows), so it no longer counts as held back.
    ctx.task_svc.update(mcp_task.id, status="urgent")
    ctx.task_svc.create(title="legacy task")  # blank channel

    from prism_service.config import project_data_dir

    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1','services/foo.py')")
    conn.execute("INSERT INTO docs VALUES ('d2','services/bar.py')")
    for i in range(20):
        conn.execute(
            "INSERT INTO docs VALUES (?, ?)",
            (f"pad{i}", f"padding/folder{i % 5}/file{i}.py"),
        )
    conn.commit()
    conn.close()

    graph_db = project_data_dir(pid) / "graph.db"
    conn = sqlite3.connect(str(graph_db))
    conn.execute("CREATE TABLE entities (id INTEGER PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE relationships (relation TEXT)")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('foo', 'function')")
    conn.execute("INSERT INTO entities (name, kind) VALUES ('Bar', 'class')")
    conn.execute("INSERT INTO relationships VALUES ('calls')")
    conn.commit()
    conn.close()

    from prism_service.models.signal import Signal
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(pid)
    store.create(Signal(project=pid, channel="slack", subject="please review this PR",
                         state="open"))
    store.create(Signal(project=pid, channel="mail", subject="fyi heads up",
                         state="dropped"))
    store.close()

    from prism_service.services import ontology_prototype_projection as proj

    proj.rebuild(pid)
    return pid


# ---------------------------------------------------------------------------
# Structure: pre-order taxonomy, parent/depth, rolled-up counts, a comment
# on every class, and relations with count + a real example.
# ---------------------------------------------------------------------------

def test_structure_preorder_rollup_comments_and_relations(project):
    from prism_service.api import okf

    out = okf.ontology_structure(project=project)
    classes = out["classes"]
    assert classes, "structure() returned no classes"
    assert classes[0]["id"] == "Workspace"
    assert classes[0]["depth"] == 0
    assert classes[0]["parent"] is None

    by_id = {c["id"]: c for c in classes}
    seen: set[str] = set()
    for c in classes:
        if c["parent"] is not None:
            assert c["parent"] in seen, f"{c['id']} appears before its parent {c['parent']}"
            assert c["depth"] == by_id[c["parent"]]["depth"] + 1
        seen.add(c["id"])
        assert c["comment"], f"o:{c['id']} has no comment"
        assert c["count"] >= c["own_count"] >= 0

    assert by_id["Activity"]["count"] >= by_id["Task"]["count"]
    assert by_id["Task"]["own_count"] >= 2  # ui + mcp tasks (legacy has no channel edge)

    relations = out["relations"]
    assert relations
    with_count = [r for r in relations if r["count"] > 0]
    assert with_count, "no relation has any real edges"
    real_example = next(r for r in with_count if r["example"] is not None)
    assert real_example["example"]["from_label"]
    assert real_example["example"]["to_label"]
    # sorted by count desc
    counts = [r["count"] for r in relations]
    assert counts == sorted(counts, reverse=True)

    assert out["built_from"]["tasks"] == 3
    assert out["validated_at"]


# ---------------------------------------------------------------------------
# Records: totals are internally consistent, samples are real labels.
# ---------------------------------------------------------------------------

def test_records_totals_consistent_and_samples_real(project):
    from prism_service.api import okf

    out = okf.ontology_records(project=project)
    assert out["things"] > 0
    assert out["values"] > 0
    assert out["classes"]

    assert sum(c["count"] for c in out["classes"]) == out["things"]
    counts = [c["count"] for c in out["classes"]]
    assert counts == sorted(counts, reverse=True)

    task_class = next(c for c in out["classes"] if c["id"] == "Task")
    assert task_class["count"] == 3
    assert set(task_class["sample"]) == {"ui task", "mcp task", "legacy task"}
    for c in out["classes"]:
        assert len(c["sample"]) <= 6
        assert all(isinstance(s, str) and s for s in c["sample"])


# ---------------------------------------------------------------------------
# Terms: the seven real vocabularies, in_use/count, and a held-back value
# for the task carrying an undeclared status.
# ---------------------------------------------------------------------------

def test_terms_seven_vocabularies_and_held_back_status(project):
    from prism_service.api import okf

    out = okf.ontology_terms(project=project)
    names = {v["name"] for v in out["vocabularies"]}
    assert names == {"channel", "signal_state", "task_status", "workflow",
                      "proof_type", "ask", "gate_state"}

    for v in out["vocabularies"]:
        assert v["comment"]
        for term in v["terms"]:
            assert term["count"] >= 0
            assert term["in_use"] == (term["count"] > 0)

    channel_vocab = next(v for v in out["vocabularies"] if v["name"] == "channel")
    ui_term = next(t for t in channel_vocab["terms"] if t["value"] == "ui")
    assert ui_term["in_use"] and ui_term["count"] >= 1

    held_back = out["held_back"]
    assert any(h["vocabulary"] == "task_status" and h["value"] == "urgent"
               for h in held_back), held_back
    # And the aligned vocabulary declares the statuses the product really
    # uses, so they are in the term list, never held back.
    status_vocab = next(v for v in out["vocabularies"] if v["name"] == "task_status")
    assert {"deleted", "archived"} <= {t["value"] for t in status_vocab["terms"]}


# ---------------------------------------------------------------------------
# Rules: title/description/focus labels, need_decision == violated count.
# ---------------------------------------------------------------------------

def test_rules_carry_titles_and_need_decision_matches_violations(project):
    from prism_service.api import okf

    out = okf.ontology_rules(project=project)
    assert out["total"] == len(out["rules"])
    assert out["validated_at"]

    violated = [r for r in out["rules"] for f in [r["violations"]] if f > 0]
    assert out["need_decision"] == len(violated)
    assert out["need_decision"] > 0  # legacy task has no channel -> a real violation

    for r in out["rules"]:
        assert r["title"], f"{r['name']} has no title"
        assert r["description"], f"{r['name']} has no description"
        for f in r["focus"]:
            assert set(f.keys()) == {"iri", "label"}
            assert f["label"]

    channel_rule = next(r for r in out["rules"] if r["name"] == "task-names-its-channel")
    assert channel_rule["title"] == "Every task names its channel"
    assert channel_rule["violations"] >= 1
    assert any(f["label"] == "legacy task" for f in channel_rule["focus"])
