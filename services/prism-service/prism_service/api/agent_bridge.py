"""Agent bridge — browser-facing REST surface (task: live remote-assist).

See services/agent_bridge.py's module docstring and
/home/siegeon/.claude/plans/peaceful-seeking-octopus.md for the full design.

Only `POST /sessions` (session creation) uses the caller's REAL principal
(current_principal) — that call is what decides whose session this is. The
other two routes here, plus `GET /sse/agent-bridge/{id}` (routes/sse.py),
carry the session's own short-lived token as their credential instead, and
each performs that check itself; api/security.py carves these item paths out
of the general team-boundary gate for exactly that reason (EventSource can't
send an Authorization header, and reusing the general access key over this
channel would be broader than the bridge session's intended scope).
"""

from __future__ import annotations

import base64
import re
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel

from prism_service.api.auth import coerce_principal, current_principal
from prism_service.data_dir import agent_bridge_screenshot_dir
from prism_service.models.workspace import Principal
from prism_service.services.agent_bridge import get_agent_bridge_service

router = APIRouter()

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg);base64,(.+)$", re.DOTALL)


def _persist_screenshot_if_present(session_id: str, data: dict) -> dict:
    """A `screenshot` command's result carries a full PNG as a base64 data
    URL — round-tripping that through the MCP tool's text response would
    dump the whole image into an agent's context as a giant string. Persist
    it to disk (mirrors evidence_dir's existing "screenshots live in PRISM's
    own data dir, never inline" pattern) and hand back a path instead."""
    image = data.get("image")
    if not isinstance(image, str) or not _SESSION_ID_RE.match(session_id):
        return data
    match = _DATA_URL_RE.match(image)
    if not match:
        return data
    ext = "png" if match.group(1) == "png" else "jpg"
    try:
        raw = base64.b64decode(match.group(2), validate=True)
    except Exception:
        return data
    path = agent_bridge_screenshot_dir(session_id) / f"{uuid.uuid4().hex}.{ext}"
    path.write_bytes(raw)
    rest = {k: v for k, v in data.items() if k != "image"}
    return {**rest, "image_path": str(path)}


class CreateSessionBody(BaseModel):
    project: str = "default"


@router.post("/sessions")
def create_session(
    body: CreateSessionBody = Body(default_factory=CreateSessionBody),
    principal: Principal = Depends(current_principal),
) -> dict:
    """Mint a bridge session for the CALLER's own tab. The returned token is
    held in the browser's memory only (never localStorage — it must not
    outlive the tab) and is a distinct credential from the caller's general
    access key: it can only relay commands into this one session."""
    principal = coerce_principal(principal)
    service = get_agent_bridge_service()
    session = service.mint_session(
        user_id=principal.user_id, project_id=(body.project or "default"))
    return {
        "id": session.id,
        "token": session.token,
        "project": session.project_id,
        "expires_at": session.expires_at,
    }


class ResultBody(BaseModel):
    token: str
    command_id: str
    ok: bool = True
    error: str = ""
    data: dict = {}


@router.post("/sessions/{session_id}/results")
def submit_result(session_id: str, body: ResultBody) -> dict:
    """Browser -> server: report the outcome of one executed command. The
    agent_bridge_command MCP tool is blocked waiting on exactly this."""
    service = get_agent_bridge_service()
    session = service.validate_token(session_id, body.token)
    if session is None:
        raise HTTPException(401, "invalid, expired, or revoked bridge session")
    data = _persist_screenshot_if_present(session_id, body.data)
    accepted = service.submit_result(session_id, body.command_id, {
        "ok": body.ok, "error": body.error, "data": data,
    })
    if not accepted:
        raise HTTPException(404, "no command is waiting on that command_id")
    return {"ok": True}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: str, token: str = Query("")) -> dict:
    """Explicit end (owner: 'the user can always revoke'). Accepts the
    session's OWN token as its credential — a tab-close beforeunload call has
    no reliable chance to attach the caller's real access key, and the token
    is exactly the thing being torn down, so token-only revocation is safe."""
    service = get_agent_bridge_service()
    session = service.validate_token(session_id, token)
    if session is None:
        raise HTTPException(404, "no active bridge session with that id/token")
    service.revoke(session_id)
    return {"ok": True}
