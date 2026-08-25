"""OKF API — PRISM's stores projected as a live, read-only OKF wiki.

Thin read-through to services/okf_host.OkfHost (which only READS the memory +
brain stores; never writes brain.db / graph.db). One host is cached per project
so the bundle isn't rebuilt on every request — OkfHost itself invalidates on a
cheap content signature, so the cache stays correct across writes elsewhere.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from prism_service.project_context import get_project
from prism_service.services import ontology_prototype_projection
from prism_service.services.okf_host import OkfHost
from prism_service.services.ontology_store import OntologyStore

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
# Ontology (task 15c06516) — persisted classes/instances/properties/axioms,
# served straight off OntologyStore. Never computed on this read path; the
# projection (services/ontology_prototype_projection.rebuild) is the only
# writer. Auto-runs ONCE on an empty store so /understand shows real data
# without a manual rebuild click (no per-project refresh job is reachable
# from this file's allowed scope for this walking skeleton).
# ---------------------------------------------------------------------------

@router.get("/ontology")
def ontology(project: str = Query("default")) -> dict:
    store = OntologyStore(project)
    if store.is_empty():
        ontology_prototype_projection.rebuild(project)
    return {
        "classes": store.list_classes(),
        "properties": store.list_properties(),
        "axioms": store.list_axioms(),
    }


@router.get("/ontology/instances")
def ontology_instances(
    project: str = Query("default"), class_id: str = Query(...),
    limit: int = Query(200),
) -> dict:
    store = OntologyStore(project)
    return {"instances": store.list_instances(class_id, limit=limit)}


@router.post("/ontology/rebuild")
def ontology_rebuild(project: str = Query("default")) -> dict:
    return ontology_prototype_projection.rebuild(project)
