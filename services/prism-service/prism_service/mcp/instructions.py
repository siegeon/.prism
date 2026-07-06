"""MCP server instructions returned to clients on connect.

Kept out of server.py so the literal stays legible. Model-AGNOSTIC by design:
the role/tier doctrine is appended from models.roles.doctrine() (tiers, never
model names) so any model/harness can honor it.
"""

from __future__ import annotations

from prism_service.models import roles

_PREAMBLE = """\
PRISM is an on-prem engineering substrate for coding agents. It brings three
things to the table, all over this one MCP endpoint:
- MEMORY: MCP-owned, per-project project knowledge (Brain code-graph + curated
  Understand memory) — read it before you grep; write decisions back.
- CONTEXT: a deterministic context layer (context_bundle) — role card, rules,
  live tasks, and workflow state assembled the same way every time.
- SDLC: a STATE-MACHINE-ENFORCED delivery flow — the conductor gates each step
  (story -> plan -> red -> green) and refuses self-review at the gate.
Start a session with prism_guide; onboard a fresh project with prism_onboard.

CONDUCTOR DRIVER CONTRACT (every SDLC skill/workflow MUST honor this):
The conductor is DRIVER-ADVANCED — the daemon NEVER self-advances. A task moves
only while YOU drive it (conductor_advance / conductor_gate). If you stop, it
FREEZES wherever it was and does not self-heal. So any skill/workflow doing SDLC
work MUST:
  1. Keep the driving session linked (task_link_session); a sessionless
     in_progress is refused.
  2. Advance/gate at EVERY step boundary — never leave a task parked mid-step
     after its work is done. When a fan-out's units all return, FAN IN and
     advance; completion is a signal to MOVE, not to sit.
  3. Report real activity (e.g. POST /api/conductor/fanout) — work done in
     sub-agents is INVISIBLE to the daemon unless you report it, so the tile
     shows the truth (working) instead of adrift/stalled.
  4. Never leave a task un-driven: on pause/failure, advance, mark it blocked
     (with reason), or hand the drive to another session — never a silent stall.
A stalled/adrift task is a DRIVER bug, not a daemon bug. The honest activity
states (working / awaiting_gate / adrift / stalled) exist to surface violations.
"""


def build_instructions() -> str:
    """Compact server instructions: what PRISM is + role/tier doctrine."""
    return _PREAMBLE + "\n" + roles.doctrine()


PRISM_INSTRUCTIONS = build_instructions()
