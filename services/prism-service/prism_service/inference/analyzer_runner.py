"""Run one Understand-Anything analyzer via claude_cli.

Single shared executor used by both:
  * `prism_service.cli.understand_cli` — the foreground `prism understand drain` path
  * `prism_service.services.understand_drainer` — the server-side auto-drain loop

Owns the prompt loading, frontmatter strip, runtime-context block,
claude_cli.invoke call, and payload extraction. Returns a structured
dict the callers persist via `understand_artifact_store.put`.

INV-1 is enforced inside `claude_cli.invoke` — this module never
re-enables the stripped env vars.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from prism_service.inference import claude_cli, pi_agent
from prism_service.services import source_service as ss


_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
_log = logging.getLogger("prism.analyzer_runner")

# Analyzers walk the source tree (no Brain MCP available in the
# server-side drain context) so they need more turns than the harness
# default. Budget caps still bound aggregate spend.
DEFAULT_MAX_TURNS = 35

# Read-only file tools an Understand analyzer needs to walk the source tree.
# These are the claude_cli READ_ONLY_TOOLS; the pi runtime does NOT bridge
# them today (task 96341ef8 — see _pi_has_fs_tools).
ANALYZER_FS_TOOLS = ("Read", "Glob", "Grep")


def _analyzer_backend() -> str:
    """Configured analyzer inference backend. '' (unset/any other value)
    means the default claude_cli path; 'pi' opts into the pi-agent runtime
    (task 96341ef8, claude-p-exit epic be898578)."""
    return (os.environ.get("PRISM_ANALYZER_BACKEND") or "").strip().lower()


def _pi_has_fs_tools() -> bool:
    """True iff the pi runtime advertises the Read/Glob/Grep file tools an
    analyzer needs.

    The pi runtime bridges Brain tools through the /api/agent/tool surface
    (its catalog lives in web/pi-runtime.mjs + web/pi-expert.mjs), NOT the
    Read/Glob/Grep file tools. An analyzer is a ~35-turn file-walk, so
    PRISM_ANALYZER_BACKEND=pi is only viable once those fs tools join the pi
    catalog — tracked as a follow-up (web/** is outside this task's allowed
    files). Detection is by membership so the day they land, the pi path
    lights up here with no further change; until then run_analyzer falls
    back to claude with a logged skip note."""
    catalog = (set(getattr(pi_agent, "EXPERT_TOOLS", ()))
               | set(getattr(pi_agent, "CONDUCTOR_TOOLS", ()))
               | set(getattr(pi_agent, "DEFAULT_TOOLS", ())))
    return all(tool in catalog for tool in ANALYZER_FS_TOOLS)


def _strip_frontmatter(text: str) -> str:
    """Drop leading YAML frontmatter before sending to claude.

    Claude's CLI arg parser treats a prompt that starts with `---` as
    an unknown flag and exits 1. The frontmatter is metadata for our
    tooling — not instructions for the model.
    """
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end > 0:
            return text[end + len("\n---\n"):].lstrip()
    return text


def _strip_code_fence(text: str, kind: str) -> str:
    pattern = rf"```\s*{kind}?\s*\n(.*?)```"
    m = re.search(pattern, text, re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else text


def _strip_preamble_to_heading(text: str) -> str:
    """Trim any conversational preamble before the first markdown heading.

    Claude (any model) often opens with a 'I have what I need — emitting
    the doc now' sentence before producing the real document. That
    preamble breaks the `_classify` startswith('#') check and renders
    badly in the OnboardingView. This finds the first `# ` line and
    returns from there. If no heading exists, the original text is
    returned unchanged so the classifier can mark it failed.
    """
    for i, line in enumerate(text.splitlines()):
        if line.lstrip().startswith("# "):
            return "\n".join(text.splitlines()[i:]).strip()
    return text


def _payload_from_final(final: str, analyzer: str) -> Any:
    """Parse ONE final assistant text block into the analyzer payload.
    Shared by the claude (stream-json) and pi (single text) backends so the
    payload contract is identical on both (task 96341ef8)."""
    final = (final or "").strip()
    if analyzer == "onboarding_writer":
        # Strip the ```markdown ... ``` fence first (if the model wrapped it),
        # then drop any preamble before the first H1.
        return _strip_preamble_to_heading(_strip_code_fence(final, "markdown"))
    cleaned = _strip_code_fence(final, "json")
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"raw": final, "error": "not_valid_json"}


def _extract_payload(res: claude_cli.ClaudeCliResult, analyzer: str) -> Any:
    """Pull the analyzer's JSON or markdown payload out of stream-json events."""
    text_blocks: list[str] = []
    for evt in res.parsed_events:
        msg = evt.get("message") or evt
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_blocks.append(block.get("text", ""))
    final = text_blocks[-1] if text_blocks else ""
    return _payload_from_final(final, analyzer)


def _payload_parseable(payload: Any) -> bool:
    """A payload is parseable when it's markdown starting with '#' or a
    schema-shaped dict (no error envelope). Backend-agnostic."""
    return (
        isinstance(payload, str) and payload.startswith("#")
    ) or (
        isinstance(payload, dict) and "error" not in payload
        and ("schema" in payload or "steps" in payload or "layers" in payload
             or "domains" in payload)
    )


def _classify_ok(exit_ok: bool, payload: Any) -> str:
    """complete | partial | failed from an exit-ok flag + payload."""
    parseable = _payload_parseable(payload)
    if exit_ok and parseable:
        return "complete"
    return "partial" if parseable else "failed"


def _classify(res: claude_cli.ClaudeCliResult, payload: Any) -> str:
    """complete | partial | failed — same rules as the legacy CLI executor."""
    return _classify_ok(res.exit_code == 0, payload)


def run_analyzer(
    project: str,
    analyzer: str,
    target_sha: str,
    scope_hash: str = "full",
    *,
    plugin_dir: Optional[str] = None,
    max_turns: int = DEFAULT_MAX_TURNS,
    max_budget_usd: float = 0.0,
) -> dict:
    """Execute one analyzer for `project` at `target_sha`.

    Returns:
        {
            "payload": <analyzer output: dict or markdown str>,
            "tokens_used": int,
            "wall_clock_s": float,
            "status": "complete" | "partial" | "failed",
            "error": str,
        }

    Raises:
        FileNotFoundError: when the `claude` CLI is not on PATH.
        claude_cli.ClaudeNotLoggedInError: when claude reports auth failure.
        FileNotFoundError: when the analyzer prompt file is missing.
    """
    prompt_path = _PROMPTS_DIR / f"{analyzer}.md"
    template = _strip_frontmatter(prompt_path.read_text(encoding="utf-8"))

    source_dir = ss.source_dir_for(project)
    runtime = (
        f"\n\n## Runtime context\n"
        f"- project: {project}\n"
        f"- target_sha: {target_sha}\n"
        f"- scope_hash: {scope_hash}\n"
        f"- source_dir: your cwd — the source tree at the pinned SHA "
        f"is already checked out here; use Read/Glob/Grep to explore.\n"
        f"- output: emit the schema's JSON (or markdown for "
        f"onboarding_writer) as your final assistant message; do NOT "
        f"write files.\n"
    )

    # Backend seam (task 96341ef8). Default -> claude_cli (below). The pi
    # runtime path only engages when it can actually walk files; otherwise
    # a skip-guard falls back to claude so a premature opt-in never yields
    # empty analyses. pi_agent.invoke records its own pi_run_log row
    # (backend='pi'), so the ledger stays truthful on both paths.
    if _analyzer_backend() == "pi":
        if _pi_has_fs_tools():
            return _run_via_pi(
                template + runtime, project=project, analyzer=analyzer,
                max_turns=max_turns,
            )
        _log.warning(
            "PRISM_ANALYZER_BACKEND=pi but the pi runtime advertises no "
            "%s file tools; falling back to claude for analyzer %r "
            "(follow-up: add read-only fs tools to the pi catalog)",
            "/".join(ANALYZER_FS_TOOLS), analyzer,
        )

    res = claude_cli.invoke(
        template + runtime,
        source_dir,
        plugin_dir=plugin_dir or str(source_dir),
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        parse_events=True,
        project=project,
        purpose=f"{analyzer}@{(target_sha or '')[:10]}",
    )
    payload = _extract_payload(res, analyzer)
    status = _classify(res, payload)
    return {
        "payload": payload,
        "tokens_used": res.usage.get("output_tokens", 0)
                       + res.usage.get("input_tokens", 0),
        "wall_clock_s": 0.0,
        "status": status,
        "error": "" if status != "failed" else f"exit={res.exit_code}",
    }


def _run_via_pi(prompt: str, *, project: str, analyzer: str,
                max_turns: int) -> dict:
    """Execute one analyzer through the pi-agent runtime (task 96341ef8).

    Returns the IDENTICAL run_analyzer contract (payload / tokens_used /
    wall_clock_s / status / error) so callers persist the artifact the same
    way regardless of backend. The agentic file-walk uses ANALYZER_FS_TOOLS;
    pi_agent.invoke records the pi_run_log row (backend='pi', tokens, purpose)
    itself — the single recording point (task d1d4fe00), so no extra ledger
    write here. A spawn/parse failure (PiRuntimeError) surfaces as a failed
    status rather than raising into the drain loop."""
    try:
        result = pi_agent.invoke(
            prompt,
            allowed_tools=ANALYZER_FS_TOOLS,
            project=project,
            max_turns=max_turns,
            purpose=f"{analyzer}@pi",
        )
    except pi_agent.PiRuntimeError as exc:
        return {"payload": {"raw": "", "error": "pi_runtime"},
                "tokens_used": 0, "wall_clock_s": 0.0,
                "status": "failed", "error": f"pi runtime: {exc}"}
    payload = _payload_from_final(result.get("text") or "", analyzer)
    status = _classify_ok(bool(result.get("ok")), payload)
    return {
        "payload": payload,
        "tokens_used": int(result.get("tokens") or 0),
        "wall_clock_s": round(float(result.get("ms") or 0.0) / 1000.0, 3),
        "status": status,
        "error": ("" if status != "failed"
                  else str(result.get("error") or "pi run not parseable")),
    }
