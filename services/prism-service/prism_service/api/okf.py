"""OKF API — PRISM's stores projected as a live, read-only OKF wiki.

Thin read-through to services/okf_host.OkfHost (which only READS the memory +
brain stores; never writes brain.db / graph.db). One host is cached per project
so the bundle isn't rebuilt on every request — OkfHost itself invalidates on a
cheap content signature, so the cache stays correct across writes elsewhere.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse

from prism_service.project_context import get_project
from prism_service.services.okf_host import OkfHost

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


@router.get("/concept")
def concept(project: str = Query("default"), path: str = Query(...)) -> dict:
    """One projected OKF concept (frontmatter + body + links) by path."""
    c = _host(project).get(path)
    if c is None:
        raise HTTPException(404, f"unknown concept: {path}")
    return c


@router.get("/raw/{path:path}", response_class=PlainTextResponse)
def raw(path: str, project: str = Query("default")) -> PlainTextResponse:
    """Conformant OKF markdown for a path ('index.md' serves the root index)."""
    text = _host(project).raw("/" + path)
    if text is None:
        raise HTTPException(404, f"unknown path: {path}")
    return PlainTextResponse(text, media_type="text/markdown")
