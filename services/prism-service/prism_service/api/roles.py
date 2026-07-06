"""Roles API — the single source-of-truth role/tier registry for the SPA.

Thin read-through to prism_service.models.roles (the ONLY place roles/tiers
are declared). Backs the "Token cost per role" panel's tier/label lookups and
any UI that renders the model-agnostic routing table.
"""

from fastapi import APIRouter

from prism_service.models import roles

router = APIRouter()


@router.get("")
def get_roles() -> dict:
    """Return the role/tier registry: {tiers, efforts, roles, step_roles}."""
    return roles.registry()
