"""Background worker that fills `ExpertiseEntry.summary` for memories that
ship without one.

The Memory page tile face renders this summary instead of the raw
`description` so a memory reads as a human-skimmable card rather than a
dense block of code references. Existing memories had `summary=""` until
this worker filled them; new memories created via `memory_store` start
empty and are filled on the next sweep.

Like reflection_worker, this shells out to `claude -p` and therefore
costs the user tokens — but unlike reflection_worker it defaults to ON
because the user explicitly requested the feature (the only way to get
summaries on the tiles). Disable with PRISM_MEMORY_SUMMARY_WORKER=off.

Env:
  PRISM_MEMORY_SUMMARY_WORKER=off        # default on
  PRISM_MEMORY_SUMMARY_WORKER_INTERVAL=60   # seconds between sweeps
  PRISM_MEMORY_SUMMARY_WORKER_MAX_PER_CYCLE=3
"""

from __future__ import annotations

import os
import sys as _sys
import threading
import time

# Prompt template kept at module level so /settings/activity's drilldown
# panel (v6.1.1) can render exactly what the worker sends to claude. The
# {name} / {description} placeholders are filled per-memory in summarize_one.
SUMMARY_PROMPT_TEMPLATE = (
    "You are summarizing a PRISM memory entry — a stored piece of "
    "project knowledge.\n\n"
    "Given the memory's name and description, write a single concise "
    "sentence (under 30 words) in plain English that captures its "
    "essence. The sentence will appear on a tile in the PRISM Memory "
    "page, so it should be skimmable and human-friendly. Do NOT include "
    "code, file paths, or jargon unless they're the central point. "
    "Don't preface with \"this memory…\" or \"this is about…\" — just "
    "say the thing.\n\n"
    "Name: {name}\n"
    "Description: {description}\n\n"
    "Output: a single sentence. No preamble. No quotation marks."
)

WORKER_ID = "memory_summary_worker"
WORKER_LABEL = "Memory summary writer"


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def _interval_s() -> int:
    try:
        return max(15, int(os.environ.get(
            "PRISM_MEMORY_SUMMARY_WORKER_INTERVAL", "60",
        )))
    except ValueError:
        return 60


def _max_per_cycle() -> int:
    try:
        return max(1, int(os.environ.get(
            "PRISM_MEMORY_SUMMARY_WORKER_MAX_PER_CYCLE", "3",
        )))
    except ValueError:
        return 3


def is_enabled() -> bool:
    """Honor the off-switch but default to ON.

    Surfaced for /api/consolidation/workers so the activity panel's
    'running' dot reflects reality.
    """
    return _env_truthy("PRISM_MEMORY_SUMMARY_WORKER", default=True)


def render_prompt(name: str, description: str) -> str:
    """Materialize SUMMARY_PROMPT_TEMPLATE with a concrete memory.

    Public so /api/consolidation/workers/{id}/prompt can render an
    example using whichever memory currently sits at the head of the
    queue, instead of a static template-with-braces.
    """
    return SUMMARY_PROMPT_TEMPLATE.format(name=name, description=description)


def _log(msg: str) -> None:
    print(f"[{WORKER_ID}] {msg}", file=_sys.stderr, flush=True)


def _strip(text: str) -> str:
    """Clean claude's response: trim, drop wrapping quotes, collapse
    whitespace. Models sometimes echo the instruction not to quote, so
    we strip a leading/trailing pair defensively."""
    t = (text or "").strip()
    if len(t) >= 2 and t[0] == t[-1] and t[0] in ('"', "'"):
        t = t[1:-1].strip()
    return " ".join(t.split())


def summarize_one(name: str, description: str, project: str) -> str:
    """Synchronously generate a summary via `claude -p`. Returns the
    cleaned single-sentence summary, or "" on any failure (auth missing,
    parse error, etc.) so the caller can move on and retry next cycle."""
    from prism_service.inference import claude_cli
    from prism_service.project_context import get_project
    prompt = render_prompt(name, description)
    # The project data dir is a safe cwd/plugin-dir — claude doesn't load
    # plugins from it (none present) and we don't need any tools for the
    # task. allowed_tools=() omits --allowedTools entirely.
    try:
        ctx = get_project(project)
        work_dir = str(ctx._data_dir)
    except Exception:
        work_dir = os.getcwd()
    try:
        result = claude_cli.invoke(
            prompt=prompt,
            work_dir=work_dir,
            plugin_dir=work_dir,
            max_turns=1,
            project=project,
            purpose="memory_summary",
            allowed_tools=(),
        )
    except claude_cli.ClaudeNotLoggedInError:
        _log("claude not logged in; pausing this cycle")
        raise
    except Exception as exc:
        _log(f"summarize_one raised: {exc}")
        return ""
    text = result.final_text() if result.exit_code == 0 else ""
    return _strip(text)


def pending_memories(project: str, limit: int) -> list:
    """Return up to `limit` active memories in this project missing a
    summary, oldest first."""
    from prism_service.project_context import get_project
    try:
        ctx = get_project(project)
    except Exception:
        return []
    svc = getattr(ctx, "memory_svc", None)
    if svc is None:
        return []
    out: list = []
    for domain in svc.list_domains():
        for entry in svc.list_entries(domain=domain, status_filter="active"):
            if entry.summary or entry.invalid_at:
                continue
            out.append(entry)
    out.sort(key=lambda e: e.recorded_at or "")
    return out[:limit]


def run_once() -> dict:
    """Sweep every known project once. Returns a small summary the
    /settings/activity panel + tests can inspect without log scraping."""
    from prism_service.config import list_projects
    n_max = _max_per_cycle()
    summary: dict = {"projects": [], "summarized": 0, "errors": 0}
    try:
        projects = list_projects()
    except Exception as exc:
        _log(f"list_projects failed: {exc}")
        return summary
    for project in projects:
        pending = pending_memories(project, n_max)
        if not pending:
            continue
        from prism_service.project_context import get_project
        try:
            svc = get_project(project).memory_svc
        except Exception as exc:
            _log(f"{project}: get_project failed: {exc}")
            summary["errors"] += 1
            continue
        done_here = 0
        for entry in pending:
            try:
                text = summarize_one(entry.name, entry.description, project)
            except Exception:
                # Auth missing — give up this cycle, retry next.
                break
            if not text:
                summary["errors"] += 1
                continue
            try:
                svc.update_entry(entry.id, summary=text)
                done_here += 1
                summary["summarized"] += 1
            except Exception as exc:
                _log(f"{project}: update_entry({entry.id}) failed: {exc}")
                summary["errors"] += 1
        if done_here:
            summary["projects"].append({"project": project, "summarized": done_here})
    return summary


def _loop() -> None:
    interval = _interval_s()
    _log(f"started; interval={interval}s")
    while True:
        try:
            run_once()
        except Exception as exc:
            _log(f"run_once raised: {exc}")
        time.sleep(interval)


def start_memory_summary_worker() -> threading.Thread | None:
    """Spawn the daemon thread unless explicitly disabled."""
    if not is_enabled():
        return None
    t = threading.Thread(target=_loop, name="prism-memory-summary-worker", daemon=True)
    t.start()
    return t
