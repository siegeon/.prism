"""pi-agent subprocess primitive — PRISM's internal agentic inference
(task ac69ee28).

Shells out to web/pi-runtime.mjs (Node + pi-agent-core, MIT) the same way
claude_cli shells to `claude`: one JSON job on stdin, one JSON result on
stdout. The agent runs the LOCAL micro model (PRISM_LOCAL_LLM_MODEL /
PRISM_LOCAL_LLM_URL — bench-ranked in benchmarks/micro_llm_selflearn) with
PRISM tools bridged through the whitelisted /api/agent/tool surface, so
internal inference can GROUND itself in memory/brain before answering.
Keyless by construction (INV-1).

Env:
  PRISM_PI_RUNNER   override the runner path (tests point it at a fake)
  PRISM_PI_NODE     override the node executable
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from prism_service.inference import local_llm

_CREATE_NO_WINDOW = 0x08000000  # keep worker spawns invisible (v6.7.25 lesson)

DEFAULT_TOOLS: tuple[str, ...] = ("brain_search", "memory_recall")

# PI ships pre-loaded as the PRISM expert (task e70cdcda): the FULL
# catalog mirrored from web/pi-expert.mjs (EXPERT_TOOL_DEFS) and
# api/agent.py (AGENT_TOOL_WHITELIST) — the three move together. Opt-in
# per job; reflection keeps the lean DEFAULT_TOOLS above.
EXPERT_TOOLS: tuple[str, ...] = (
    "brain_search", "brain_understand", "brain_find_symbol",
    "brain_outline", "brain_find_references", "brain_call_chain",
    "memory_recall", "memory_store", "memory_invalidate",
    "task_list", "task_next", "task_create", "task_update",
    "conductor_advance", "conductor_gate",
    "workflow_state", "context_bundle", "prism_status",
)

# Conductor surface (task 9f20b605, phase 2): OPT-IN toolset for jobs
# DIRECTED at SDLC work — read the task (task_list by id), author/patch
# plan_doc (task_update), advance the state machine (conductor_advance),
# propose gate evidence (conductor_gate). Never part of DEFAULT_TOOLS;
# the bridge additionally gates the mutating three to internal callers.
CONDUCTOR_TOOLS: tuple[str, ...] = (
    "task_list", "task_update", "conductor_advance", "conductor_gate",
)


class PiRuntimeError(RuntimeError):
    """Runner could not be spawned or returned an unparseable result."""


def _runner_path() -> Path:
    override = os.environ.get("PRISM_PI_RUNNER")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "web" / "pi-runtime.mjs"


def _node_exe() -> str:
    exe = os.environ.get("PRISM_PI_NODE") or shutil.which("node")
    if not exe:
        raise PiRuntimeError("node executable not found (install Node or set PRISM_PI_NODE)")
    return exe


def _ui_port() -> int:
    from prism_service.config import UI_PORT
    return UI_PORT


def invoke(
    prompt: str,
    *,
    system: str = "",
    allowed_tools: tuple[str, ...] = DEFAULT_TOOLS,
    model: str = "",
    base_url: str = "",
    project: str = "default",
    max_turns: int = 6,
    timeout: float = 300.0,
) -> dict:
    """Run one agentic job. Returns the runner's result dict:
    {ok, text, turns, tools_used[{name,ms,ok}], ms, tokens, model, error?}.
    Raises PiRuntimeError on spawn/parse failure (model failures come back
    as ok=False in the payload instead)."""
    runner = _runner_path()
    if not runner.exists():
        raise PiRuntimeError(f"pi runner missing: {runner}")
    job = {
        "prompt": prompt,
        "system": system,
        "model": model or local_llm.configured_model(),
        "base_url": base_url or local_llm.configured_url(),
        "api_base": f"http://127.0.0.1:{_ui_port()}",
        "project": project,
        "allowed_tools": list(allowed_tools),
        "max_turns": max_turns,
    }
    kwargs: dict = {}
    if sys.platform.startswith("win"):
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    try:
        proc = subprocess.run(
            [_node_exe(), str(runner)],
            input=json.dumps(job),
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout, cwd=str(runner.parent),
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PiRuntimeError(f"pi runner spawn failed: {exc}") from exc
    out = (proc.stdout or "").strip()
    try:
        result = json.loads(out)
    except ValueError as exc:
        raise PiRuntimeError(
            f"pi runner returned non-JSON (exit {proc.returncode}): "
            f"{out[:300]!r} stderr={proc.stderr[:300]!r}"
        ) from exc
    if not isinstance(result, dict):
        raise PiRuntimeError(f"pi runner returned {type(result).__name__}, expected object")
    return result
