"""OKF API — PRISM's stores projected as a live, read-only OKF wiki.

Thin read-through to services/okf_host.OkfHost (which only READS the memory +
brain stores; never writes brain.db / graph.db). One host is cached per project
so the bundle isn't rebuilt on every request — OkfHost itself invalidates on a
cheap content signature, so the cache stays correct across writes elsewhere.
"""

import time

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from prism_service.project_context import get_project
from prism_service.services import ontology_prototype_projection
from prism_service.services.okf_host import OkfHost
from prism_service.services.ontology_graph import OntologyGraph

router = APIRouter()

_HOSTS: dict[str, OkfHost] = {}


def _host(project: str) -> OkfHost:
    host = _HOSTS.get(project)
    if host is None:
        try:
            p = get_project(project)
        except Exception as exc:
            raise HTTPException(404, f"unknown project: {project}: {exc}")
        host = OkfHost(p.memory_svc, p.brain_svc)
        _HOSTS[project] = host
    return host


@router.get("/index")
def index(project: str = Query("default")) -> dict:
    """OKF manifest: version, sections, concept count, index.md, paths."""
    return _host(project).index()


@router.get("/graph")
def graph(project: str = Query("default")) -> dict:
    """Concept graph (nodes colored by type + directed cross-link edges)."""
    return _host(project).graph()


@router.get("/concept")
def concept(project: str = Query("default"), path: str = Query(...)) -> dict:
    """One projected OKF concept (frontmatter + body + links + recalled_by)."""
    c = _host(project).get(path)
    if c is None:
        raise HTTPException(404, f"unknown concept: {path}")
    return c


@router.get("/task_concepts")
def task_concepts(project: str = Query("default"), task_id: str = Query(...)) -> dict:
    """Concepts a task recalled — the Task detail 'Knowledge · Understand' rail.

    Sourced from the memory recall_log (task_id -> entry_id), resolved to live
    concepts. Empty list when the task recalled no surviving concept."""
    return {"concepts": _host(project).task_concepts(task_id)}


@router.get("/raw/{path:path}", response_class=PlainTextResponse)
def raw(path: str, project: str = Query("default")) -> PlainTextResponse:
    """Conformant OKF markdown for a path ('index.md' serves the root index)."""
    text = _host(project).raw("/" + path)
    if text is None:
        raise HTTPException(404, f"unknown path: {path}")
    return PlainTextResponse(text, media_type="text/markdown")


# ---------------------------------------------------------------------------
# Ontology (task 15c06516, RDF graph task 495d3a69) — the ontology is a
# pyoxigraph RDF store (services/ontology_graph.OntologyGraph), queried via
# SPARQL. Never computed on this read path; the projection (services/
# ontology_prototype_projection.rebuild) is the only writer, and it also
# keeps ontology_store.py's sqlite table warm as a thin cache (read
# directly by tests/unit/test_prototype_axioms.py, outside this task's
# allowed_files) — but THIS module answers from the graph, not sqlite.
# Auto-runs ONCE on an empty graph so /understand shows real data without
# a manual rebuild click.
# ---------------------------------------------------------------------------

@router.get("/ontology")
def ontology(project: str = Query("default")) -> dict:
    graph = OntologyGraph(project)
    if graph.is_empty():
        ontology_prototype_projection.rebuild(project)
    return {
        "classes": graph.classes(),
        "properties": graph.properties(),
        "axioms": graph.axioms(),
    }


@router.get("/ontology/instances")
def ontology_instances(
    project: str = Query("default"), class_id: str = Query(...),
    limit: int = Query(200),
) -> dict:
    return {"instances": OntologyGraph(project).instances(class_id, limit=limit)}


@router.post("/ontology/rebuild")
def ontology_rebuild(project: str = Query("default")) -> dict:
    return ontology_prototype_projection.rebuild(project)["graph"]


@router.get("/ontology/rules")
def ontology_rules(project: str = Query("default")) -> dict:
    """The rules are SHACL shapes that can fail (task 8eeb3e65) — the full
    persisted validation report, per rule: focus nodes capped at 20."""
    from prism_service.services import ontology_rules as rules_svc

    return rules_svc.full_report(project)


@router.post("/ontology/sparql")
def ontology_sparql(payload: dict, project: str = Query("default")) -> dict:
    """SELECT/ASK only, bounded LIMIT, real rows from the live graph."""
    query = payload.get("query") or ""
    limit = int(payload.get("limit") or 500)
    start = time.perf_counter()
    try:
        result = OntologyGraph(project).query(query, limit=limit)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # SPARQL parse errors surface as SyntaxError
        raise HTTPException(400, str(exc))
    result["elapsed_ms"] = round((time.perf_counter() - start) * 1000, 1)
    return result
