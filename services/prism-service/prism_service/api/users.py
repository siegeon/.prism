"""/api/users — PRISM's first-class identity surface (Slice C).

A PRISM User is created and OWNED in PRISM; a Jira account is linked ONTO
it (never the other way round). PRISM is single-operator today, so this
surfaces THE auto-provisioned operator user and lets the operator link the
CURRENTLY-AUTHENTICATED Jira account (its accountId/handle only — NEVER a
token; tokens stay in jira_auth's chmod-600 store and never leave the
process). Read-through to services/users.py + services/jira_auth.py.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from prism_service.services import jira_auth as ja
from prism_service.services import users as users_svc

router = APIRouter()


class LinkBody(BaseModel):
    """Optional explicit account override; empty = auto-match the
    currently-authenticated Jira account."""
    account_id: str = ""


def _jira_account_hint() -> str:
    """The account id/handle to auto-match from the OAuth-authorized Jira
    session — the email when known, else the masked fingerprint. NEVER a
    token."""
    if not ja.is_authenticated():
        return ""
    meta = ja.current_meta()
    return (meta.get("email") or "").strip() or ja.token_fingerprint()


def _status_payload() -> dict:
    op = users_svc.operator()
    connected = ja.is_authenticated()
    return {
        "user": dataclasses.asdict(op),
        "principal": op.id,
        "jira_linked": bool(op.jira_account_id),
        "jira_connected": connected,
        # The account PRISM would auto-match if the operator links now.
        "jira_account_hint": _jira_account_hint() if connected else "",
        "store_path": users_svc.default_service()._db_path,
    }


@router.get("")
@router.get("/")
def list_users() -> dict:
    """List all PRISM users (operator auto-provisioned so this is never
    empty)."""
    svc = users_svc.default_service()
    op = svc.get_or_create_operator()
    return {
        "users": [dataclasses.asdict(u) for u in svc.list()],
        "operator_id": op.id,
    }


@router.get("/status")
@router.get("/me")
def status() -> dict:
    return _status_payload()


@router.post("/link-jira")
def link_jira(body: LinkBody | None = None) -> dict:
    """Link the currently-authenticated Jira account (or an explicit
    override) onto the operator user — storing the accountId/handle, NEVER
    a token."""
    account = ((body.account_id if body else "") or "").strip()
    account = account or _jira_account_hint()
    if not account:
        raise HTTPException(
            400, "no Jira account to link — connect Jira in Connections first"
        )
    svc = users_svc.default_service()
    svc.link_jira(svc.get_or_create_operator().id, account)
    return _status_payload()


@router.post("/unlink")
def unlink() -> dict:
    """Remove the operator's Jira link. Idempotent."""
    svc = users_svc.default_service()
    svc.link_jira(svc.get_or_create_operator().id, "")
    return _status_payload()
