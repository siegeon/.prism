"""UI contract tests for task 4f5cc773 -- agent-bridge `console` and
`network` observability actions.

The PRISM SPA has NO JS test runner, so these are pinned by asserting the
ACTUAL rendered source in lib/agentBridge.tsx, the same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py and the existing
agent-bridge source-reading tests in tests/integration/test_agent_bridge_flow.py.

Every assertion here is a plain `needle in haystack` membership check, never
a raw `str.index()`/`str.find()`-then-slice lookup -- the actions under test
do not exist in this worktree yet, so an index-style lookup would raise an
uncaught ValueError before any assertion ran. This suite is meant to FAIL
via a genuine AssertionError (pytest rc=1) until the console/network actions
land, per this repo's red-gate convention (rc=1 is the only outcome the
verifier counts as red demonstrated; rc=2/4 -- collection error / uncaught
exception -- does not count).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src" / "lib" / "agentBridge.tsx"


def _read() -> str:
    return _SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# AC-4 (partial, static half): the two new actions are declared on the
# BridgeCommand.action union at all.
# ---------------------------------------------------------------------------

def test_bridge_command_action_union_includes_console_and_network():
    src = _read()
    assert '"console"' in src, (
        'BridgeCommand.action union must include "console" -- not present yet'
    )
    assert '"network"' in src, (
        'BridgeCommand.action union must include "network" -- not present yet'
    )


# ---------------------------------------------------------------------------
# AC-3: capture installs at MODULE LOAD, unconditionally -- a driver that
# enables Remote Assist, navigates, THEN calls `console` must still see what
# fired during the navigation (not just what happens after the first call).
# ---------------------------------------------------------------------------

def test_observability_capture_installs_unconditionally_at_module_load():
    src = _read()
    assert "installObservability();" in src, (
        "installObservability() must be CALLED at module scope (not only "
        "defined), so capture starts recording before any bridge session or "
        "`console`/`network` call even exists -- no such call found"
    )
    assert "export function AgentBridgeProvider" in src
    call_idx = src.find("installObservability();")
    provider_idx = src.find("export function AgentBridgeProvider")
    assert call_idx != -1 and provider_idx != -1 and call_idx < provider_idx, (
        "installObservability() must run before AgentBridgeProvider is even "
        "defined -- i.e. at module import time, not component mount time"
    )


def test_console_capture_wraps_console_methods_and_global_error_handlers():
    src = _read()
    assert "function installObservability" in src, (
        "no installObservability() function defined yet -- console capture "
        "is not implemented"
    )
    body_start = src.find("function installObservability")
    install_call = src.find("installObservability();")
    assert install_call != -1 and body_start != -1 and body_start < install_call
    body = src[body_start:install_call]
    for method in ("log", "warn", "error"):
        assert f'"{method}"' in body, f'console.{method} must be wrapped, "{method}" not found'
    assert 'addEventListener("error"' in body, (
        "must capture window.onerror-style script errors, not just explicit "
        "console.* calls"
    )
    assert 'addEventListener("unhandledrejection"' in body, (
        "must capture unhandled promise rejections too"
    )


# ---------------------------------------------------------------------------
# AC-1: `console` action returns real captured entries from a ring buffer
# that has been recording since load, not a fresh/empty/stubbed capture.
# ---------------------------------------------------------------------------

def test_console_action_dispatch_branch_exists():
    src = _read()
    assert 'cmd.action === "console"' in src, (
        'no `cmd.action === "console"` dispatch branch -- the console action '
        "is not wired into agentBridge.tsx's command switch yet"
    )


def test_console_action_reads_from_the_ring_buffer_not_a_fresh_capture():
    src = _read()
    assert "consoleLog" in src, (
        "the `console` action must read a module-level ring buffer "
        "(consoleLog) that has been recording since module load, not start "
        "capturing only from the moment it's first called -- consoleLog not "
        "found anywhere in the file"
    )


# ---------------------------------------------------------------------------
# AC-2: `network` action returns real captured requests with status codes,
# flagging 4xx/5xx.
# ---------------------------------------------------------------------------

def test_network_action_dispatch_branch_exists():
    src = _read()
    assert 'cmd.action === "network"' in src, (
        'no `cmd.action === "network"` dispatch branch -- the network action '
        "is not wired into agentBridge.tsx's command switch yet"
    )


def test_network_capture_wraps_both_fetch_and_xhr():
    src = _read()
    assert "function installObservability" in src, (
        "no installObservability() function defined yet -- network capture "
        "is not implemented"
    )
    body_start = src.find("function installObservability")
    install_call = src.find("installObservability();")
    assert install_call != -1 and body_start != -1 and body_start < install_call
    body = src[body_start:install_call]
    assert "window.fetch = " in body, "must patch window.fetch to capture fetch-based requests"
    assert "XMLHttpRequest" in body, "must also cover XMLHttpRequest-based requests, not fetch-only"


def test_network_action_reads_from_the_ring_buffer_and_flags_4xx_5xx():
    src = _read()
    assert "networkLog" in src, (
        "the `network` action must read a module-level ring buffer "
        "(networkLog) that has been recording since module load -- "
        "networkLog not found anywhere in the file"
    )
    assert "failed_count" in src, (
        "network entries must be flagged for 4xx/5xx failures via a "
        "failed_count -- not found anywhere in the file"
    )
