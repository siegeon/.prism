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
  (story -> plan -> red -> green) and refuses self-review at the gate. Work it
  as a SERVER-DRIVEN QUEUE: loop on the single verb `conductor_work` — the
  server owns the step sequence, so you never name a step:
    job = conductor_work()            # omit id -> server picks the next task
    while not job["done"]:
        # do EXACTLY job["instructions"], produce job["expected_proof"]
        job = conductor_work(id=job["task_id"], outcome="pass", proof=<artifact>)
  A gate job is decided by a DISTINCT actor (the producing session cannot clear
  its own gate). conductor_advance/conductor_gate/workflow_state still exist for
  admin/debug behind tool_profile=all, but the loop verb supersedes them.
- CLAIM EARLY: the moment you start working a task — BEFORE reading or
  researching it — call task_update(id, status="in_progress") to claim it.
  This is free (no worktree, no conductor_service.py involvement) and makes
  the task visible on the Work board and the Conductor page's intake lane
  right away, instead of staying invisible until conductor_work's first
  call. It is distinct from conductor_work, which enters the graded SDLC.
Start a session with prism_guide; onboard a fresh project with prism_onboard.
"""


def build_instructions() -> str:
    """Compact server instructions: what PRISM is + role/tier doctrine."""
    return _PREAMBLE + "\n" + roles.doctrine()


PRISM_INSTRUCTIONS = build_instructions()
