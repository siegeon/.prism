"""RED scaffold — signed, replay-safe webhook ingestion (task c16cb8e3).

Drives the not-yet-built webhook route + IntegrationEventStore: HMAC signature
verification, delivery-id replay dedupe, and restart durability. Deterministic
(HMAC over the exact body bytes; no network).

Prism modules import INSIDE the fixture/tests so the file collects and fails at
runtime (red = rc 1) before the modules exist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS_A = "workspace-a"
GH_SECRET = "gh-webhook-secret"
JIRA_SECRET = "jira-webhook-secret"


def _sig(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def ctx(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import integration_webhooks as wh
    from prism_service.services.integration_events import (
        IntegrationEventStore,
        set_event_store,
    )

    store = IntegrationEventStore(str(tmp_path / "integration_events.db"))
    set_event_store(store)
    wh.set_webhook_secret("github", GH_SECRET)
    wh.set_webhook_secret("jira", JIRA_SECRET)

    app = FastAPI()
    app.include_router(wh.router, prefix="/integrations/webhooks")
    with TestClient(app) as client:
        yield {"client": client, "store": store, "tmp": tmp_path}

    set_event_store(None)
    wh.reset_webhook_secrets()


def _post_github(client, delivery, body_obj, secret=GH_SECRET, event="issues"):
    body = json.dumps(body_obj).encode("utf-8")
    return client.post(
        "/integrations/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": _sig(secret, body),
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "Content-Type": "application/json",
            "X-Prism-Workspace": WS_A,
        },
    )


def test_valid_github_signature_is_accepted_and_recorded(ctx):
    resp = _post_github(ctx["client"], "d-1", {"action": "opened"})
    assert resp.status_code == 200
    events = ctx["store"].pending_events()
    assert [e.delivery_id for e in events] == ["d-1"]


def test_invalid_signature_is_rejected_and_not_recorded(ctx):
    body = json.dumps({"action": "opened"}).encode("utf-8")
    resp = ctx["client"].post(
        "/integrations/webhooks/github",
        content=body,
        headers={
            "X-Hub-Signature-256": "sha256=deadbeef",
            "X-GitHub-Delivery": "d-bad",
            "X-GitHub-Event": "issues",
            "X-Prism-Workspace": WS_A,
        },
    )
    assert resp.status_code == 401
    assert ctx["store"].pending_events() == []


def test_duplicate_delivery_id_is_applied_once(ctx):
    first = _post_github(ctx["client"], "d-2", {"action": "opened"})
    second = _post_github(ctx["client"], "d-2", {"action": "opened"})
    assert first.status_code == 200 and second.status_code == 200
    assert second.json().get("duplicate") is True
    assert len(ctx["store"].pending_events()) == 1


def test_recorded_events_survive_restart(ctx):
    _post_github(ctx["client"], "d-3", {"action": "opened"})
    from prism_service.services.integration_events import IntegrationEventStore

    reopened = IntegrationEventStore(str(ctx["tmp"] / "integration_events.db"))
    assert "d-3" in {e.delivery_id for e in reopened.pending_events()}


def test_valid_jira_signed_delivery_is_accepted(ctx):
    body = json.dumps({"webhookEvent": "jira:issue_updated"}).encode("utf-8")
    resp = ctx["client"].post(
        "/integrations/webhooks/jira",
        content=body,
        headers={
            "X-Hub-Signature-256": _sig(JIRA_SECRET, body),
            "X-Atlassian-Webhook-Identifier": "j-1",
            "X-Prism-Workspace": WS_A,
        },
    )
    assert resp.status_code == 200
    assert "j-1" in {e.delivery_id for e in ctx["store"].pending_events()}
