"""Slice A - Jira OAuth connect - TDD red suite (auto-generated, one test per use case)."""

from fastapi.testclient import TestClient
from prism_service.main import app
from urllib.parse import urlparse, parse_qs
import os

client = TestClient(app)


def test_status_unauthenticated_returns_false_and_empty_fingerprint():
    client = TestClient(app)
    resp = client.get("/api/jira/status")
    # Never 500s on the missing credential file; an honest 200 status payload.
    assert resp.status_code == 200, (
        f"expected 200 from /api/jira/status on a fresh service, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    # Mirrors github_auth._status_payload: authenticated flag + empty fingerprint
    # + a credentials_path when no credential file / env override is present.
    assert body.get("authenticated") is False, body
    assert body.get("fingerprint") == "", body
    assert isinstance(body.get("credentials_path"), str) and body["credentials_path"], body


def test_authorize_returns_atlassian_oauth_redirect_url():
    client = TestClient(app)
    resp = client.get("/api/jira/authorize", follow_redirects=False)

    # Route must exist and not error (RED today: /api/jira/* does not exist -> 404).
    assert resp.status_code != 404, "/api/jira/authorize is not routed yet"
    assert resp.status_code < 500, f"authorize errored: {resp.status_code} {resp.text}"

    # Extract the authorize URL from either a redirect Location or a JSON body.
    authorize_url = None
    if resp.status_code in (301, 302, 303, 307, 308):
        authorize_url = resp.headers.get("location")
    else:
        try:
            body = resp.json()
        except ValueError:
            body = None
        assert isinstance(body, dict), f"expected redirect or JSON body, got {resp.text!r}"
        authorize_url = (
            body.get("authorize_url")
            or body.get("authorizeUrl")
            or body.get("url")
        )

    assert authorize_url, f"no Atlassian authorize URL returned (status={resp.status_code})"

    parsed = urlparse(authorize_url)
    # OAuth 3LO must be the PRIMARY path: host proves it's Atlassian's authorize endpoint.
    assert parsed.scheme == "https", f"authorize URL must be https, got {parsed.scheme!r}"
    assert parsed.netloc == "auth.atlassian.com", (
        f"authorize host must be auth.atlassian.com, got {parsed.netloc!r}"
    )
    assert parsed.path == "/authorize", f"expected /authorize path, got {parsed.path!r}"

    qs = parse_qs(parsed.query)
    for key in ("client_id", "redirect_uri", "scope", "response_type", "state"):
        assert key in qs and qs[key] and qs[key][0], f"missing OAuth param {key!r} in {qs!r}"

    assert qs["response_type"][0] == "code", (
        f"response_type must be 'code', got {qs['response_type'][0]!r}"
    )
    # state must be opaque/non-trivial to defend against CSRF.
    assert len(qs["state"][0]) >= 8, f"state param too short to be opaque: {qs['state'][0]!r}"


def test_oauth_callback_stores_refresh_token(monkeypatch):
    # Stub the Atlassian token-exchange seam so no real network call is made and
    # the callback receives a deterministic access + refresh token pair. Done
    # best-effort: today prism_service.services.jira_oauth does not exist, so the
    # patch simply no-ops and the request below returns 404 (the healthy RED).
    # Once implemented, jira_oauth.exchange_code(code, state=...) is the seam the
    # callback route must call to trade the auth code for tokens.
    try:
        from prism_service.services import jira_oauth
        monkeypatch.setattr(
            jira_oauth,
            "exchange_code",
            lambda code, *a, **k: {
                "access_token": "acc-abc123",
                "refresh_token": "ref-xyz789",
                "scope": "read:jira-work offline_access",
                "expires_in": 3600,
            },
            raising=False,
        )
    except ImportError:
        pass

    client = TestClient(app)

    # Start from a clean slate so the "flip to authenticated" is meaningful and
    # not satisfied by a pre-existing stored credential.
    try:
        from prism_service.services import jira_auth as _pre
        _pre.clear_token()
    except Exception:
        pass

    # The OAuth provider redirects the browser here with the code + state.
    resp = client.get(
        "/api/jira/callback",
        params={"code": "the-auth-code", "state": "the-state-nonce"},
        follow_redirects=False,
    )

    # A handled callback either renders a landing page (200) or redirects the
    # browser back into the SPA (3xx) — never a 404. This assertion is the
    # healthy RED today because api/jira.py does not exist yet.
    assert resp.status_code in (200, 302, 303, 307), (
        f"expected the Jira OAuth callback to be handled, got {resp.status_code}"
    )

    # The refresh token must be persisted server-side via jira_auth.set_token,
    # and the stored-credential state must now report authenticated.
    from prism_service.services import jira_auth
    assert jira_auth.is_authenticated() is True

    # And the public status surface reflects the stored credential.
    status = client.get("/api/jira/status")
    assert status.status_code == 200
    assert status.json().get("authenticated") is True


def test_status_authenticated_returns_masked_oauth_fingerprint():
    client = TestClient(app)

    # A distinctive OAuth access token whose last 4 chars are "wxyz".
    token = "atlassian_oauth_secret_wxyz"

    # Simulate a successful OAuth connect: PRISM stores the freshly minted
    # access token (mirrors the github-auth /configure fallback that lands a
    # token without needing to round-trip the real provider).
    connect = client.post("/api/jira/connect", json={"access_token": token})
    assert connect.status_code == 200, (
        f"jira connect should succeed once /api/jira exists, "
        f"got {connect.status_code}: {connect.text}"
    )

    # After connect, status must report authenticated + a masked fingerprint.
    resp = client.get("/api/jira/status")
    assert resp.status_code == 200, (
        f"/api/jira/status should exist post-implementation, "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()

    assert body.get("authenticated") is True, (
        f"status must report authenticated:true after connect, got {body!r}"
    )

    fp = body.get("fingerprint")
    assert isinstance(fp, str) and fp, f"fingerprint must be present, got {fp!r}"

    # Matches the github_auth user:•••last4 convention, here oauth:•••<last4>.
    assert fp == "oauth:•••wxyz", (
        f"fingerprint must be masked as 'oauth:•••' + last 4 of "
        f"the token, got {fp!r}"
    )

    # The masking must never leak the full token.
    assert token not in fp
    assert "secret" not in fp



def test_raw_token_never_leaks_in_any_response():
    # A distinctive value we plant as the raw Jira credential. If this string
    # appears anywhere in any /api/jira response body or header, the token leaked.
    SENTINEL = "SENTINEL-RAW-TOKEN-8f3c9d2e-DO-NOT-LEAK-c1a4"

    client = TestClient(app, raise_server_exceptions=False)

    def leaks(resp):
        """True if the raw sentinel appears anywhere serialized in the response."""
        blob = resp.text or ""
        for k, v in resp.headers.items():
            blob += "\n" + str(k) + ": " + str(v)
        return SENTINEL in blob

    # 1. Store the credential via /configure. Send a superset of plausible
    #    credential field names so we don't couple to one exact schema; pydantic
    #    ignores the extras. Post-implementation this MUST succeed (200) and
    #    persist the raw token server-side, returning only a masked fingerprint.
    configure_body = {
        "api_token": SENTINEL,
        "token": SENTINEL,
        "access_token": SENTINEL,
        "refresh_token": SENTINEL,
        "email": "jira-user@example.com",
        "base_url": "https://example.atlassian.net",
    }
    cfg = client.post("/api/jira/configure", json=configure_body)

    # RED anchor: the endpoint must exist and accept the credential. Today
    # api/jira.py does not exist, so this is a 404 and the assertion fails.
    assert cfg.status_code == 200, (
        f"/api/jira/configure should accept the credential and return 200, "
        f"got {cfg.status_code}: {cfg.text[:300]}"
    )
    # The very response that stored the token must not echo it back raw.
    assert not leaks(cfg), "raw token leaked in /api/jira/configure response"

    # 2. Now sweep every Jira endpoint and assert the raw token never appears.
    #    (method, path, kwargs) — callback carries OAuth query params.
    sweep = [
        ("get", "/api/jira/status", {}),
        ("get", "/api/jira/authorize", {}),
        ("post", "/api/jira/authorize", {"json": {}}),
        ("get", "/api/jira/callback", {"params": {"code": "test-code", "state": "test-state"}}),
        ("post", "/api/jira/configure", {"json": configure_body}),
        ("post", "/api/jira/clear", {"json": {}}),
    ]

    seen_status = False
    for method, path, kwargs in sweep:
        resp = getattr(client, method)(path, **kwargs)
        # A 404 means the route is missing entirely — the feature is unbuilt.
        assert resp.status_code != 404, f"{method.upper()} {path} is missing (404) — /api/jira not implemented"
        assert not leaks(resp), f"raw token leaked in {method.upper()} {path} response"
        if path == "/api/jira/status" and resp.status_code == 200:
            seen_status = True

    # 3. Prove the sweep actually exercised a live status endpoint (guards
    #    against a vacuous pass where nothing real was checked) and that the
    #    stored credential surfaces only as a masked fingerprint, not raw.
    assert seen_status, "/api/jira/status did not return 200 — cannot confirm masking"
    status = client.get("/api/jira/status")
    assert status.json().get("authenticated") is True, (
        "after /configure, /api/jira/status must report authenticated=True"
    )
    assert not leaks(status), "raw token leaked in /api/jira/status after configure"


def test_clear_removes_credential_and_status_reverts():
    client = TestClient(app)

    # 1. Connect: store a Jira credential (refresh token). This is the
    #    precondition — after it, the store holds a credential.
    connect = client.post(
        "/api/jira/connect",
        json={
            "base_url": "https://acme.atlassian.net",
            "email": "dev@acme.example",
            "refresh_token": "jira-refresh-abcdef123456",
        },
    )
    assert connect.status_code == 200, connect.text
    assert connect.json().get("authenticated") is True

    # Sanity: status confirms we are authenticated with a non-empty fingerprint.
    pre = client.get("/api/jira/status")
    assert pre.status_code == 200, pre.text
    pre_body = pre.json()
    assert pre_body.get("authenticated") is True
    assert pre_body.get("fingerprint", "") != ""

    # 2. Clear: delete the stored credential.
    cleared = client.post("/api/jira/clear")
    assert cleared.status_code == 200, cleared.text

    # 3. Status reverts: authenticated:false with an empty fingerprint.
    post = client.get("/api/jira/status")
    assert post.status_code == 200, post.text
    post_body = post.json()
    assert post_body.get("authenticated") is False
    assert post_body.get("fingerprint", "") == ""

    # 4. Idempotent: clearing an already-empty store is not an error.
    again = client.post("/api/jira/clear")
    assert again.status_code == 200, again.text
    assert again.json().get("authenticated") is False


def test_configure_email_token_fallback_authenticates():
    client = TestClient(app)

    # Happy path: email + API token (the non-OAuth basic-auth fallback).
    token = "abcd1234TOKENwxyz"  # last4 == "wxyz"
    email = "dev@example.com"
    resp = client.post(
        "/api/jira/configure",
        json={"email": email, "api_token": token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # The basic-auth credential + status is stored and reported as authenticated.
    assert body.get("authenticated") is True, body

    # A user-style masked fingerprint: "user:•••last4" — carries the email
    # (the basic-auth username), the •••  mask, and only the token's last 4.
    fp = body.get("fingerprint", "")
    assert fp, body
    assert email in fp, fp
    assert "•••" in fp, fp
    assert fp.endswith("wxyz"), fp
    # The raw token must never be echoed back.
    assert token not in fp, fp

    # Empty token -> 400 (mirrors github_auth.configure's ValueError->400).
    r_empty_token = client.post(
        "/api/jira/configure",
        json={"email": email, "api_token": ""},
    )
    assert r_empty_token.status_code == 400, r_empty_token.text

    # Empty email -> 400.
    r_empty_email = client.post(
        "/api/jira/configure",
        json={"email": "", "api_token": token},
    )
    assert r_empty_email.status_code == 400, r_empty_email.text


def test_env_var_override_takes_precedence_over_stored_credential(monkeypatch, tmp_path):
    # Point the service at an empty data dir so there is NO on-disk Jira
    # credential file — the only credential available comes from the env override.
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))

    env_email = "ops@prism.dev"
    env_token = "ATATT3xFfGF0-prism-env-secret-9WZ7K4Q2"
    monkeypatch.setenv("PRISM_JIRA_EMAIL", env_email)
    monkeypatch.setenv("PRISM_JIRA_API_TOKEN", env_token)

    client = TestClient(app)

    resp = client.get("/api/jira/status")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # jira_auth must report authenticated purely from the env override, even
    # though no credential file exists on disk.
    assert data.get("authenticated") is True

    # The status must declare the credential came from the environment, i.e.
    # the env-sourced credential wins over any file-stored one.
    assert data.get("from_env") is True or data.get("source") == "env"

    # Status reflects the ENV token's masked fingerprint: the last 4 chars of
    # the env token surface, the email is echoed, and the full secret never
    # leaks in the response body.
    fingerprint = data.get("fingerprint", "")
    assert env_token[-4:] in fingerprint
    assert env_email in resp.text
    assert env_token not in resp.text
