"""Version API — single source of truth for the SPA's footer + Settings."""

import os
from pathlib import Path

from fastapi import APIRouter, Query

from prism_service.__version__ import PRISM_VERSION, PRISM_VERSION_NOTES

router = APIRouter()

_WEB_INDEX = Path(__file__).resolve().parents[1] / "web_dist" / "index.html"


def _web_build() -> str:
    """Cheap dev-bundle identity; changes whenever Vite replaces index.html."""
    try:
        stat = _WEB_INDEX.stat()
        return f"{stat.st_mtime_ns:x}-{stat.st_size:x}"
    except OSError:
        return "missing"


def _dev_mode() -> bool:
    return os.environ.get("PRISM_DEV_MODE", "").strip().lower() in ("1", "true", "yes", "on")


@router.get("")
def version(notes: bool = Query(
        False, description="Include the full accumulated changelog "
                            "(task 842248bd: ~262 KB, polled every 15s by "
                            "the SPA's watchdog which only ever reads "
                            "`.version`) - opt in explicitly, never the "
                            "routine default.")) -> dict:
    """Return the live service version. The Sidebar footer and the
    Settings page both read this — never hardcode a version string in
    the React tree, or they'll drift apart on the next bump.

    `notes` defaults OFF: the 15s poll and the mount-time fetch only ever
    need `.version` (see lib/version.ts), so shipping the whole changelog
    on every call was pure waste. Pass `?notes=true` for the one place that
    genuinely wants the full string (Sidebar's tooltip)."""
    return {
        "version": PRISM_VERSION,
        "notes": PRISM_VERSION_NOTES if notes else "",
        "dev_mode": _dev_mode(),
        "web_build": _web_build(),
    }
