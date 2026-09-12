"""Headless `claude -p` subprocess primitive for the PRISM service.

Promoted from the harness primitive at
`plugins/prism-devtools/tests/harness/prism_harness/claude_session.py:
run_claude` so the service can drive Understand-Anything analyzers
(T8) and the `prism understand` CLI (T11) with the same
env-stripping, no-API-key invocation contract.

INV-1 from story 5.1: `ANTHROPIC_API_KEY`, `CLAUDECODE`, and
`CLAUDE_CODE_ENTRYPOINT` are stripped from the child env. This is
the single enforcement point.
"""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


_STRIP_VARS = frozenset({
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "ANTHROPIC_API_KEY",
})

# Sentinel exit code for a `claude -p` child killed by subprocess.run's
# own timeout= — distinct from any real CLI exit code so callers can
# tell "wedged, we killed it" apart from "the model actually failed".
TIMEOUT_EXIT_CODE = -9


class ClaudeNotLoggedInError(RuntimeError):
    """Raised when the Claude CLI reports an unauthenticated state.

    Remediation: run `claude login` in the same shell where the
    service is started.
    """

    def __init__(self, stderr_excerpt: str = "") -> None:
        msg = (
            "claude CLI is not logged in. Run `claude login` and retry."
        )
        if stderr_excerpt:
            msg += f"\nstderr excerpt: {stderr_excerpt[:200]}"
        super().__init__(msg)
        self.stderr_excerpt = stderr_excerpt


@dataclass
class ClaudeCliResult:
    """Structured result from a `claude -p` invocation."""

    output_path: Path
    exit_code: int
    parsed_events: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    run_id: str = ""
    duration_s: float = 0.0
    structured_output: dict | None = None

    def final_text(self) -> str:
        """Pull the last assistant text block out of the stream events.
        Convenience accessor so callers don't have to walk parsed_events
        themselves (analyzer_runner re-implements this locally — could
        be unified later)."""
        return _final_assistant_text(self.parsed_events)

    def graceful_budget_stop(self) -> bool:
        """True when the run ended because a turn/budget CEILING was hit
        AFTER the model finished a complete message, not mid-generation.

        `task_runner._run_one_step` used to treat any non-zero exit_code
        as "no usable output" and discard `final_text()` outright — but a
        step whose own grounding-heavy work costs right around
        `--max-budget-usd` gets `is_error:true` from a POST-HOC budget
        check even when the model's own turn ended normally. The tell is
        the terminal `type=="result"` event: `stop_reason=="end_turn"`
        means the model was not cut off mid-answer, while
        `subtype` in ("error_max_budget_usd", "error_max_turns") names
        which ceiling stopped the run. Both conditions together mean the
        run has a complete, usable final message despite the non-zero
        exit code; a crash, auth failure, or a truncation mid-generation
        (any other stop_reason) must still be treated as a real failure.
        """
        return _graceful_budget_stop(self.parsed_events)


# Default tool whitelist for batch / background invocations
# (Understand-Anything analyzers, drift sync, etc.). Strictly
# read-only: the model walks the source tree and emits structured
# output, never touches disk or shells out. Interactive call sites
# (e.g. future "ask memory" Q&A) pass their own broader list.
READ_ONLY_TOOLS = ("Read", "Glob", "Grep")


def _build_cmd(
    prompt: str,
    plugin_dir: Path | str,
    model: str,
    max_budget_usd: float,
    max_turns: int,
    allowed_tools: tuple[str, ...] = READ_ONLY_TOOLS,
    json_schema: dict | None = None,
    session_id: str = "",
) -> list[str]:
    """Build the `claude -p` invocation.

    Permission model — v5.1.7 onward:
      Each call site chooses its own tool scope via `allowed_tools`.
      Batch analyzers stay on the READ_ONLY_TOOLS default; an
      interactive "ask memory" path can pass e.g.
      ("Read", "Glob", "Grep", "Bash") to let claude shell out and
      query MCP tools. We never use --dangerously-skip-permissions —
      it bypasses every guard, trips Claude's root/sudo safety check,
      and grants tool access we don't actually need.

      Pass `allowed_tools=()` for NO TOOLS AT ALL: `--tools ""` disables
      the built-in set and `--strict-mcp-config`, with no --mcp-config
      beside it, leaves no MCP server configured.

      IT USED TO MEAN THE OPPOSITE, and every call site read it the way it
      reads: premise-judge ("this call needs zero tool round trips"),
      brain /ask ("no tool calls"), graph_enrich, and the narrow declared
      middles in task_runner and resume_actuator all pass `()` meaning
      none. What they got was claude's DEFAULT toolset plus every
      configured MCP server, because an empty tuple is falsy and the flag
      was simply skipped. v5.3.14 already recorded the symptom
      ("`allowed_tools=()` doesn't disable claude's default tool set") and
      worked around it in the PROMPT rather than on the command line, so
      the trap stayed set. It cost task d5808cd1 seven `exit=-9` runs at
      the 900 s bound: the `system/init` event of run a141a41ee2ae shows
      40+ tools and 13 MCP servers offered to a single no-tool text
      generation, which then spent the whole budget in
      `mcp__prism__brain_*` round trips and never reported.

      A call site that genuinely wants claude's default permission model
      passes the tools it wants by name; there is no longer a spelling
      that silently means "everything".
    """
    cmd = [
        "claude",
        "-p", prompt,
        "--plugin-dir", str(plugin_dir),
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--max-turns", str(max_turns),
    ]
    if allowed_tools:
        cmd += ["--allowedTools", *allowed_tools]
    else:
        # Both halves are load-bearing. `--tools ""` empties the built-in
        # set; without --strict-mcp-config the workspace .mcp.json still
        # hands the model a full MCP toolset through the back door.
        cmd += ["--tools", "", "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    if max_budget_usd > 0:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if session_id:
        cmd += ["--session-id", session_id]
    if json_schema is not None:
        # Verified live (2026-08-20): --json-schema + --output-format
        # json/stream-json returns a "structured_output" field, already
        # parsed, on the single type=="result" event -- no manual
        # json.loads(result["result"]) needed. Also verified: schema-
        # constrained output costs at least one retry turn beyond a plain
        # call (num_turns==2 failed against max_turns=1 in testing) --
        # callers passing json_schema should budget max_turns >= 3.
        cmd += ["--json-schema", json.dumps(json_schema)]
    return cmd


@functools.lru_cache(maxsize=1)
def _setpriv_path() -> str:
    """Absolute path to setpriv, or "" when this host has no usable one.

    Cached: the answer cannot change inside one process, and `invoke` runs
    on every step of every drive.
    """
    path = shutil.which("setpriv") or ""
    if not path:
        return ""
    # Old util-linux builds have setpriv without --pdeathsig. Ask the binary
    # instead of assuming, because a wrong guess turns every dispatch into an
    # immediate usage error and stops the daemon dead.
    try:
        probe = subprocess.run(
            [path, "--help"], capture_output=True, timeout=5,
        )
    except Exception:
        return ""
    helptext = (probe.stdout or b"") + (probe.stderr or b"")
    return path if b"--pdeathsig" in helptext else ""


def _with_parent_death_signal(cmd: list[str]) -> list[str]:
    """Wrap `cmd` so the kernel kills the child when THIS process dies.

    THE ORPHAN (measured 2026-09-11). `subprocess.run` cleans a child up on
    its own timeout, but it never runs when the PARENT dies hard -- a pytest
    session under `timeout 3000`, or a killed daemon. The `claude -p` child
    is then reparented to init and runs to its own budget with nobody
    collecting the result. Ten were live on this host, seven of them inside
    fixture directories that had already been deleted, the oldest 32 minutes
    old. Each carried --max-budget-usd, so each was still spending.

    PR_SET_PDEATHSIG closes that: the kernel sends SIGTERM the moment the
    parent goes away. We set it through setpriv rather than a preexec_fn
    because the daemon is threaded, and the CPython docs warn that a
    preexec_fn can deadlock a forked child in a threaded parent.

    setpriv EXECS the wrapped program, so it replaces its own process image
    and `/proc/<pid>/cmdline` still reads "claude -p ...". That matters:
    dispatch_guard's reaper finds its children by that exact prefix. The
    setting survives the exec because claude is not setuid.

    The signal fires on death of the parent THREAD, not the process. That is
    safe here only because `subprocess.run` blocks the calling thread for the
    whole life of the child -- the thread cannot exit first. Do not reuse
    this helper behind Popen without re-checking that.

    Falls back to the bare command when setpriv is unavailable, so a host
    without util-linux keeps today's behaviour instead of failing to spawn.
    """
    setpriv = _setpriv_path()
    if not setpriv:
        return cmd
    return [setpriv, "--pdeathsig", "TERM", "--", *cmd]


def _narrow_context_dir() -> Path:
    """An empty directory a NO-TOOL call runs from, so it inherits no CLAUDE.md.

    THE MEASUREMENT (2026-09-10, live daemon). The declared verify_plan middle
    is a 1,295-character prompt with no tools, and its call carried 28,560
    input tokens. Three `claude -p` runs with the live seat's exact flags,
    varying only what the child could see:

        trivial 30-char prompt, real task workspace .. 28,003 input
        trivial 30-char prompt, empty scratch dir ....  7,602 input
        difference ................................... 20,401 tokens

    A thirty-character prompt costs 28,003 tokens in the task workspace,
    because that workspace carries a 75,961-byte CLAUDE.md written for an
    INTERACTIVE CODING AGENT. Given ~20k tokens of "you read files and run
    commands" and then `--tools ""`, the model role-plays the loop anyway and
    emits `<file-read>` as text with INVENTED contents -- task d5808cd1's plan
    cites prism_service/tasks/reap.py, which does not exist. a9f2bec7 removed
    the tools; only this removes the belief that it has them.

    NOT `--bare`, which also skips CLAUDE.md discovery but declares "Anthropic
    auth is strictly ANTHROPIC_API_KEY or apiKeyHelper (OAuth and keychain are
    never read)" -- and `_strip_env` removes ANTHROPIC_API_KEY under INV-1.
    Discovery is directory-driven (`--bare`'s own escape hatch is "--add-dir
    (CLAUDE.md dirs)"), so a clean cwd gets the reduction without touching
    authentication.

    STABLE, NEVER A PER-CALL TEMPDIR. The cwd appears in the harness system
    prompt, so a path that changed per call would change the prefix per call
    and defeat prompt caching -- and caching is what makes the envelope cheap
    when it hits (a draft_story run paid 613 fresh tokens against 27,520
    cache_read and finished in 23.9s).
    """
    from prism_service.config import DATA_DIR

    d = Path(DATA_DIR) / "narrow_context"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backend_env() -> dict:
    """Return the redirect that points `claude -p` at the chosen model.

    PRISM keeps the claude harness on both settings. The harness owns the
    tool loop, the turns, the budget and the run manifest, so this changes
    the model and nothing else.

    The default backend is "claude" and returns an empty mapping, so the
    child environment is byte-identical to the behaviour before this
    setting existed. The "local" backend sets ANTHROPIC_BASE_URL.

    This never sets ANTHROPIC_API_KEY. INV-1 strips that variable, and the
    local engine needs no credential.
    """
    from prism_service import config

    if (config.INFERENCE_BACKEND or "").strip().lower() != "local":
        return {}
    env = {"ANTHROPIC_BASE_URL": config.LOCAL_INFERENCE_BASE_URL}
    if config.LOCAL_INFERENCE_AUTH_TOKEN:
        env["ANTHROPIC_AUTH_TOKEN"] = config.LOCAL_INFERENCE_AUTH_TOKEN
    return env


def _strip_env(base_env: dict | None = None) -> dict:
    """Return a copy of base_env (or os.environ) with INV-1 vars removed.

    The configured backend redirect applies last, so the setting wins over
    an ANTHROPIC_BASE_URL that the parent environment already carries.
    """
    src = os.environ if base_env is None else base_env
    env = {k: v for k, v in src.items() if k not in _STRIP_VARS}
    env.update(_backend_env())
    return env


_AUTH_FAIL_MARKERS = (
    "not logged in",
    "claude login",
    "please log in",
    "please run /login",
    "unauthenticated",
)


def _looks_like_not_logged_in(
    stderr_text: str, exit_code: int, final_text: str = "",
) -> bool:
    """Return True when either stderr or the model's final assistant
    text indicates auth failure. Newer claude versions surface
    'Not logged in · Please run /login' through stdout as a normal
    assistant message instead of stderr — final_text catches that case.
    """
    if exit_code == 0:
        return False
    for source in (stderr_text, final_text):
        if not source:
            continue
        lower = source.lower()
        if any(marker in lower for marker in _AUTH_FAIL_MARKERS):
            return True
    return False


def _final_assistant_text(parsed_events: list[dict]) -> str:
    """Pull the last assistant text block out of parsed stream events."""
    text_blocks: list[str] = []
    for evt in parsed_events:
        msg = evt.get("message") or evt
        if not isinstance(msg, dict):
            continue
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text":
                text_blocks.append(block.get("text", ""))
    return (text_blocks[-1] if text_blocks else "").strip()


_GRACEFUL_STOP_SUBTYPES = frozenset({
    "error_max_budget_usd",
    "error_max_turns",
})


def _graceful_budget_stop(parsed_events: list[dict]) -> bool:
    """See ClaudeCliResult.graceful_budget_stop -- standalone so callers
    without a ClaudeCliResult (e.g. a raw parsed-events list in a test)
    can check the same condition. False when there is no result event at
    all (a crash never gets to emit one)."""
    result = next(
        (e for e in reversed(parsed_events) if e.get("type") == "result"),
        None,
    )
    if result is None:
        return False
    subtype = result.get("subtype")
    stop_reason = result.get("stop_reason")
    return subtype in _GRACEFUL_STOP_SUBTYPES and stop_reason == "end_turn"


def _usage_from_result(parsed: list[dict]) -> dict:
    """Authoritative whole-run usage: the single ``type=="result"`` event
    carries the run's totals counted ONCE, across all four token fields, plus
    ``total_cost_usd`` and the model that did the work. Summing usage across
    the per-assistant-event snapshots (as the old code did) multi-counts the
    same turn — the identical usage object recurs on many stream chunks. No
    result event (crash / not-logged-in) -> an honest zero, never a partial
    sum. (task 45e04fad)"""
    usage = {"input_tokens": 0, "output_tokens": 0,
             "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
             "cost_usd": 0.0, "model": ""}
    result = next((e for e in reversed(parsed) if e.get("type") == "result"), None)
    if result is None:
        return usage
    u = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    for f in ("input_tokens", "output_tokens",
              "cache_read_input_tokens", "cache_creation_input_tokens"):
        usage[f] = int(u.get(f) or 0)
    usage["cost_usd"] = float(result.get("total_cost_usd") or 0.0)
    mu = result.get("modelUsage")
    if isinstance(mu, dict) and mu:
        # the model that actually ran (single-key on a normal -p run)
        usage["model"] = next(iter(mu.keys()), "") or ""
    return usage


def _structured_output_from_result(parsed: list[dict]) -> dict | None:
    """The schema-constrained payload off the same type=="result" event
    _usage_from_result reads -- already parsed by the CLI, never a raw
    string needing json.loads() here. None when the call wasn't made with
    json_schema, or the run never reached a result event."""
    result = next((e for e in reversed(parsed) if e.get("type") == "result"), None)
    if result is None:
        return None
    out = result.get("structured_output")
    return out if isinstance(out, dict) else None


def _parse_jsonl(out_path: Path) -> tuple[list[dict], dict]:
    parsed: list[dict] = []
    try:
        for line in out_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            parsed.append(evt)
    except OSError:
        pass
    return parsed, _usage_from_result(parsed)


def invoke(
    prompt: str,
    work_dir: Path | str,
    plugin_dir: Path | str,
    *,
    model: str = "",
    max_budget_usd: float = 0.0,
    max_turns: int = 3,
    parse_events: bool = True,
    project: str = "",
    purpose: str = "",
    allowed_tools: tuple[str, ...] = READ_ONLY_TOOLS,
    json_schema: dict | None = None,
    timeout_s: float | None = None,
    session_id: str = "",
) -> ClaudeCliResult:
    """Run `claude -p` headless and capture stream-json output.

    The captured output is moved to /data/claude_runs/<run_id>.jsonl and
    a manifest line is appended so operators can verify executions via
    /api/claude-runs. `project` and `purpose` are stored on the manifest
    line — pass them when you have them (e.g. analyzer_runner does).

    Raises:
        ClaudeNotLoggedInError: when the CLI reports auth failure.
            Remediation: run `docker exec -it prism-service claude login`.
    """
    # Try to land the stream-json directly in the persistent run log
    # dir; fall back to a tempfile if that volume isn't writable (e.g.
    # in unit tests with no /data mount).
    out_path: Path
    run_id: str = ""
    try:
        from prism_service.services import claude_run_log
        run_id, out_path = claude_run_log.new_run()
    except Exception:
        tmp = tempfile.NamedTemporaryFile(
            prefix="prism-claude-", suffix=".jsonl", delete=False,
        )
        tmp.close()
        out_path = Path(tmp.name)

    # A no-tool call gets a clean room. `allowed_tools=()` is the narrow
    # marker -- the same condition that selects the declared prompt and
    # disables every tool -- and a narrow call has no use for the task
    # workspace, only for the ~20k tokens of interactive-agent doctrine it
    # would inherit from there. Both the cwd AND --plugin-dir move, because
    # each carries its own discovery. A tool-using step is untouched: it reads
    # and writes real files in the workspace and wants that CLAUDE.md.
    if not allowed_tools:
        work_dir = plugin_dir = _narrow_context_dir()

    cmd = _build_cmd(
        prompt, plugin_dir, model, max_budget_usd, max_turns,
        allowed_tools=allowed_tools, json_schema=json_schema,
        session_id=session_id,
    )
    env = _strip_env()

    run_kwargs = {} if timeout_s is None else {"timeout": timeout_s}

    ts_start = time.time()
    with open(out_path, "w", encoding="utf-8") as fh:
        try:
            result = subprocess.run(
                _with_parent_death_signal(cmd), cwd=str(work_dir), env=env,
                stdout=fh, stderr=subprocess.PIPE,
                **run_kwargs,
            )
        except subprocess.TimeoutExpired:
            ts_end = time.time()
            parsed_events, usage = _parse_jsonl(out_path)
            if not parse_events:
                parsed_events, usage = [], {"input_tokens": 0, "output_tokens": 0}
            stderr_text = f"claude -p timed out after {timeout_s}s"
            _safe_record_run(
                run_id=run_id, stream_path=out_path,
                ts_start=ts_start, ts_end=ts_end,
                project=project, purpose=purpose,
                exit_code=TIMEOUT_EXIT_CODE, usage=usage,
                stderr_text=stderr_text,
            )
            return ClaudeCliResult(
                output_path=out_path, exit_code=TIMEOUT_EXIT_CODE,
                parsed_events=parsed_events, usage=usage,
                run_id=run_id, duration_s=(ts_end - ts_start),
                structured_output=None,
            )
    ts_end = time.time()

    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")

    # Parse the stream first so we can also inspect the assistant's final
    # text — newer claude surfaces "Not logged in · Please run /login"
    # there instead of stderr, and we need to catch both.
    parsed_events, usage = _parse_jsonl(out_path)
    final_text = _final_assistant_text(parsed_events)

    if _looks_like_not_logged_in(stderr_text, result.returncode, final_text):
        # Still record the failed-auth attempt so operators can see it
        # from the UI without trawling docker logs.
        _safe_record_run(
            run_id=run_id, stream_path=out_path,
            ts_start=ts_start, ts_end=ts_end,
            project=project, purpose=purpose,
            exit_code=result.returncode, usage=usage,
            stderr_text=stderr_text or final_text,
        )
        raise ClaudeNotLoggedInError(stderr_text or final_text)

    if not parse_events:
        parsed_events, usage = [], {"input_tokens": 0, "output_tokens": 0}

    _safe_record_run(
        run_id=run_id, stream_path=out_path,
        ts_start=ts_start, ts_end=ts_end,
        project=project, purpose=purpose,
        exit_code=result.returncode, usage=usage,
        stderr_text=stderr_text,
    )

    return ClaudeCliResult(
        output_path=out_path, exit_code=result.returncode,
        parsed_events=parsed_events, usage=usage,
        run_id=run_id, duration_s=(ts_end - ts_start),
        structured_output=(
            _structured_output_from_result(parsed_events)
            if json_schema is not None else None
        ),
    )


def _safe_record_run(**kw) -> None:
    """Append to /data/claude_runs/manifest.jsonl. Best-effort: any
    failure (no /data mount in tests, manifest unwritable, etc.) is
    swallowed so the caller sees an unaltered ClaudeCliResult."""
    if not kw.get("run_id"):
        return  # tempfile fallback — nothing to record
    try:
        from prism_service.services import claude_run_log
        claude_run_log.record_run(**kw)
    except Exception:
        pass


def run_claude(
    prompt: str,
    work_dir: Path | str,
    plugin_dir: Path | str,
    *,
    model: str = "",
    max_budget_usd: float = 0.0,
    max_turns: int = 3,
) -> tuple[Path, int]:
    """Back-compat tuple-return API.

    Mirrors the original harness signature so the 11 harness tests
    under `plugins/prism-devtools/tests/harness/prism_harness/tests/`
    keep working unchanged. New callers should prefer `invoke()`
    which returns a structured ClaudeCliResult.
    """
    res = invoke(
        prompt, work_dir, plugin_dir,
        model=model,
        max_budget_usd=max_budget_usd,
        max_turns=max_turns,
        parse_events=False,
    )
    return res.output_path, res.exit_code
