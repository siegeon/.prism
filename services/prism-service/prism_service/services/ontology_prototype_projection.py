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


def _queue_item_instances(project: str) -> list[tuple[str, str, str]]:
    """QueueItem <- SIGNALS (task 785bb4ce, owner: the Queue is where
    signals arrive, not tasks): (id, label, state) triples, one instance
    per signal. Task moved to its own class below (`_task_instances`) so
    nothing that used to live under QueueItem is lost."""
    from prism_service.services.signal_store import SignalStore

    store = SignalStore(project)
    try:
        signals = store.list(limit=2000)
    finally:
        store.close()
    return [(s.id, s.subject or s.channel or s.id, s.state) for s in signals]


def _task_instances(project: str) -> list[tuple[str, str]]:
    """Task <- tasks: (id, title) pairs -- its own class (task 785bb4ce),
    split out of QueueItem so real task rows are still projected."""
    ctx = get_project(project)
    return [(t.id, t.title) for t in ctx.task_svc.list()]


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


def _axiom_names(project: str) -> list[str]:
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
    """Rebuild the ontology tables from real PRISM rows -- persisted,
    never computed at request time. Returns row counts for the caller
    (the /api/okf/ontology/rebuild route) to report back."""
    classes: list[dict[str, Any]] = []
    instances: list[dict[str, Any]] = []
    properties: list[dict[str, Any]] = []
    axioms: list[dict[str, Any]] = []

    _add_class(classes, instances, "Channel", "class", "tasks",
               _channel_instances(project))
    _add_class(classes, instances, "Agent", "class", "workflows",
               _agent_instances(project))
    _add_class(classes, instances, "Provider", "class", "integrations",
               _provider_instances())

    qi = _queue_item_instances(project)
    # ontology_instances has no `state` column yet (task 785bb4ce) -- state
    # is carried inline in the label until the sibling RDF-graph epic lands
    # a real property for it.
    _add_class(classes, instances, "QueueItem", "class", "signals",
               [f"{label} · {state}" for _, label, state in qi],
               refs=[sid for sid, _, _ in qi])

    task_rows = _task_instances(project)
    _add_class(classes, instances, "Task", "class", "tasks",
               [title for _, title in task_rows],
               refs=[tid for tid, _ in task_rows])

    docs = _document_paths(project)
    _add_class(classes, instances, "Document", "class", "brain", docs)
    folders = sorted({str(Path(d).parent) for d in docs if d})
    _add_class(classes, instances, "Folder", "class", "brain", folders)

    for kind, count, sample in _code_graph_kinds(project):
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

    for name in _axiom_names(project):
        axioms.append({
            "id": f"axiom::{name}", "name": name,
            "description": "", "state": "quiet", "detail": "",
        })

    # Prototype rule axioms (c1d0ee70): EVALUATED, not seeded quiet — a
    # real violation lights the Understand view's --alarm state.
    from prism_service.services.arc_governance import evaluate_axioms

    context = {
        "tasks": _task_rows(project),
        "document_paths": docs,
        "catalog_entries": _catalog_entries(project),
    }
    for axiom in evaluate_axioms(context):
        axioms.append({
            "id": f"axiom::{axiom['name']}", "name": axiom["name"],
            "description": axiom["description"], "state": axiom["state"],
            "detail": axiom["detail"],
        })

    store = OntologyStore(project)
    store.replace_all(classes, instances, properties, axioms)
    store.close()

    return {
        "classes": len(classes), "instances": len(instances),
        "properties": len(properties), "axioms": len(axioms),
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
