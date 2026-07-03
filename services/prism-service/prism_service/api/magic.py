"""/api/magic — PRISM's connection to a headless Magic Cloud backend.

Configure/clear the stored credential (fingerprint-only responses —
the secret never leaves the server), bootstrap a fresh instance, and
proxy admin operations (execute Hyperlambda, list endpoints) through
services/magic_client. Lane 1 of the LOB-factory epic.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from prism_service.services import magic_client as mc
from prism_service.services import magic_app_builder as ab

router = APIRouter()


class ConfigureBody(BaseModel):
    url: str
    user: str = "root"
    password: str


class SetupBody(BaseModel):
    url: str
    password: str
    name: str = ""
    email: str = ""


class ExecuteBody(BaseModel):
    hyperlambda: str


@router.get("/status")
def status() -> dict:
    return mc.status()


@router.post("/configure")
def configure(body: ConfigureBody) -> dict:
    try:
        mc.authenticate(body.url, body.user, body.password)
    except mc.MagicError as e:
        code = 400 if e.status in (400, 401, 403) else 502
        raise HTTPException(code, f"magic connection failed: {e}")
    try:
        mc.save_connection(body.url, body.user, body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return mc.status()


@router.post("/clear")
def clear() -> dict:
    mc.clear_connection()
    return mc.status()


@router.post("/setup")
def setup(body: SetupBody) -> dict:
    """Bootstrap a fresh headless instance — PRISM owns the root/root
    window so nobody else can claim it after provisioning."""
    try:
        out = mc.bootstrap(body.url, body.password,
                           name=body.name, email=body.email)
    except mc.MagicError as e:
        raise HTTPException(502, f"magic bootstrap failed: {e}")
    resp = mc.status()
    resp.update(out)
    return resp


@router.post("/execute")
def execute(body: ExecuteBody) -> dict:
    try:
        return mc.execute(body.hyperlambda)
    except mc.MagicError as e:
        raise HTTPException(502, f"magic execute failed: {e}")


@router.get("/endpoints")
def endpoints():
    try:
        return mc.endpoints()
    except mc.MagicError as e:
        raise HTTPException(502, f"magic endpoints failed: {e}")


class BuildAppBody(BaseModel):
    spec: dict


@router.post("/build-app")
def build_app(body: BuildAppBody) -> dict:
    """PRISM as expert user of Magic: render a structured app spec into
    whitespace-perfect Hyperlambda and deploy it to the connected tenant.
    The spec is what a small model can reliably produce; the render is
    deterministic so the built app always parses."""
    try:
        return ab.deploy_app(body.spec)
    except mc.MagicError as e:
        raise HTTPException(502, f"magic build-app failed: {e}")
    except (KeyError, TypeError) as e:
        raise HTTPException(400, f"invalid app spec: {e}")
