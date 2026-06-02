"""/api/update — surface auto-updater state to the SPA + allow manual trigger.

  GET  /api/update/status      current state (running version, latest known,
                                update available, last check, errors)
  POST /api/update/check       force a fresh GitHub Releases poll
  POST /api/update/apply       download + pip install the latest wheel
                                (no-op in docker — Watchtower's job there)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from prism_service.services import auto_updater

router = APIRouter()


@router.get("/status")
def status() -> dict:
    return auto_updater.get_status()


@router.post("/check")
def check() -> dict:
    """Force-refresh the version check now instead of waiting for the
    background poll. Cheap (1 GET against the GitHub Releases API)."""
    s = auto_updater.check_for_update()
    # Convert dataclass to dict the same way /status does.
    return auto_updater.get_status() if s is None else auto_updater.get_status()


@router.post("/apply")
def apply() -> dict:
    """Download the latest wheel and pip-install it. Returns a result
    dict — caller (SPA) checks `ok` to decide whether to prompt for a
    restart. On success the apply sets `restart_required=True` and the
    auto-updater requests a restart that the MAIN thread performs
    (graceful uvicorn stop, then os.execv) so the served version flips on
    its own — supervisor-agnostic, no manual step. The result's
    `restart_auto` reflects whether that automatic restart will fire."""
    result = auto_updater.apply_update()
    if not result.get("ok"):
        # 409 = "not ready / no update / already up to date" — let the
        # SPA distinguish this from a real server error.
        raise HTTPException(409, result.get("reason", "apply failed"))
    return result
