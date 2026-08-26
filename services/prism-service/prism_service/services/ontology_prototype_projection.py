"""Prototype ontology classes projection (task 15c06516, owner top priority:
"i dont see the ontology stuff").

Populates OntologyStore from REAL PRISM rows -- the mx-2d14b0 mapping:
graph entity kinds -> classes, graph rows -> instances, graph edge kinds ->
properties, arc_governance principle names -> axioms. Walking skeleton
(owner 2026-08-25). Axiom VIOLATION detection (task c1d0ee70) is now
wired: arc_governance principle names stay quiet axioms by construction,
and PROTOTYPE_AXIOMS (arc_governance.evaluate_axioms) are EVALUATED
against real task/document/catalog rows each rebuild, so a real violation
persists as state='violated' with the offending row named in `detail`.

Task 495d3a69 ("the ontology is an RDF graph you can query with SPARQL"):
gather() is now the ONE row-gathering pass, feeding BOTH the sqlite cache
below (OntologyStore -- kept as a thin best-effort cache; unchanged
behaviour, since tests/unit/test_prototype_axioms.py reads it directly
and sits outside that task's allowed_files) and services/ontology_graph.
OntologyGraph's RDF representation, which api/okf.py actually reads from
now. See ontology_graph.py's module docstring for the real thing this
was pointing at (rdflib/pyoxigraph/SPARQL — the Subsume prototype's own
stack, per ontology-SKILL.md).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from prism_service.config import project_data_dir
from prism_service.models.task import CHANNELS
from prism_service.project_context import get_project
from prism_service.services.ontology_store import OntologyStore
from prism_service.services import sqlite_db

_EXTRA_PROVIDERS = ("claude",)  # always-on, outside integrations_connect.PROVIDERS


def _connect(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    # sqlite chokepoint (row_factory=Row, timeout, WAL) — never bare connect.
    return sqlite_db.connect(path)


def _channel_instances(project: str) -> list[str]:
    """Channel <- models.task.CHANNELS + distinct task.channel in use."""
    ctx = get_project(project)
    used = {t.channel for t in ctx.task_svc.list() if t.channel}
    return sorted(set(CHANNELS) | used)


def _agent_instances(project: str) -> list[str]:
    """Agent <- the /api/workflows catalog ids, called directly (not HTTP).

    Half of that builder (validation, conductor behaviors) reaches out to the
    AosWorkflows engine over the network and raises when it isn't running --
    so on ANY failure this falls back to the network-free half of the SAME
    source of truth: WORKFLOW_STEPS, which the catalog's own 'steps' field is
    built from (api/workflows.py get_workflows), never fabricated data.
    """
    try:
        from prism_service.api.workflows import get_workflows
        catalog = get_workflows(project=project)
        ids = [w["id"] for w in catalog.get("workflows", []) if w.get("id")]
        if ids:
            return ids
    except Exception:
        pass
    from prism_service.models.workflow import WORKFLOW_STEPS
    return sorted({s["id"] for s in WORKFLOW_STEPS})


def _provider_instances() -> list[str]:
    """Provider <- connector cards (api.integrations_connect.PROVIDERS) plus
    'claude', the always-on provider that module excludes by name."""
    from prism_service.api.integrations_connect import PROVIDERS
    return list(PROVIDERS) + list(_EXTRA_PROVIDERS)


def _document_paths(project: str) -> list[str]:
    """Document <- brain docs source_file paths (docs table, brain.db)."""
    conn = _connect(project_data_dir(project) / "brain.db")
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_file FROM docs WHERE source_file IS NOT NULL"
        ).fetchall()
        return sorted({r[0] for r in rows if r[0]})
    finally:
        conn.close()


def _task_rows(project: str) -> list[dict]:
    """Task rows (id/title/channel) for the task-names-its-channel axiom
    (c1d0ee70) — real rows, never fabricated."""
    ctx = get_project(project)
    return [{"id": t.id, "title": t.title, "channel": t.channel}
            for t in ctx.task_svc.list()]


def _catalog_entries(project: str) -> list[dict]:
    """Workflow/behavior catalog entries -> {id, description} for the
    skill-description-says-when axiom (c1d0ee70). Same real-data-first,
    network-free-fallback shape as _agent_instances above: the live
    /api/workflows catalog's own entries carry a real 'description'; if
    that call fails (no AosWorkflows engine reachable), fall back to
    WORKFLOW_STEPS' own STEP_ACTIONS action text — still real doctrine,
    never fabricated for this axiom."""
    try:
        from prism_service.api.workflows import get_workflows
        catalog = get_workflows(project=project)
        entries = [{"id": w["id"], "description": w.get("description", "")}
                   for w in catalog.get("workflows", []) if w.get("id")]
        if entries:
            return entries
    except Exception:
        pass
    from prism_service.api.workflows import STEP_ACTIONS
    from prism_service.models.workflow import WORKFLOW_STEPS
    return [{"id": s["id"], "description": STEP_ACTIONS.get(s["id"], ("", "", ""))[1]}
            for s in WORKFLOW_STEPS]


def axiom_names(project: str) -> list[str]:
    """arc_governance principle NAMES as quiet axioms (mx-2d14b0). Reads the
    project's own seeded principles (memory domain 'architecture-principles')
    first; PRISM_PRINCIPLES (real source data, not fabricated for a fresh
    project) is the fallback so an unseeded project still gets real rows."""
    from prism_service.services.arc_governance import PRINCIPLES_DOMAIN, PRISM_PRINCIPLES

    ctx = get_project(project)
    try:
        entries = ctx.memory_svc.list_entries(PRINCIPLES_DOMAIN)
    except Exception:
        entries = []
    names: list[str] = []
    for e in entries:
        principle = (e.evidence or {}).get("principle") if isinstance(e.evidence, dict) else None
        names.append((principle or {}).get("id") or e.name)
    return names or [p["id"] for p in PRISM_PRINCIPLES]


def _code_graph_kinds(project: str) -> list[tuple[str, int, list[str]]]:
    """Code-graph entity KINDS as classes (mx-2d14b0): (kind, real total
    count, [capped sample entity names]) from graph.db's entities table --
    module/class/function/... The UI's rail click gets a bounded sample; the
    class's instance_count is the true COUNT(*), never len(sample)."""
    conn = _connect(project_data_dir(project) / "graph.db")
    if conn is None:
        return []
    try:
        kinds = conn.execute(
            "SELECT kind, COUNT(*) n FROM entities "
            "WHERE kind IS NOT NULL AND kind != '' GROUP BY kind"
        ).fetchall()
        out = []
        for kind, n in kinds:
            names = conn.execute(
                "SELECT name FROM entities WHERE kind=? ORDER BY name LIMIT 50",
                (kind,),
            ).fetchall()
            out.append((kind, n, [x[0] for x in names if x[0]]))
        return out
    finally:
        conn.close()


def axiom_context(project: str) -> dict:
    """The real-row context arc_governance.evaluate_axioms(context) needs —
    shared by rebuild()'s sqlite cache write and OntologyGraph.axioms()
    (task 495d3a69) so both read the SAME rows, never two computations
    that can drift."""
    return {
        "tasks": _task_rows(project),
        "document_paths": _document_paths(project),
        "catalog_entries": _catalog_entries(project),
    }


def gather(project: str) -> dict:
    """The real rows for BOTH representations of the ontology (task
    495d3a69): rebuild()'s sqlite cache below, and OntologyGraph.rebuild()'s
    RDF triples. One gather pass, two projections — never two independent
    reads of the same underlying rows that can silently disagree."""
    return {
        "channels": _channel_instances(project),
        "agents": _agent_instances(project),
        "providers": _provider_instances(),
        "tasks": _task_rows(project),
        "documents": _document_paths(project),
        "code_kinds": _code_graph_kinds(project),
    }


def _add_class(
    classes: list[dict[str, Any]], instances: list[dict[str, Any]],
    cid: str, kind: str, source: str, members: list[str],
    refs: list[str] | None = None,
) -> None:
    classes.append({
        "id": cid, "name": cid, "kind": kind, "source": source,
        "instance_count": len(members),
    })
    for i, label in enumerate(members):
        ref = refs[i] if refs else label
        instances.append({
            "id": f"{cid}::{i}", "class_id": cid, "label": str(label),
            "ref": str(ref), "provenance": source,
        })


def rebuild(project: str) -> dict:
    """Rebuild the ontology -- persisted, never computed at request time.
    ONE gather() pass feeds both representations (task 495d3a69): the
    sqlite cache below (ontology_store.py -- kept as a thin best-effort
    cache; tests/unit/test_prototype_axioms.py reads it directly and sits
    outside this task's allowed_files) AND the RDF graph
    (services/ontology_graph.OntologyGraph), which is api/okf.py's actual
    READ PATH now. Returns row counts (sqlite) plus the graph's own
    triple-counts-per-class under "graph"."""
    rows = gather(project)
    classes: list[dict[str, Any]] = []
    instances: list[dict[str, Any]] = []
    properties: list[dict[str, Any]] = []
    axioms: list[dict[str, Any]] = []

    _add_class(classes, instances, "Channel", "class", "tasks", rows["channels"])
    _add_class(classes, instances, "Agent", "class", "workflows", rows["agents"])
    _add_class(classes, instances, "Provider", "class", "integrations",
               rows["providers"])

    qi = [(t["id"], t["title"]) for t in rows["tasks"]]
    _add_class(classes, instances, "QueueItem", "class", "tasks",
               [title for _, title in qi], refs=[tid for tid, _ in qi])

    docs = rows["documents"]
    _add_class(classes, instances, "Document", "class", "brain", docs)
    folders = sorted({str(Path(d).parent) for d in docs if d})
    _add_class(classes, instances, "Folder", "class", "brain", folders)

    for kind, count, sample in rows["code_kinds"]:
        cid = f"CodeGraph::{kind}"
        classes.append({
            "id": cid, "name": kind.capitalize(), "kind": "class",
            "source": "graph", "instance_count": count,
        })
        for i, label in enumerate(sample):
            instances.append({
                "id": f"{cid}::{i}", "class_id": cid, "label": label,
                "ref": label, "provenance": "graph",
            })

    for name in _edge_kinds(project):
        properties.append({
            "id": f"edge::{name}", "name": name,
            "domain_class": "CodeGraph", "range_class": "CodeGraph",
            "kind": "property",
        })

    for name in axiom_names(project):
        axioms.append({
            "id": f"axiom::{name}", "name": name,
            "description": "", "state": "quiet", "detail": "",
        })

    # Prototype rule axioms (c1d0ee70): EVALUATED, not seeded quiet — a
    # real violation lights the Understand view's --alarm state.
    from prism_service.services.arc_governance import evaluate_axioms

    for axiom in evaluate_axioms(axiom_context(project)):
        axioms.append({
            "id": f"axiom::{axiom['name']}", "name": axiom["name"],
            "description": axiom["description"], "state": axiom["state"],
            "detail": axiom["detail"],
        })

    store = OntologyStore(project)
    store.replace_all(classes, instances, properties, axioms)
    store.close()

    # The RDF representation (task 495d3a69) — SAME gathered rows, so the
    # sqlite cache above and the graph can never silently disagree.
    from prism_service.services.ontology_graph import OntologyGraph

    graph_result = OntologyGraph(project).rebuild(rows=rows)

    return {
        "classes": len(classes), "instances": len(instances),
        "properties": len(properties), "axioms": len(axioms),
        "graph": graph_result,
    }


def _edge_kinds(project: str) -> list[str]:
    """Existing graph EDGE kinds as properties (mx-2d14b0): relationships.relation."""
    conn = _connect(project_data_dir(project) / "graph.db")
    if conn is None:
        return []
    try:
        rows = conn.execute(
            "SELECT DISTINCT relation FROM relationships "
            "WHERE relation IS NOT NULL AND relation != ''"
        ).fetchall()
        return sorted({r[0] for r in rows if r[0]})
    finally:
        conn.close()
