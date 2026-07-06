"""Slice C — /api/users identity surface + auto-matched Jira link — TDD.

RED until api/users.py exists and is mounted at /api/users. Proves the
oracle seam:
  - GET /api/users returns the auto-provisioned single-operator PRISM
    user (a 'usr_' id), cross-project.
  - GET /api/users/status reports the operator + whether a Jira account
    is linked; `principal` resolves to the operator's usr_ id (a real
    PrismRequestContext principal, not '').
  - POST /api/users/link-jira links the CURRENTLY-AUTHENTICATED jira_auth
    account (its id/handle, NEVER a token) → GET shows jira_account_id
    resolved.
  - POST /api/users/unlink reverts.
"""

from fastapi.testclient import TestClient
from prism_service.main import app


def _client():
    return TestClient(app)


def test_get_users_returns_operator_with_usr_id():
    r = _client().get("/api/users")
    assert r.status_code == 200, r.text
    body = r.json()
    users = body.get("users")
    assert isinstance(users, list) and users, body
    assert any(u["id"].startswith("usr_") for u in users), body


def test_status_reports_operator_and_principal():
    r = _client().get("/api/users/status")
    assert r.status_code == 200, r.text
    body = r.json()
    user = body.get("user")
    assert user and user["id"].startswith("usr_"), body
    # The request principal resolves to a REAL usr_ id (the operator),
    # not the empty default — this is what PrismRequestContext carries.
    assert body.get("principal") == user["id"], body
    assert "jira_linked" in body, body


def test_link_jira_links_authenticated_account_and_resolves():
    client = _client()
    # Connect a Jira account first (OAuth token landing — Slice A).
    connect = client.post(
        "/api/jira/connect",
        json={"refresh_token": "ref-abc1234", "email": "op@team.example",
              "base_url": "https://team.atlassian.net"},
    )
    assert connect.status_code == 200, connect.text

    linked = client.post("/api/users/link-jira", json={})
    assert linked.status_code == 200, linked.text
    body = linked.json()
    assert body.get("jira_linked") is True, body
    acct = body["user"]["jira_account_id"]
    assert acct, body
    # NEVER a token: the raw refresh token must not leak in the response.
    assert "ref-abc1234" not in linked.text

    st = client.get("/api/users/status").json()
    assert st["user"]["jira_account_id"] == acct
    assert st["jira_linked"] is True


def test_unlink_reverts_link_state():
    client = _client()
    client.post("/api/jira/connect", json={"refresh_token": "ref-zzzz9999"})
    client.post("/api/users/link-jira", json={})
    r = client.post("/api/users/unlink")
    assert r.status_code == 200, r.text
    assert r.json().get("jira_linked") is False
    st = client.get("/api/users/status").json()
    assert st["user"]["jira_account_id"] == ""


def test_link_jira_without_connection_400s():
    client = _client()
    client.post("/api/jira/clear")
    client.post("/api/users/unlink")
    r = client.post("/api/users/link-jira", json={})
    assert r.status_code == 400, r.text
