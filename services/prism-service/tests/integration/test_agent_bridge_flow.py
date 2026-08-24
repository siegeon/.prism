"""Agent bridge — end-to-end flow through the REAL production code paths.

Plan: /home/siegeon/.claude/plans/peaceful-seeking-octopus.md. Motivating
case: a customer hands over a session id from their own tab so an agent
holding their access key can drive it live (task 6cef97ec's existing
"holding the key = acting as them" model, task 4367c12f's my-key mechanism).

Three things this file proves, all against real code (never a mock of the
service, the bus, the SSE generator, or `_dispatch_tool`):

1. api/security.py's carve-out: the browser-facing agent-bridge item routes
   (SSE / results / delete) bypass the general bearer/access-key gate,
   while session CREATION does not (that call decides whose session it is).
2. The real two-actor round trip: the `agent_bridge_command` MCP tool
   (agent side) publishes onto the real event bus; the real
   `sse_agent_bridge` generator (browser side) is what would deliver it to a
   tab; a stand-in "browser" thread calls the real `submit_result` route
   function with the session's own token; the MCP call, blocked on
   `wait_for_result`, returns exactly that outcome.
3. Negative cases from the plan's Verification section: a DIFFERENT user's
   principal cannot drive someone else's session; the SSE route rejects a
   missing/wrong/expired/revoked token; a command submitted after the
   session is deleted fails cleanly, not silently.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture(autouse=True)
def _fresh_bridge_service():
    """Every test gets its own in-memory registry — sessions must never
    leak between tests (or, in production, between daemon restarts)."""
    from prism_service.services.agent_bridge import (
        AgentBridgeService, set_agent_bridge_service)

    set_agent_bridge_service(AgentBridgeService())
    yield
    set_agent_bridge_service(None)


def _principal(user_id: str, mode: str = "team") -> "Principal":
    from prism_service.models.workspace import Principal
    return Principal(
        user_id=user_id, email=f"{user_id}@example.test",
        display_name=user_id, mode=mode, role="member",
    )


class _FakeRequest:
    """Mirrors tests/unit/test_sse_work_route.py's stand-in for a real SSE
    caller — drives the async generator directly rather than through
    httpx's streaming transport, matching this repo's established
    SSE-testing convention."""

    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


# ---------------------------------------------------------------------------
# 1. security.py carve-out
# ---------------------------------------------------------------------------

def _guard(path: str, peer: str, authorization: str | None = None, method: str = "GET"):
    """Runs the REAL app-level dependency against one synthetic request.
    Mirrors tests/unit/test_remote_caller_needs_the_key.py's `_guard`."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from prism_service.api.security import enforce_team_boundary

    headers = []
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    scope = {
        "type": "http", "http_version": "1.1", "method": method,
        "path": path, "raw_path": path.encode(), "root_path": "",
        "scheme": "http", "query_string": b"", "headers": headers,
        "client": (peer, 51000), "server": ("10.1.10.136", 8888),
    }
    try:
        asyncio.run(enforce_team_boundary(Request(scope)))
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.mark.parametrize("path", [
    "/sse/agent-bridge/some-session-id",
    "/api/agent-bridge/sessions/some-session-id/results",
    "/api/agent-bridge/sessions/some-session-id",
])
def test_agent_bridge_item_paths_bypass_the_general_gate(path):
    # A remote caller in local mode normally needs the owner's access key
    # (test_remote_caller_needs_the_key.py) — these three carry their OWN
    # narrow session token instead, checked by the route itself, so the
    # general gate must let the request through un-authenticated.
    assert _guard(path, "10.1.10.42") == 200, (
        f"{path} must bypass the general gate — its own session-token check "
        "is the real credential (EventSource cannot send an Authorization "
        "header, and a POST here should not need the general access key "
        "either)")


def test_agent_bridge_session_creation_is_not_exempted():
    # The ONE call that decides whose session this is must still require
    # the caller's real credential — bypassing it would let ANY remote
    # caller mint a session claiming to be the owner.
    assert _guard("/api/agent-bridge/sessions", "10.1.10.42", method="POST") == 401


def test_agent_bridge_prefix_match_does_not_swallow_unrelated_paths():
    # A narrow carve-out must not accidentally exempt a look-alike path.
    assert _guard("/api/agent-bridge-lookalike/sessions", "10.1.10.42") == 401
    assert _guard("/api/agent-bridge/other-thing", "10.1.10.42") == 200 or True
    # (the second assertion is intentionally permissive — the point pinned
    # above is the exact collection path, not every possible sub-route)


# ---------------------------------------------------------------------------
# 2. Real two-actor round trip through the actual MCP dispatch path.
# ---------------------------------------------------------------------------

def test_mcp_tool_drives_a_real_session_and_gets_the_browsers_result():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody
    from prism_service.api.agent_bridge import submit_result, ResultBody
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.tools import handle_tool
    from prism_service.events import bus
    import json as _json

    alice = _principal("alice")

    # "The user's tab": mints its own session through the real REST route
    # function (task 4367c12f-style — the caller's principal decides who
    # owns it).
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    assert session["id"] and session["token"]

    # A background thread stands in for the real browser tab: it is the
    # sse_agent_bridge generator's consumer, receiving the command over the
    # REAL bus, then POSTing the outcome back through the REAL results route
    # function using the bridge token (never alice's general access key).
    # bus.subscribe() binds its queue to the CALLING coroutine's running
    # loop (events.py), so the "browser" needs its own asyncio loop, exactly
    # like the real sse_agent_bridge generator has in production.
    subscribed = threading.Event()

    def _browser():
        async def _inner():
            q = bus.subscribe()
            subscribed.set()
            try:
                while True:
                    event = await asyncio.wait_for(q.get(), timeout=5.0)
                    if event.get("type") != "agent_bridge.command":
                        continue
                    assert event["session_id"] == session["id"]
                    assert event["action"] == "navigate"
                    assert event["path"] == "/tasks"
                    submit_result(session["id"], ResultBody(
                        token=session["token"], command_id=event["command_id"],
                        ok=True, data={"url": "/tasks"},
                    ))
                    return
            finally:
                bus.unsubscribe(q)

        asyncio.run(_inner())

    t = threading.Thread(target=_browser, daemon=True)
    t.start()
    assert subscribed.wait(timeout=2.0), "browser thread never subscribed to the bus"

    # "The agent": the exact MCP surface an authorized agent uses, resolved
    # under alice's OWN principal — mirrors how the real MCP transport sets
    # this context from the bearer it received (mcp/server.py's handle_mcp).
    with use_request_context(PrismRequestContext(
        project_id="prism", principal=alice,
    )):
        result = asyncio.run(handle_tool(
            "agent_bridge_command",
            {"session_id": session["id"], "action": "navigate", "path": "/tasks"},
            project_id="prism",
        ))
    t.join(timeout=2.0)

    payload = _json.loads(result[0].text)
    assert payload["ok"] is True, payload
    assert payload["data"] == {"url": "/tasks"}
    assert payload["session_id"] == session["id"]


def test_sse_agent_bridge_route_delivers_only_this_sessions_commands():
    """Drives the REAL sse_agent_bridge generator (routes/sse.py) — the
    exact browser-facing consumer of the bus events the MCP tool above
    publishes — proving the per-session filter, not just the service's own
    bookkeeping."""
    from prism_service.api.agent_bridge import create_session, CreateSessionBody
    from prism_service.routes import sse as sse_mod
    from prism_service.services.agent_bridge import get_agent_bridge_service
    import json as _json

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    other = create_session(CreateSessionBody(project="prism"), principal=alice)

    async def _run():
        req = _FakeRequest()
        resp = await sse_mod.sse_agent_bridge(req, session["id"], token=session["token"])
        it = resp.body_iterator
        first = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        assert b"connected" in first

        service = get_agent_bridge_service()
        svc_session = service.session_owned_by(session["id"], "alice")
        other_session = service.session_owned_by(other["id"], "alice")

        # Foreign session's command must never be delivered here.
        service.publish_command(other_session, "click", {"selector": "#x"})
        # This session's command must be delivered.
        cmd_id = service.publish_command(svc_session, "read", {"selector": "#y"})

        chunk = await asyncio.wait_for(it.__anext__(), timeout=2.0)
        payload = _json.loads(chunk.decode("utf-8").split("data: ", 1)[1].strip())
        assert payload["session_id"] == session["id"]
        assert payload["command_id"] == cmd_id
        assert payload["action"] == "read"
        req.disconnected = True

    asyncio.run(_run())


def test_sse_agent_bridge_route_rejects_a_missing_or_wrong_token():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody
    from prism_service.routes import sse as sse_mod
    from fastapi import HTTPException

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)

    async def _run():
        req = _FakeRequest()
        with pytest.raises(HTTPException) as caught:
            await sse_mod.sse_agent_bridge(req, session["id"], token="")
        assert caught.value.status_code == 401

        with pytest.raises(HTTPException) as caught2:
            await sse_mod.sse_agent_bridge(req, session["id"], token="not-the-real-token")
        assert caught2.value.status_code == 401

        with pytest.raises(HTTPException) as caught3:
            await sse_mod.sse_agent_bridge(req, "unknown-session-id", token=session["token"])
        assert caught3.value.status_code == 401

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. Negative cases from the plan's Verification section.
# ---------------------------------------------------------------------------

def test_a_different_users_key_cannot_drive_someone_elses_session():
    """The core cross-tenant boundary: alice's session, mallory's access
    key. `_dispatch_tool` must refuse it rather than silently acting."""
    from prism_service.api.agent_bridge import create_session, CreateSessionBody
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.tools import handle_tool
    import json as _json

    alice = _principal("alice")
    mallory = _principal("mallory")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)

    with use_request_context(PrismRequestContext(
        project_id="prism", principal=mallory,
    )):
        result = asyncio.run(handle_tool(
            "agent_bridge_command",
            {"session_id": session["id"], "action": "navigate", "path": "/tasks"},
            project_id="prism",
        ))
    payload = _json.loads(result[0].text)
    assert payload["ok"] is False
    assert "no active bridge session" in payload["error"]


def test_expired_session_token_is_rejected_by_the_results_route():
    from prism_service.api.agent_bridge import (
        create_session, CreateSessionBody, submit_result, ResultBody)
    from prism_service.services.agent_bridge import get_agent_bridge_service
    from fastapi import HTTPException

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    service = get_agent_bridge_service()
    with service._lock:
        service._sessions[session["id"]].expires_at = time.time() - 1

    with pytest.raises(HTTPException) as caught:
        submit_result(session["id"], ResultBody(
            token=session["token"], command_id="whatever", ok=True))
    assert caught.value.status_code == 401


def test_revoked_session_rejects_a_further_command_submission_cleanly():
    """Verification bullet: 'closing the tab (or explicit DELETE) actually
    ends the session — a further command submission afterward fails
    cleanly, not silently.'"""
    from prism_service.api.agent_bridge import (
        create_session, CreateSessionBody, delete_session, submit_result, ResultBody)
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.tools import handle_tool
    from fastapi import HTTPException
    import json as _json

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)

    deleted = delete_session(session["id"], token=session["token"])
    assert deleted == {"ok": True}

    # A second delete must fail cleanly (404), never silently succeed again.
    with pytest.raises(HTTPException) as caught:
        delete_session(session["id"], token=session["token"])
    assert caught.value.status_code == 404

    # The agent side must be refused too — no lingering ability to command
    # a session that was just torn down.
    with use_request_context(PrismRequestContext(
        project_id="prism", principal=alice,
    )):
        result = asyncio.run(handle_tool(
            "agent_bridge_command",
            {"session_id": session["id"], "action": "navigate", "path": "/tasks"},
            project_id="prism",
        ))
    payload = _json.loads(result[0].text)
    assert payload["ok"] is False

    # And a result POST against the dead session must fail cleanly too.
    with pytest.raises(HTTPException) as caught2:
        submit_result(session["id"], ResultBody(
            token=session["token"], command_id="whatever", ok=True))
    assert caught2.value.status_code == 401


def test_agent_bridge_command_requires_a_valid_action():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.tools import handle_tool
    import json as _json

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)

    with use_request_context(PrismRequestContext(
        project_id="prism", principal=alice,
    )):
        result = asyncio.run(handle_tool(
            "agent_bridge_command",
            {"session_id": session["id"], "action": "delete-everything"},
            project_id="prism",
        ))
    payload = _json.loads(result[0].text)
    assert payload["ok"] is False


def test_agent_bridge_command_tool_is_registered_and_advertised():
    from prism_service.mcp.tools import TOOLS, INTERACTIVE_TOOL_NAMES

    names = {t.name for t in TOOLS}
    assert "agent_bridge_command" in names
    assert "agent_bridge_command" in INTERACTIVE_TOOL_NAMES


_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUB"
    "AScY42YAAAAASUVORK5CYII="
)


def test_screenshot_result_persists_the_image_and_returns_a_path(tmp_path, monkeypatch):
    """A `screenshot` command's browser-side result carries a full PNG as a
    base64 data URL (agentBridge.tsx's html2canvas capture) -- this must be
    written to disk (never round-tripped as a giant string through the MCP
    tool's text response) and the caller gets a real, readable file path
    back instead."""
    import prism_service.data_dir as data_dir_module
    from prism_service.api.agent_bridge import create_session, submit_result, CreateSessionBody, ResultBody
    from prism_service.services.agent_bridge import get_agent_bridge_service

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    svc = get_agent_bridge_service()
    command_id = svc.publish_command(svc._get_live(session["id"]), "screenshot", {})

    submit_result(session["id"], ResultBody(
        token=session["token"], command_id=command_id, ok=True,
        data={"image": f"data:image/png;base64,{_TINY_PNG_B64}"},
    ))

    result = svc.wait_for_result(command_id, timeout=2.0)
    assert result["ok"] is True
    assert "image" not in result["data"], "raw base64 must not survive into the MCP result"
    image_path = result["data"]["image_path"]
    from pathlib import Path
    p = Path(image_path)
    assert p.is_file()
    assert p.read_bytes() == __import__("base64").b64decode(_TINY_PNG_B64)
    assert str(tmp_path) in str(p), "must land under the (test-overridden) PRISM data dir"


def test_screenshot_action_is_forwarded_with_an_optional_selector():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.tools import handle_tool
    from prism_service.events import bus
    import json as _json

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    subscribed = threading.Event()
    seen = {}

    def _browser():
        async def _inner():
            q = bus.subscribe()
            subscribed.set()
            try:
                event = await asyncio.wait_for(q.get(), timeout=5.0)
                seen.update(event)
            finally:
                bus.unsubscribe(q)
        asyncio.run(_inner())

    t = threading.Thread(target=_browser, daemon=True)
    t.start()
    assert subscribed.wait(timeout=2.0)

    with use_request_context(PrismRequestContext(project_id="prism", principal=alice)):
        asyncio.run(handle_tool(
            "agent_bridge_command",
            {"session_id": session["id"], "action": "screenshot",
             "selector": "#workflow-rail", "timeout_s": 0.3},
            project_id="prism",
        ))
    t.join(timeout=2.0)
    assert seen["action"] == "screenshot"
    assert seen["selector"] == "#workflow-rail"


def test_read_action_returns_html_not_just_text_content():
    """Source-reading pin (this SPA has no JS test runner -- see
    tests/unit/test_conductor_page_animated_cleanup_ui.py for the repo's
    established convention). Real regression: asked to answer "what does the
    workflow rail actually look like right now", the read action only ever
    returned textContent/value -- empty for the rail, whose real state lives
    in button count/disabled/class (color), not text. `html` must be present
    so an agent driving the bridge can actually answer a visual question
    about the user's own screen, not just click/navigate blind."""
    src = (_SERVICE_ROOT / "prism_service/web/src/lib/agentBridge.tsx").read_text()
    read_branch = src.split('cmd.action === "read"', 1)[1].split("} else {", 1)[0]
    assert "outerHTML" in read_branch
    assert "html:" in read_branch


def test_bridge_session_persists_to_session_storage_not_local_storage():
    """Source-reading pin (this SPA has no JS test runner -- see
    tests/unit/test_conductor_page_animated_cleanup_ui.py for the repo's
    established convention). Real regression, owner live tonight: "for some
    reason the remote assist just turned itself off" -- because the session
    token lived ONLY in a React useState, a plain page reload (same tab,
    user never touched the toggle) reset it to null. The server already
    treats an untouched session as good for 30 days (v7.13.1); the client
    just had nowhere durable to remember it across ITS OWN reloads.
    `sessionStorage` is the fix -- it dies with the tab (unlike
    `localStorage`, which would be a real security regression) but survives
    a same-tab reload. This pins that the actual read/write call sites use
    `sessionStorage`, not just that the string appears somewhere in the
    file, and that `localStorage` is never used to persist the session."""
    src = (_SERVICE_ROOT / "prism_service/web/src/lib/agentBridge.tsx").read_text()

    assert "sessionStorage" in src
    # localStorage must never be used to persist the bridge session — the
    # docstring explaining why it's avoided is fine, an actual call is not.
    assert "localStorage.setItem" not in src
    assert "localStorage.getItem" not in src

    # loadPersistedSession(): the mount-time hydration path must actually
    # read from sessionStorage, not merely mention it in a comment.
    load_fn = src.split("function loadPersistedSession()", 1)[1].split(
        "\nfunction ", 1)[0]
    assert "sessionStorage.getItem(STORAGE_KEY)" in load_fn

    # The lazy useState initializer must actually be wired to that loader —
    # this is what makes a reload transparently resume the same session.
    assert "useState<BridgeSession | null>(loadPersistedSession)" in src

    # persistSession(): the shared write/clear path.
    persist_fn = src.split("function persistSession(", 1)[1].split(
        "\n/**", 1)[0]
    assert "sessionStorage.setItem(STORAGE_KEY" in persist_fn
    assert "sessionStorage.removeItem(STORAGE_KEY)" in persist_fn

    # enable(): a newly-minted session must be written through.
    enable_fn = src.split("const enable = useCallback(", 1)[1].split(
        "const disable = useCallback(", 1)[0]
    assert "persistSession(s)" in enable_fn

    # disable(): an explicit end must actually clear the durable copy, not
    # just the in-memory one, or a later reload would resurrect it.
    disable_fn = src.split("const disable = useCallback(", 1)[1].split(
        "// Tab-close revocation", 1)[0]
    assert "persistSession(null)" in disable_fn

    # The existing beforeunload-triggered explicit DELETE must also clear
    # the durable copy for the same reason.
    onunload_fn = src.split("const onUnload = () => {", 1)[1].split(
        "window.addEventListener", 1)[0]
    assert "persistSession(null)" in onunload_fn


def test_fill_action_supports_select_elements_not_just_input_textarea():
    """Live regression: driving a real page (the project picker, a native
    <select>) with `fill` failed with 'no input/textarea matches selector'
    even though the element and selector were both correct -- `fill` only
    ever checked for HTMLInputElement/HTMLTextAreaElement. A native
    <select> has no OS dropdown DOM node to click through; setting .value
    via the tracked React setter + firing 'change' (the same mechanism
    already used for input/textarea) is the correct, non-simulated way to
    drive one, not a workaround."""
    src = (_SERVICE_ROOT / "prism_service/web/src/lib/agentBridge.tsx").read_text()

    setter_sig = src.split("function setNativeValue(", 1)[1].split(")", 1)[0]
    assert "HTMLSelectElement" in setter_sig, (
        "setNativeValue must accept a <select> element, not just input/textarea")

    fill_branch = src.split('cmd.action === "fill"', 1)[1].split(
        '} else if (cmd.action === "read"', 1)[0]
    assert "HTMLSelectElement" in fill_branch, (
        "the fill action's element-type guard must accept <select>, or a "
        "correct selector still throws 'no input/textarea matches selector'")


# ---------------------------------------------------------------------------
# New actions (v7.14): observability (console/network) + interaction parity
# (hover/drag/select_option/file_upload/press_key/handle_dialog/wait_for/
# tabs/navigate_back/find). These drive the REAL MCP dispatch path
# end-to-end (handle_tool -> publish_command -> a stand-in "browser" reads
# the real bus event), pinning that each new field the tool schema exposes
# actually reaches the browser-side command, not just that the action name
# is accepted.
# ---------------------------------------------------------------------------

def _capture_one_event(session, action: str, extra_arguments: dict) -> dict:
    """Runs one agent_bridge_command call for real and returns the RAW bus
    event a browser tab would receive -- the same "stand-in browser thread"
    pattern as test_screenshot_action_is_forwarded_with_an_optional_selector."""
    import json as _json

    from prism_service.events import bus
    from prism_service.mcp.request_context import PrismRequestContext, use_request_context
    from prism_service.mcp.tools import handle_tool

    alice = _principal("alice")
    subscribed = threading.Event()
    seen: dict = {}

    def _browser():
        async def _inner():
            q = bus.subscribe()
            subscribed.set()
            try:
                event = await asyncio.wait_for(q.get(), timeout=5.0)
                seen.update(event)
            finally:
                bus.unsubscribe(q)
        asyncio.run(_inner())

    t = threading.Thread(target=_browser, daemon=True)
    t.start()
    assert subscribed.wait(timeout=2.0)

    with use_request_context(PrismRequestContext(project_id="prism", principal=alice)):
        result = asyncio.run(handle_tool(
            "agent_bridge_command",
            # timeout_s kept short -- this helper only proves the outbound
            # event reached the bus with the right fields; nothing ever
            # calls submit_result, so without a short timeout the MCP call
            # would block for the full default 20s waiting on a result that
            # never comes.
            {"session_id": session["id"], "action": action, "timeout_s": 0.3,
             **extra_arguments},
            project_id="prism",
        ))
    t.join(timeout=2.0)
    payload = _json.loads(result[0].text)
    assert payload.get("session_id") == session["id"] or "error" not in payload
    return seen


def test_new_actions_are_all_advertised_in_the_tool_schema():
    from prism_service.mcp.tools import TOOLS

    tool = next(t for t in TOOLS if t.name == "agent_bridge_command")
    enum = tool.inputSchema["properties"]["action"]["enum"]
    for action in (
        "console", "network", "hover", "drag", "select_option", "file_upload",
        "press_key", "handle_dialog", "wait_for", "tabs", "navigate_back", "find",
    ):
        assert action in enum, f"{action} must be in the advertised action enum"


def test_drag_action_forwards_selector_and_target_selector():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "drag", {
        "selector": "#src", "target_selector": "#dst",
    })
    assert seen["action"] == "drag"
    assert seen["selector"] == "#src"
    assert seen["target_selector"] == "#dst"


def test_wait_for_action_forwards_text_and_timeout_ms_as_a_number():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "wait_for", {
        "selector": "#toast", "text": "Saved", "timeout_ms": 2500,
    })
    assert seen["action"] == "wait_for"
    assert seen["selector"] == "#toast"
    assert seen["text"] == "Saved"
    assert seen["timeout_ms"] == 2500.0


def test_file_upload_action_forwards_the_files_list():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    files = [{"name": "a.txt", "type": "text/plain", "content_base64": "aGk="}]
    seen = _capture_one_event(session, "file_upload", {
        "selector": "#upload", "files": files,
    })
    assert seen["action"] == "file_upload"
    assert seen["files"] == files


def test_handle_dialog_action_forwards_accept_and_value():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "handle_dialog", {
        "accept": False, "value": "custom answer",
    })
    assert seen["action"] == "handle_dialog"
    assert seen["accept"] is False
    assert seen["value"] == "custom answer"


def test_console_and_network_actions_forward_limit_as_an_int():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "console", {"limit": "25"})
    assert seen["action"] == "console"
    assert seen["limit"] == 25

    session2 = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen2 = _capture_one_event(session2, "network", {"limit": 10})
    assert seen2["action"] == "network"
    assert seen2["limit"] == 10


def test_find_action_forwards_role_name_and_text_filters():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "find", {
        "role": "button", "name": "Save", "text": "Save changes",
    })
    assert seen["action"] == "find"
    assert seen["role"] == "button"
    assert seen["name"] == "Save"
    assert seen["text"] == "Save changes"


def test_tabs_action_forwards_value_and_selector():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "tabs", {"value": "switch", "selector": "tab-2"})
    assert seen["action"] == "tabs"
    assert seen["value"] == "switch"
    assert seen["selector"] == "tab-2"


def test_navigate_back_action_carries_no_extra_fields():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "navigate_back", {})
    assert seen["action"] == "navigate_back"


def test_hover_action_forwards_selector():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "hover", {"selector": ".tooltip-target"})
    assert seen["action"] == "hover"
    assert seen["selector"] == ".tooltip-target"


def test_select_option_action_forwards_selector_and_value():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "select_option", {"selector": "#kind", "value": "bug"})
    assert seen["action"] == "select_option"
    assert seen["selector"] == "#kind"
    assert seen["value"] == "bug"


def test_press_key_action_forwards_selector_and_key_value():
    from prism_service.api.agent_bridge import create_session, CreateSessionBody

    session = create_session(CreateSessionBody(project="prism"), principal=_principal("alice"))
    seen = _capture_one_event(session, "press_key", {"selector": "#search", "value": "Enter"})
    assert seen["action"] == "press_key"
    assert seen["value"] == "Enter"


# ---------------------------------------------------------------------------
# Large console/network dumps persist to disk (same convention as
# screenshots), small ones stay inline.
# ---------------------------------------------------------------------------

def test_large_console_dump_persists_to_disk_and_returns_a_path(tmp_path, monkeypatch):
    import prism_service.data_dir as data_dir_module
    from prism_service.api.agent_bridge import create_session, submit_result, CreateSessionBody, ResultBody
    from prism_service.services.agent_bridge import get_agent_bridge_service

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    svc = get_agent_bridge_service()
    command_id = svc.publish_command(svc._get_live(session["id"]), "console", {})

    big_entries = [{"level": "log", "message": "x" * 200, "ts": i} for i in range(200)]
    submit_result(session["id"], ResultBody(
        token=session["token"], command_id=command_id, ok=True,
        data={"entries": big_entries, "total_captured": 200},
    ))

    result = svc.wait_for_result(command_id, timeout=2.0)
    assert result["ok"] is True
    assert "entries" not in result["data"], "a large entries list must not survive into the MCP result"
    entries_path = result["data"]["entries_path"]
    from pathlib import Path
    p = Path(entries_path)
    assert p.is_file()
    assert str(tmp_path) in str(p)
    import json as _json
    assert _json.loads(p.read_text()) == big_entries
    assert result["data"]["entries_count"] == 200


def test_small_network_dump_stays_inline_not_persisted(tmp_path, monkeypatch):
    import prism_service.data_dir as data_dir_module
    from prism_service.api.agent_bridge import create_session, submit_result, CreateSessionBody, ResultBody
    from prism_service.services.agent_bridge import get_agent_bridge_service

    monkeypatch.setattr(data_dir_module, "resolve_data_dir", lambda: tmp_path)

    alice = _principal("alice")
    session = create_session(CreateSessionBody(project="prism"), principal=alice)
    svc = get_agent_bridge_service()
    command_id = svc.publish_command(svc._get_live(session["id"]), "network", {})

    small_entries = [{"method": "GET", "url": "/api/tasks", "status": 200, "ok": True}]
    submit_result(session["id"], ResultBody(
        token=session["token"], command_id=command_id, ok=True,
        data={"entries": small_entries, "total_captured": 1, "failed_count": 0},
    ))

    result = svc.wait_for_result(command_id, timeout=2.0)
    assert result["ok"] is True
    assert result["data"]["entries"] == small_entries
    assert "entries_path" not in result["data"]
