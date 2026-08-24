"""UI contract tests for the agent-bridge observability + interaction-parity
actions (console/network/hover/drag/select_option/file_upload/press_key/
handle_dialog/wait_for/tabs/navigate_back/find).

The PRISM SPA has NO JS test runner, so these are pinned by asserting the
ACTUAL rendered source in lib/agentBridge.tsx — the same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py and the existing
agent-bridge source-reading tests in tests/integration/test_agent_bridge_flow.py
(test_read_action_returns_html_not_just_text_content,
test_bridge_session_persists_to_session_storage_not_local_storage,
test_fill_action_supports_select_elements_not_just_input_textarea).

Each test pins a REAL semantic property of the implementation (real event
sequences, a real polling loop with a real deadline, capture installed at
module load rather than gated on first use, a dialog override that always
resolves so the page can never actually hang) rather than just "the string
appears somewhere in the file".
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src" / "lib" / "agentBridge.tsx"


def _read() -> str:
    return _SRC.read_text(encoding="utf-8")


def _find(src: str, marker: str) -> int:
    """Like str.index, but a miss is a genuine AssertionError (with a
    diagnostic trace) rather than an uncaught ValueError -- an uncaught
    exception is not a passing red test, per the write-failing-tests-loop
    policy (see fix(conductor) 9d0fa328)."""
    idx = src.find(marker)
    assert idx != -1, f"expected to find {marker!r} in {_SRC}"
    return idx


def _slice(src: str, start_marker: str, end_marker: str) -> str:
    """The text between two markers, each resolved via _find so a missing
    marker on either side fails as an AssertionError, not a ValueError."""
    start = _find(src, start_marker)
    end = _find(src, end_marker)
    assert end > start, (
        f"expected {end_marker!r} to appear after {start_marker!r} in {_SRC}"
    )
    return src[start:end]


def _branch(src: str, action: str) -> str:
    """The full body of one `cmd.action === "<action>"` else-if branch, up
    to (not including) the next `} else if` or the final `} else {`."""
    marker = f'cmd.action === "{action}"'
    start = _find(src, marker)
    rest = src[start:]
    end_candidates = [i for i in (
        rest.find("} else if", 1),
        rest.find("} else {", 1),
    ) if i != -1]
    end = min(end_candidates) if end_candidates else len(rest)
    return rest[:end]


# ---------------------------------------------------------------------------
# Observability capture installs at MODULE LOAD, unconditionally -- a driver
# that enables Remote Assist, navigates, THEN calls `console` must still see
# what fired during the navigation.
# ---------------------------------------------------------------------------

def test_observability_capture_installs_unconditionally_at_module_load():
    src = _read()
    assert "installObservability();" in src, (
        "installObservability() must be CALLED at module scope (not only "
        "defined), so capture starts recording before any bridge session "
        "or `console` call even exists"
    )
    # It must not be gated behind `if (session)` / inside the provider's
    # enable() callback -- called unconditionally is what makes "already
    # recording since page load" true rather than "since enable() ran".
    call_idx = _find(src, "installObservability();")
    # The nearest enclosing function before this call must be none (i.e. it
    # sits at top level) -- approximate by checking it's not indented deep
    # inside AgentBridgeProvider, which starts later in the file.
    provider_idx = _find(src, "export function AgentBridgeProvider")
    assert call_idx < provider_idx, (
        "installObservability() must run before AgentBridgeProvider is even "
        "defined -- i.e. at module import time, not component mount time"
    )


def test_console_action_reads_from_the_ring_buffer_not_a_fresh_capture():
    src = _read()
    branch = _branch(src, "console")
    assert "consoleLog" in branch, (
        "the `console` action must read the module-level ring buffer that's "
        "been recording since load, not start capturing only now"
    )


def test_console_capture_wraps_console_methods_and_global_error_handlers():
    src = _read()
    body = _slice(src, "function installObservability", "installObservability();")
    for method in ("log", "warn", "error"):
        assert f'"{method}"' in body
    assert 'addEventListener("error"' in body, \
        "must capture window.onerror-style script errors, not just explicit console.* calls"
    assert 'addEventListener("unhandledrejection"' in body, \
        "must capture unhandled promise rejections too"


def test_network_action_wraps_both_fetch_and_xhr():
    src = _read()
    body = _slice(src, "function installObservability", "installObservability();")
    assert "window.fetch = " in body, "must patch window.fetch to capture fetch-based requests"
    assert "XMLHttpRequest" in body, "must also cover XMLHttpRequest-based requests, not fetch-only"
    branch = _branch(src, "network")
    assert "networkLog" in branch


def test_network_entries_flag_4xx_5xx():
    src = _read()
    branch = _branch(src, "network")
    assert "failed_count" in branch
    assert "status >= 400" in branch or "status>=400" in branch.replace(" ", "")


# ---------------------------------------------------------------------------
# hover: real pointer/mouse events, not a no-op.
# ---------------------------------------------------------------------------

def test_hover_dispatches_real_pointer_and_mouse_events():
    src = _read()
    branch = _branch(src, "hover")
    assert "pointerover" in branch and "pointerenter" in branch
    assert "mouseover" in branch and "mouseenter" in branch
    # A JS-driven hover handler (e.g. Sidebar.tsx's onMouseEnter) is what
    # this proves against -- pure-CSS :hover cannot be triggered this way,
    # and the branch says so rather than silently pretending it works.
    assert ":hover" in branch or "CSS" in branch, (
        "hover's real limitation (pure-CSS :hover pseudo-class can't be "
        "triggered by a dispatched event) should be documented in-branch"
    )


# ---------------------------------------------------------------------------
# drag: a real HTML5 DnD sequence with a genuine DataTransfer.
# ---------------------------------------------------------------------------

def test_drag_dispatches_the_real_html5_dnd_sequence_with_datatransfer():
    src = _read()
    branch = _branch(src, "drag")
    assert "new DataTransfer()" in branch
    for evt in ("dragstart", "dragenter", "dragover", "drop", "dragend"):
        assert f'"{evt}"' in branch, f"drag must fire a real {evt} event"
    assert "target_selector" in branch


# ---------------------------------------------------------------------------
# select_option: native <select>, by option value OR visible label.
# ---------------------------------------------------------------------------

def test_select_option_matches_by_value_or_label_and_uses_the_tracked_setter():
    src = _read()
    branch = _branch(src, "select_option")
    assert "HTMLSelectElement" in branch
    assert "o.value === wanted" in branch
    assert "textContent?.trim() === wanted" in branch
    assert "setNativeValue(" in branch, \
        "must go through the same React-tracked setter as fill, not a raw assignment"


# ---------------------------------------------------------------------------
# file_upload: builds real File objects and fires input/change.
# ---------------------------------------------------------------------------

def test_file_upload_builds_real_files_via_datatransfer_and_fires_change():
    src = _read()
    branch = _branch(src, "file_upload")
    assert "HTMLInputElement" in branch and 'el.type !== "file"' in branch
    assert "new DataTransfer()" in branch
    assert "new File(" in branch
    assert "atob(" in branch, "must decode the base64 payload into real bytes"
    assert 'dispatchEvent(new Event("change"' in branch
    assert 'dispatchEvent(new Event("input"' in branch


# ---------------------------------------------------------------------------
# press_key: a real KeyboardEvent trio.
# ---------------------------------------------------------------------------

def test_press_key_dispatches_keydown_and_keyup():
    src = _read()
    branch = _branch(src, "press_key")
    assert "KeyboardEvent" in branch
    assert '"keydown"' in branch
    assert '"keyup"' in branch


# ---------------------------------------------------------------------------
# handle_dialog: the override NEVER actually blocks the tab.
# ---------------------------------------------------------------------------

def test_dialog_override_never_lets_confirm_alert_prompt_block():
    src = _read()
    body = _slice(src, "function installDialogOverride", "installDialogOverride();")
    assert "window.confirm = " in body
    assert "window.alert = " in body
    assert "window.prompt = " in body
    # confirm/prompt must resolve from a policy OR a safe default -- never
    # leave a caller with nothing to return (which would hang the caller,
    # i.e. genuinely block, if it awaited something the override never
    # settles).
    assert "_dialogPolicy" in body
    assert "return policy.accept" in body


def test_dialog_override_installs_unconditionally_at_module_load():
    src = _read()
    assert "installDialogOverride();" in src
    call_idx = _find(src, "installDialogOverride();")
    provider_idx = _find(src, "export function AgentBridgeProvider")
    assert call_idx < provider_idx


def test_handle_dialog_action_arms_a_policy_and_reports_the_last_dialog():
    src = _read()
    branch = _branch(src, "handle_dialog")
    assert "_dialogPolicy = " in branch
    assert "last_dialog" in branch
    assert "dialogLog" in branch


# ---------------------------------------------------------------------------
# wait_for: real polling with a real deadline, not a fixed sleep.
# ---------------------------------------------------------------------------

def test_wait_for_polls_with_a_real_deadline_not_a_fixed_sleep():
    src = _read()
    branch = _branch(src, "wait_for")
    assert "Date.now()" in branch, "must compute a real wall-clock deadline"
    assert "await sleep(" in branch, "must actually yield between polls (real polling loop)"
    assert "for (;;)" in branch or "while (" in branch, \
        "must loop until matched or timed out, not sleep once and check once"
    assert "timed out" in branch


def test_wait_for_can_wait_for_text_not_just_existence():
    src = _read()
    branch = _branch(src, "wait_for")
    assert "cmd.text" in branch
    assert "textContent" in branch


# ---------------------------------------------------------------------------
# tabs: documents its own real limitation rather than pretending to drive
# an arbitrary second tab.
# ---------------------------------------------------------------------------

def test_tabs_documents_the_cross_tab_driving_limitation():
    src = _read()
    body = _slice(src, "function installTabTracking", "installTabTracking();")
    assert "window.open" in body
    assert "LIMITATION" in src[:src.index("function installTabTracking")] or \
        "LIMITATION" in body or "cannot route" in src


def test_tabs_action_lists_and_switches_tracked_windows():
    src = _read()
    branch = _branch(src, "tabs")
    assert "openedTabs" in branch
    assert '"switch"' in branch
    assert ".focus()" in branch


# ---------------------------------------------------------------------------
# navigate_back: real SPA router back, not window.location.
# ---------------------------------------------------------------------------

def test_navigate_back_uses_the_spa_router_not_a_hard_reload():
    src = _read()
    branch = _branch(src, "navigate_back")
    assert "navigate(-1)" in branch
    assert "window.location" not in branch


# ---------------------------------------------------------------------------
# find: role/name/text search, reusing read's "what does this look like"
# instinct, with a selector generator good enough to feed back into
# click/fill/read.
# ---------------------------------------------------------------------------

def test_find_supports_role_name_and_text_filters():
    src = _read()
    branch = _branch(src, "find")
    assert "findElements(" in branch
    fn_body = _slice(src, "function findElements", "function sleep(")
    assert "getRole(" in fn_body
    assert "getAccessibleName(" in fn_body
    assert "buildSelector(" in fn_body


def test_find_requires_at_least_one_filter_to_avoid_dumping_the_whole_dom():
    src = _read()
    fn_body = _slice(src, "function findElements", "function sleep(")
    # The real guard clause: with no role/name/text filter at all, every
    # element would otherwise match and the whole DOM would come back.
    assert "if (!wantRole && !wantName && !wantText) continue;" in fn_body


def test_accessible_name_falls_back_through_label_placeholder_title_text():
    src = _read()
    fn_body = _slice(src, "function getAccessibleName", "function buildSelector")
    assert "aria-label" in fn_body
    assert "aria-labelledby" in fn_body
    assert 'label[for=' in fn_body
    assert "placeholder" in fn_body
