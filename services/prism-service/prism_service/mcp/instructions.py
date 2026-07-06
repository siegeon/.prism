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
"""


def build_instructions() -> str:
    """Compact server instructions: what PRISM is + role/tier doctrine."""
    return _PREAMBLE + "\n" + roles.doctrine()


PRISM_INSTRUCTIONS = build_instructions()
