"""Signed provider webhook ingestion (task c16cb8e3).

Mounted OUTSIDE the /api team boundary (see main.py) because a webhook
authenticates by provider SIGNATURE, not a bearer. Each delivery is signature-
verified over the raw body and recorded once by delivery id (a replay is a
no-op). The endpoint returns 200 for a new or duplicate delivery and 401 for a
bad signature.
"""

from __future__ import annotations

import hashlib
import os

from fastapi import APIRouter, HTTPException, Request

from prism_service.services.integration_events import (
    get_event_store,
    verify_hmac_sha256,
)


router = APIRouter()

# Per-provider shared secret. Tests inject via set_webhook_secret(); production
# falls back to PRISM_<PROVIDER>_WEBHOOK_SECRET.
_secrets: dict[str, str] = {}

# Where each provider puts its unique delivery id.
_DELIVERY_HEADERS = {
    "github": ["X-GitHub-Delivery"],
    "jira": ["X-Atlassian-Webhook-Identifier", "X-Prism-Delivery"],
}
_EVENT_HEADERS = {"github": "X-GitHub-Event", "jira": "X-Prism-Event"}


def set_webhook_secret(provider: str, secret: str) -> None:
    _secrets[provider] = secret


def reset_webhook_secrets() -> None:
    _secrets.clear()


def _secret_for(provider: str) -> str:
    if provider in _secrets:
        return _secrets[provider]
    return os.environ.get(f"PRISM_{provider.upper()}_WEBHOOK_SECRET", "").strip()


def _delivery_id(provider: str, request: Request) -> str:
    for header in _DELIVERY_HEADERS.get(provider, []):
        value = request.headers.get(header)
        if value:
            return value
    return request.headers.get("X-Prism-Delivery", "")


@router.post("/{provider}")
async def receive_webhook(provider: str, request: Request) -> dict:
    body = await request.body()
    secret = _secret_for(provider)
    signature = request.headers.get("X-Hub-Signature-256", "")
    if not verify_hmac_sha256(secret, body, signature):
        raise HTTPException(401, "invalid webhook signature")

    delivery_id = _delivery_id(provider, request)
    if not delivery_id:
        raise HTTPException(400, "missing delivery id")

    workspace_id = request.headers.get("X-Prism-Workspace", "")
    event_type = request.headers.get(_EVENT_HEADERS.get(provider, ""), "")
    digest = hashlib.sha256(body).hexdigest()

    _event, is_new = get_event_store().record_delivery(
        workspace_id, provider, delivery_id, event_type=event_type,
        payload_digest=digest)
    return {"ok": True, "delivery_id": delivery_id, "duplicate": not is_new}
