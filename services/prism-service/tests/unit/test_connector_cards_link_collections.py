"""RED — connector cards link to their sync collection (task a752e76c).

Owner directive 2026-08-11: Settings > Connectors renders "Tracking: ..." as
dead muted text; nothing is clickable. This slice makes the server report a
real url per tracked collection and turns the header line into anchors a
person can actually follow.

Payload half pins FR-1..FR-3 (the server contract): `tracking` on
/api/integrations/connect/status becomes list[{key,url}] for every branch of
_provider_state, GitHub's url read verbatim off the stored container, Jira's
derived server-side from the connected site, never raising on an unknown
site.

Source half pins FR-4..FR-7 (what a person can click): the rendered <a> tag
in the card header, RepoSync's tracked list, and api.ts's type shapes — via
brace/paren-balanced block extraction with comments stripped first, never a
fixed character window or a bare-name match (a comment has satisfied this
exact shape of assertion before, see CLAUDE.md Lessons).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

REPO = "siegeon/.prism"
JIRA_SITE = "https://acme.atlassian.net"
JIRA_KEY = "PRIS"

_SETTINGS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
             / "SettingsPage.tsx")
_API_TS = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "lib" / "api.ts")


# ── source-reading helpers (brace/paren-balanced, comments stripped) ──────

def _read(p: Path) -> str:
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    src = re.sub(r"//[^\n]*", "", src)
    return src


def _balanced_block(src: str, open_idx: int) -> str:
    """src[open_idx] must open a bracket. Returns the substring from
    open_idx through its matching close, tracking nesting depth across
    (), [] and {} together — robust to JSX mixing brace and paren groups,
    which a fixed-width slice is not (a comment above an element has
    silently pushed a real guard out of a fixed window before)."""
    assert src[open_idx] in "([{", src[open_idx]
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] in "([{":
            depth += 1
        elif src[i] in ")]}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    raise AssertionError(f"unbalanced bracket from index {open_idx}")


def _connectors_body() -> str:
    src = _strip_comments(_read(_SETTINGS))
    return src[src.index("function ConnectorsSection"):]


def _reposync_body() -> str:
    src = _strip_comments(_read(_SETTINGS))
    start = src.index("function RepoSync")
    end = src.index("function BacklogPush")
    return src[start:end]


def _api_ts() -> str:
    return _strip_comments(_read(_API_TS))


# ── FR-1/2/3: server payload shape ─────────────────────────────────────

@pytest.fixture
def app(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    monkeypatch.setenv("PRISM_AUTH_MODE", "local")
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("PRISM_JIRA_CLIENT_ID", raising=False)

    from prism_service.api import integrations_connect as connect
    from prism_service.services import github_cli_auth
    from prism_service.services.integration_store import (
        IntegrationStore, set_integration_store,
    )
    from prism_service.services.jira_auth import JiraAuthStore, set_jira_auth_store
    from prism_service.services.workspace_service import (
        WorkspaceService, set_workspace_service,
    )

    set_workspace_service(WorkspaceService(tmp_path / "workspace.db"))
    set_integration_store(IntegrationStore(str(tmp_path / "integrations.db")))
    set_jira_auth_store(JiraAuthStore(str(tmp_path / "jira_auth.db")))
    connect.configure_state_db(str(tmp_path / "oauth_states.db"))

    class Creds:
        def token(self): return "gho_test"
        def account(self): return "siegeon"
        def installation_token(self, _=None): return "gho_test"
        def status(self):
            return {"state": "connected", "detail": "test", "account": "siegeon"}

    github_cli_auth.set_cli_credentials(Creds())

    api = FastAPI()
    api.include_router(connect.router, prefix="/api/integrations/connect")
    with TestClient(api) as client:
        yield client

    github_cli_auth.set_cli_credentials(None)
    set_integration_store(None)
    set_jira_auth_store(None)
    set_workspace_service(None)


def _status(app):
    return {r["provider"]: r for r in
            app.get("/api/integrations/connect/status").json()["connectors"]}


def _connect_jira_api_token(app, myself):
    from prism_service.api import integrations_connect as connect

    class _T:
        def get(self, url, headers=None):
            if "/myself" in url:
                return myself
            raise RuntimeError("tenant_info unavailable")

    connect.set_transport("jira", _T())
    r = app.post("/api/integrations/connect/jira/api-token", json={
        "site_url": JIRA_SITE, "email": "ada@acme.com", "api_token": "secret-1"})
    assert r.status_code == 200, r.text


# AC-1: github tracking is [{key,url}], url read verbatim off the container
def test_github_tracking_reports_key_and_url(app):
    r = app.post("/api/integrations/connect/github/track", json={"repo": REPO})
    assert r.status_code == 200, r.text

    tracking = _status(app)["github"].get("tracking")
    assert tracking == [{"key": REPO, "url": f"https://github.com/{REPO}"}], (
        f"expected [{{key,url}}] read verbatim off the stored container; got {tracking!r}")


# AC-2: jira tracking derives its url server-side from the connected site
def test_jira_tracking_derives_url_from_connected_site(app):
    _connect_jira_api_token(app, {"accountId": "acc-1", "displayName": "Ada",
                                   "emailAddress": "ada@acme.com"})
    r = app.post("/api/integrations/connect/jira/track", json={"repo": JIRA_KEY})
    assert r.status_code == 200, r.text

    tracking = _status(app)["jira"].get("tracking")
    assert tracking == [
        {"key": JIRA_KEY, "url": f"{JIRA_SITE}/jira/software/projects/{JIRA_KEY}"}
    ], f"expected a server-derived jira url; got {tracking!r}"


# AC-3: an unknown/unresolvable jira site yields url=="", never a raise
def test_jira_tracking_with_unknown_site_yields_empty_url_not_a_raise(app):
    _connect_jira_api_token(app, {"accountId": "acc-2", "displayName": "Bob",
                                   "emailAddress": "bob@acme.com"})
    r = app.post("/api/integrations/connect/jira/track", json={"repo": JIRA_KEY})
    assert r.status_code == 200, r.text

    from prism_service.services.jira_auth import get_jira_auth_store
    # Simulate an unresolvable site the way JiraAuthStore.site_url already
    # degrades (JiraAuthError -> ""), without touching its real behaviour.
    store = get_jira_auth_store()
    orig = store.site_url
    store.site_url = lambda *a, **k: ""  # noqa: E731 - test double, restored below
    try:
        resp = app.get("/api/integrations/connect/status")
    finally:
        store.site_url = orig
    assert resp.status_code == 200, resp.text
    tracking = {r_["provider"]: r_ for r_ in resp.json()["connectors"]}["jira"]["tracking"]
    assert tracking == [{"key": JIRA_KEY, "url": ""}], (
        f"an unknown site must not raise, and must yield url==''; got {tracking!r}")


# AC-4: claude's tracking stays an unconditional empty list
def test_claude_tracking_is_still_an_empty_list(app):
    assert _status(app)["claude"]["tracking"] == []


# AC-5 / AC-6: the card header renders a real, non-toggling <a> per collection
def test_card_header_renders_a_clickable_anchor_per_collection():
    body = _connectors_body()
    i = body.index("c.tracking")
    open_idx = body.index("(", i)
    block = body[i:open_idx] + _balanced_block(body, open_idx)

    assert "tracking.join" not in block, (
        "the plain-text Tracking paragraph must be replaced, not merely "
        f"supplemented: {block!r}")

    m = re.search(r"<a\b[^>]*>", block, re.DOTALL)
    assert m, f"expected a rendered <a> element for each tracked collection in: {block!r}"
    open_tag = m.group(0)
    assert re.search(r'href=\{[^}]*\.url\}', open_tag), (
        f"the anchor must point at the server-supplied url: {open_tag!r}")
    assert 'target="_blank"' in open_tag, open_tag
    assert 'rel="noreferrer"' in open_tag, open_tag
    assert "onClick=" in open_tag and "stopPropagation" in open_tag, (
        "a link click must not also toggle the card's expand/collapse: "
        f"{open_tag!r}")


# AC-7: RepoSync's tracked list reads the object shape, not the bare string
def test_reposync_tracked_list_reads_the_object_shape():
    body = _reposync_body()
    assert "tracked.map(" in body, "expected the tracked-collections list to still render"
    i = body.index("tracked.map(")
    open_idx = body.index("(", i + len("tracked.map"))
    block = body[i:open_idx] + _balanced_block(body, open_idx)

    assert "<li key={t}>{t}</li>" not in block, (
        "still the old bare-string list item; [object Object] once this is "
        f"fed the new shape: {block!r}")
    assert re.search(r"<li\s+key=\{[\w.]*\.key\}", block), (
        f"expected key={{t.key}} on the list item: {block!r}")
    assert re.search(r"\{[\w]+\.key\}", block), (
        f"expected the rendered text to read the .key field: {block!r}")


# AC-8: api.ts type shapes — Connector.tracking becomes TrackedCollection[],
# MirrorStatus.tracking stays untouched (a different endpoint/consumer)
def test_connector_type_uses_tracked_collection_objects():
    src = _api_ts()
    assert re.search(r"export type TrackedCollection\s*=\s*\{[^}]*key[^}]*url[^}]*\}",
                      src, re.DOTALL), (
        "expected a new exported `TrackedCollection = { key, url }` type")

    i = src.index("export type Connector")
    conn = src[i:src.index("export type", i + 1)]
    assert re.search(r"tracking\??:\s*TrackedCollection\[\]", conn), (
        f"Connector.tracking must become TrackedCollection[]: {conn!r}")


def test_mirror_status_tracking_is_left_as_bare_strings():
    src = _api_ts()
    i = src.index("export type MirrorStatus")
    mirror = src[i:src.index("export type", i + 1) if "export type" in src[i + 1:] else len(src)]
    assert re.search(r"tracking:\s*string\[\]", mirror), (
        f"MirrorStatus.tracking is a DIFFERENT endpoint/consumer and must "
        f"stay string[]: {mirror!r}")
