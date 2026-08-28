"""ontology_memory_projection — memory entries as ontology rows (task
f5352fa1, epic 3a652b3b, owner: "we need to make sure that the ontology is
respected throughout the system, understanding and such").

Owns the memory-specific gathering ontology_prototype_projection.gather()
delegates to for its 'memories' key: the SAME rows the Understand read
panel and okf_host's concept graph already show — memory_svc.list_domains()
/list_entries() for id/name/type/domain/classification/evidence, and
OkfHost's own resolved concept graph for cross-links (a [[wikilink]] that
okf_host could actually match to a real concept id; a dangling link stays
absent here too, same as it does in the rendered body).

services.ontology_graph.OntologyGraph._emit_memories is the ONLY consumer
of this shape — see its docstring for what each key becomes as RDF.
"""

from __future__ import annotations

from prism_service.project_context import get_project


def memory_rows(project: str) -> list[dict]:
    """One row per active memory entry: id/name/type/domain/classification/
    cites/evidence_task/evidence_files — real fields, never fabricated.
    `cites` is the entry's outbound [[wikilink]] targets already resolved
    to real memory-entry ids by okf_host's own concept graph (the exact
    edges the Understand page's cross-links render from)."""
    ctx = get_project(project)
    memory_svc = ctx.memory_svc

    from prism_service.services.okf_host import OkfHost

    host = OkfHost(memory_svc)
    cites_by_id: dict[str, list[str]] = {}
    for edge in host.graph()["edges"]:
        cites_by_id.setdefault(edge["source"], []).append(edge["target"])

    rows: list[dict] = []
    for domain in memory_svc.list_domains():
        for entry in memory_svc.list_entries(domain):  # active only, default
            evidence = entry.evidence if isinstance(entry.evidence, dict) else {}
            rows.append({
                "id": entry.id,
                "name": entry.name or entry.id,
                # The body travels too (epic b2acfa16): Understand is under
                # the ontology, so its rules must see what a memory SAYS,
                # not only what it is called.
                "description": entry.description or "",
                "type": entry.type or "",
                "domain": domain,
                "classification": entry.classification or "",
                "cites": cites_by_id.get(entry.id, []),
                "evidence_task": (_evidence_tasks(evidence) or [""])[0],
                "evidence_tasks": _evidence_tasks(evidence),
                "evidence_files": _evidence_files(evidence),
            })
    return rows


def _as_list(value) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v).strip()]
    return []


def _evidence_tasks(evidence: dict) -> list[str]:
    """Every task id an evidence dict names, in every shape PRISM writes
    (task 6e858c89): "task" (one id), "task_id", "tasks" (a list). Measured
    on the prism project 2026-08-27: 74 of 82 decision-has-evidence
    violations were memories whose evidence sat in a shape the projection
    did not read."""
    out: list[str] = []
    for key in ("task", "task_id", "tasks"):
        for v in _as_list(evidence.get(key)):
            if v not in out:
                out.append(v)
    return out


def _evidence_files(evidence: dict) -> list[str]:
    """Every document path an evidence dict names: "file_paths", "files",
    and "source_file" (one path; 65 live decisions carry only that)."""
    out: list[str] = []
    for key in ("file_paths", "files", "source_file"):
        for v in _as_list(evidence.get(key)):
            if v not in out:
                out.append(v)
    return out
