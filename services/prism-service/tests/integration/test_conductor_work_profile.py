"""conductor_work default-profile surface contract (task 7b219546).

The inverted-queue slice's MACHINE-VERIFIABLE half: the single loop verb
`conductor_work` joins the DEFAULT interactive tool profile, and the four
driver-pushed verbs it supersedes (workflow_state, workflow_advance,
conductor_advance, conductor_gate) are DEMOTED to tool_profile=all. Net effect:
the default MCP surface shrinks by 3 routes. This is the honestly-closeable
scope of the task; the auto-driven honest-green demo is split to a follow-up.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_DEMOTED = (
    "workflow_state",
    "workflow_advance",
    "conductor_advance",
    "conductor_gate",
)


def test_conductor_work_is_on_default_surface():
    import prism_service.mcp.tools as t

    assert "conductor_work" in t.INTERACTIVE_TOOL_NAMES, (
        "conductor_work must be the single default-surface loop verb")


def test_driver_verbs_demoted_out_of_default():
    import prism_service.mcp.tools as t

    leaked = [n for n in _DEMOTED if n in t.INTERACTIVE_TOOL_NAMES]
    assert leaked == [], (
        f"these driver verbs must be tool_profile=all, not default: {leaked}")
