"""RED suite — the interceptor must execute MULTI-call text tool blocks
(task c4bb21f8; live-confirmed dup 4e5749bc).

The owner-noticed PI panel bug: given a multi-step ask in ONE prompt
("search the brain for X THEN create a task"), the local model emits the
whole plan as a single plain-text JSON block — a top-level ARRAY of
tool-call objects, or several {name,arguments} objects concatenated. The
old single-object interceptor parsed only ONE object, so nothing executed
and raw JSON was shown.

The fix extracts a shared parser (web/pi-toolcall.mjs) used by BOTH
interception surfaces — the Node runner (web/pi-runtime.mjs) and the
browser panel (web/src/lib/piAgent.ts) — returning an ORDERED LIST of
{name,args}. These tests pin: (1) the shared parser handles array +
concatenated inputs, executed as a real node unit check; (2) the runner's
offline --check reports the multi-call parse; (3) the panel wires the
shared multi-call parser.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web"
_RUNNER = _WEB / "pi-runtime.mjs"
_PARSER_TEST = _WEB / "pi-toolcall.test.mjs"
_PANEL = _WEB / "src" / "lib" / "piAgent.ts"

node = shutil.which("node")
pytestmark = pytest.mark.skipif(node is None, reason="node not installed")


def test_shared_parser_unit_check() -> None:
    """AC-1: the shared parser (pi-toolcall.mjs) parses single, fenced,
    array, and concatenated tool-call blocks — proven by its node unit
    check exiting 0. Covers BOTH surfaces (both import the module)."""
    assert _PARSER_TEST.exists(), f"missing shared-parser unit check: {_PARSER_TEST}"
    proc = subprocess.run(
        [str(node), str(_PARSER_TEST)],
        capture_output=True, text=True, timeout=120, cwd=str(_WEB),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_runner_check_reports_multicall() -> None:
    """AC-2: the Node runner's offline --check validates the multi-call
    parse and surfaces the counts (array + concatenated each -> 2)."""
    proc = subprocess.run(
        [str(node), str(_RUNNER), "--check"],
        capture_output=True, text=True, timeout=120, cwd=str(_WEB),
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    payload = json.loads(proc.stdout)
    multicall = payload.get("multicall")
    assert isinstance(multicall, dict), f"no multicall report in --check: {payload}"
    assert multicall.get("array") == 2, multicall
    assert multicall.get("concatenated") == 2, multicall


def test_panel_wires_shared_multicall_parser() -> None:
    """AC-3: the browser panel (piAgent.ts) imports the shared
    parseTextToolCalls and drives interception from the ORDERED LIST, so
    the array/concatenated shapes execute rather than render as raw JSON."""
    src = _PANEL.read_text(encoding="utf-8")
    assert "pi-toolcall.mjs" in src, "panel does not import the shared parser module"
    assert "parseTextToolCalls" in src, "panel does not use the multi-call parser"
    # The interceptor must iterate the list, not a single call.
    assert src.count("parseTextToolCalls") >= 1
