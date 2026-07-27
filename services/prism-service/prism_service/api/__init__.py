"""HTTP API surface for the React SPA.

Mounted under /api. Each sub-router is a thin read-through to existing
services (brain_svc, project_context, etc.) — no behavior changes.

Today this is attached to NiceGUI's underlying FastAPI app from main.py.
After the Task #6 cutover, the same router attaches to a plain FastAPI
app; the import-time side effects stay in the per-module routers.
"""

from fastapi import APIRouter

from prism_service.api.agent_runs import router as agent_runs_router
from prism_service.api.auth import router as auth_router
from prism_service.api.brain import router as brain_router
from prism_service.api.claude_auth import router as claude_auth_router
from prism_service.api.claude_runs import router as claude_runs_router
from prism_service.api.consolidation import router as consolidation_router
from prism_service.api.conductor import router as conductor_router
from prism_service.api.conductor_flow import router as conductor_flow_router
from prism_service.api.dashboard import router as dashboard_router
from prism_service.api.github_auth import router as github_auth_router
from prism_service.api.graph import router as graph_router
from prism_service.api.integrations import router as integrations_router
from prism_service.api.integrations_connect import router as integrations_connect_router
from prism_service.api.jobs import router as jobs_router
from prism_service.api.learning import router as learning_router
from prism_service.api.memory import router as memory_router
from prism_service.api.okf import router as okf_router
from prism_service.api.projects import router as projects_router
from prism_service.api.retrievals import router as retrievals_router
from prism_service.api.roles import router as roles_router
from prism_service.api.sandbox_jobs import router as sandbox_jobs_router
from prism_service.api.service_info import router as service_info_router
from prism_service.api.sessions import router as sessions_router
from prism_service.api.tasks import router as tasks_router
from prism_service.api.staleness import router as staleness_router
from prism_service.api.understand import router as understand_router
from prism_service.api.update import router as update_router
from prism_service.api.version import router as version_router
from prism_service.api.watchdog import router as watchdog_router
from prism_service.api.workspaces import router as workspaces_router
from prism_service.api.xref import router as xref_router

api_router = APIRouter(prefix="/api")
api_router.include_router(agent_runs_router, prefix="/agent-runs", tags=["agent-runs"])
api_router.include_router(auth_router, prefix="/auth", tags=["auth"])
api_router.include_router(brain_router, prefix="/brain", tags=["brain"])
api_router.include_router(claude_auth_router, prefix="/claude-auth", tags=["claude-auth"])
api_router.include_router(claude_runs_router, prefix="/claude-runs", tags=["claude-runs"])
api_router.include_router(consolidation_router, prefix="/consolidation", tags=["consolidation"])
api_router.include_router(conductor_router, prefix="/conductor", tags=["conductor"])
api_router.include_router(conductor_flow_router, prefix="/conductor/flow", tags=["conductor-flow"])
api_router.include_router(dashboard_router, prefix="/dashboard", tags=["dashboard"])
api_router.include_router(github_auth_router, prefix="/github-auth", tags=["github-auth"])
api_router.include_router(graph_router, prefix="/graph", tags=["graph"])
api_router.include_router(jobs_router, prefix="/jobs", tags=["jobs"])
api_router.include_router(learning_router, prefix="/learning", tags=["learning"])
api_router.include_router(memory_router, prefix="/memory", tags=["memory"])
api_router.include_router(okf_router, prefix="/okf", tags=["okf"])
api_router.include_router(projects_router, prefix="/projects", tags=["projects"])
api_router.include_router(retrievals_router, prefix="/retrievals", tags=["retrievals"])
api_router.include_router(roles_router, prefix="/roles", tags=["roles"])
api_router.include_router(sandbox_jobs_router, prefix="/conductor/jobs", tags=["sandbox-jobs"])
api_router.include_router(service_info_router, prefix="/service-info", tags=["service-info"])
api_router.include_router(sessions_router, prefix="/sessions", tags=["sessions"])
api_router.include_router(tasks_router, prefix="/tasks", tags=["tasks"])
api_router.include_router(staleness_router, prefix="/staleness", tags=["staleness"])
api_router.include_router(understand_router, prefix="/understand", tags=["understand"])
api_router.include_router(update_router, prefix="/update", tags=["update"])
api_router.include_router(version_router, prefix="/version", tags=["version"])
api_router.include_router(watchdog_router, prefix="/watchdog", tags=["watchdog"])
api_router.include_router(workspaces_router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(integrations_router, prefix="/workspaces", tags=["integrations"])
# The CONNECT front door (task dbbea1d3) — user-scoped, works solo, so it is
# NOT under /workspaces/{id}: providers are optional and connecting must never
# require a team workspace (owner decision mx-639efa).
api_router.include_router(integrations_connect_router,
                          prefix="/integrations/connect", tags=["integrations-connect"])
api_router.include_router(xref_router, prefix="/xref", tags=["xref"])
