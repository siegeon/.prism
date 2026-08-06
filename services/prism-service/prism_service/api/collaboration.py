"""Collaboration surface routes + production registration (epic 0784729f).

This is the api/integrations.py pattern applied to the collaboration port
(services/collaboration.py): a builtin adapter registers itself HERE, at
import time, best-effort. A provider that fails to construct records why in
`collaboration_registration_errors` and never takes the API down - connecting
is optional (mx-639efa), same as the work-item adapters.

`collaboration_surfaces()` is what
tests/integration/test_team_work_hub.py::
test_a_collaboration_surface_is_registered_by_production_code reads out of a
clean subprocess import. A test that registers its own adapter cannot
satisfy that assertion - only production code importing this module can,
which is the whole point (task f4dd3687: seven green children shipped over a
feature that could not run because nothing in production ever constructed a
collaborator).

Publish/poll endpoints are a later slice; this one only exposes read-only
diagnostics, and never wires polling into a background loop.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from prism_service.api.auth import current_principal
from prism_service.services import collaboration

# Authenticated like every sibling router (api/integrations.py:31). PRISM
# always requires a login, solo or not (mx-7d38a3), and the errors dict
# below carries provider-side detail that is not public.
router = APIRouter(dependencies=[Depends(current_principal)])

# Why a builtin surface failed to register, if it did. Mirrors
# api/integrations.py's adapter_registration_errors.
collaboration_registration_errors: dict = {}


def register_builtin_collaboration_adapters() -> None:
    try:
        from prism_service.services.github_cli_auth import get_cli_credentials
        from prism_service.services.github_collab import GitHubCommentAdapter
        from prism_service.services.github_rest import GithubRestClient

        collaboration.register_adapter(GitHubCommentAdapter(
            client=GithubRestClient(),
            credentials=get_cli_credentials(),
            # Same CLI-token caveat as api/integrations.py:81-83: not
            # installation-scoped, and installation_token() ignores it.
            installation="github-cli",
        ))
    except Exception as exc:  # pragma: no cover - a broken provider is not fatal
        collaboration_registration_errors["github"] = f"{type(exc).__name__}: {exc}"
        logging.getLogger(__name__).warning(
            "github collaboration adapter not registered: %s", exc)


register_builtin_collaboration_adapters()


def collaboration_surfaces() -> list:
    return collaboration.surfaces()


@router.get("/surfaces")
def get_surfaces() -> dict:
    return {"surfaces": collaboration_surfaces(),
           "errors": dict(collaboration_registration_errors)}
