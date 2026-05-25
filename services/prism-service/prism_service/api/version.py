"""Version API — single source of truth for the SPA's footer + Settings."""

import os

from fastapi import APIRouter

from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES

router = APIRouter()


def _dev_mode() -> bool:
    return os.environ.get("PRISM_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")


@router.get("")
def version() -> dict:
    """Return the live service version. The Sidebar footer and the
    Settings page both read this — never hardcode a version string in
    the React tree, or they'll drift apart on the next bump."""
    return {
        "version": PRISM_VERSION,
        "notes": PRISM_VERSION_NOTES,
        "dev_mode": _dev_mode(),
    }
