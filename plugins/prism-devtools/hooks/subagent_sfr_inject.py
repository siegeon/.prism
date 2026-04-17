#!/usr/bin/env python3
"""SubagentStart hook — inject SFR certificate template + Brain context.

Fires when Claude spawns a sub-agent via the Agent tool. Reads agent_name
from stdin JSON, queries Conductor for an SFR certificate variant
(epsilon-greedy on validator/* namespace), fetches Brain context for the
agent's domain, and outputs hookSpecificOutput.additionalContext so the
content is injected into the sub-agent's context window.

Output schema (JSON, stdout):
    {"hookSpecificOutput": {"additionalContext": "<combined context>"}}

Empty additionalContext is valid — freeform reasoning, no SFR template.
Fails silently (exit 0) on any error so the sub-agent always starts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _ensure_hook_dir_on_path() -> None:
    hook_dir = str(Path(__file__).parent)
    if hook_dir not in sys.path:
        sys.path.insert(0, hook_dir)


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    agent_name = (
        payload.get("agent_name")
        or payload.get("agentName")
        or ""
    )

    if not agent_name:
        # No agent name — output empty additionalContext and exit
        print(json.dumps({"hookSpecificOutput": {"additionalContext": ""}}))
        sys.exit(0)

    _ensure_hook_dir_on_path()

    additional_context = ""
    try:
        from conductor_engine import Conductor

        conductor = Conductor()
        prompt_id, sfr_template = conductor._select_subagent_variant(agent_name)
        brain_ctx = conductor._subagent_brain_context(agent_name)

        # Persist selected variant so SubagentStop recorder can read it
        if prompt_id:
            conductor._save_subagent_prompt_id(agent_name, prompt_id)

        parts = []
        if sfr_template:
            parts.append(sfr_template)
        if brain_ctx:
            parts.append(brain_ctx)

        additional_context = "\n\n".join(parts)
    except Exception as exc:
        print(
            f"subagent_sfr_inject: Conductor unavailable ({exc})",
            file=sys.stderr,
        )

    print(json.dumps({"hookSpecificOutput": {"additionalContext": additional_context}}))
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        print(f"subagent_sfr_inject: fatal ({exc})", file=sys.stderr)
        sys.exit(0)
