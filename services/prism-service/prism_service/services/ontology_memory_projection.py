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
                "type": entry.type or "",
                "domain": domain,
                "classification": entry.classification or "",
                "cites": cites_by_id.get(entry.id, []),
                "evidence_task": str(evidence.get("task") or ""),
                "evidence_files": list(evidence.get("file_paths") or []),
            })
    return rows
