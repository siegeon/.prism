"""Persistent audit ledger of PRISM's internal pi/local inference runs.

Every internal agentic job through `inference.pi_agent.invoke` (incl.
reflection with PRISM_REFLECTION_BACKEND=pi) and every plain completion
through `inference.local_llm.complete` is recorded so operators can
VISUALLY audit what the internal agent is doing (owner directive:
internal tools come with an audit surface by default). This is the
sibling of services/claude_run_log.py — same pattern (append-only
manifest under DATA_DIR, env-tunable bound + trim, best-effort record,
list_recent/get_run), different store and row shape:

    /data/pi_runs/
        manifest.jsonl          # one append-only line per run

Manifest line schema (each row a complete JSON object):

    {
        "run_id":       "uuid hex, 12 chars",
        "ts":           1779417347.123,      # run start (epoch s)
        "ts_end":       1779417349.456,
        "duration_ms":  2333.0,
        "backend":      "pi" | "local",
        "model":        "qwen3:0.6b",
        "purpose":      "reflect" | "panel-bridge" | "adhoc",
        "project":      "prism",             # may be ""
        "prompt_chars": 1234,
        "tools_used":   [{"name": "brain_search", "ms": 40.2, "ok": true}],
        "turns":        3,                   # 0 for tool-less local runs
        "tokens":       42,                  # completion side (KPI strip)
        "input_tokens":  33,                 # additive split (task d1d4fe00)
        "output_tokens": 42,                 # — telemetry survives the
                                             # claude_runs -> pi_runs move
        "task_id":      "fc08da8d-...",      # driven task (fc08da8d); "" adhoc
        "ok":           true,
        "error":        ""                   # first 1 KB on failure
    }

    pi_runs is THE ledger for backend pi|local (task d1d4fe00):
    local_llm.complete and pi_agent.invoke record here themselves, so
    seam callers must NOT write their own row — they pass purpose/
    project through and read the run_id back off the completion.

Unlike claude runs there is NO per-run stream file: the tool receipts
are small structured rows that ride the manifest line directly, which
is why this is a sibling ledger rather than a `backend` field bolted
onto claude_run_log (whose rows are subprocess-shaped: exit_code,
stream_path, stderr_excerpt, final_text — and whose /api/claude-runs
consumers render exactly those fields).

`/api/pi-runs` reads this file from the tail; the SPA's /internal-agent
audit view renders the rows. Bounded by RUN_LOG_LIMIT.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from typing import Optional

from prism_service.config import DATA_DIR

_RUNS_DIR = DATA_DIR / "pi_runs"
_MANIFEST = _RUNS_DIR / "manifest.jsonl"

# Bound on how many runs we keep. Operator-tunable via env.
RUN_LOG_LIMIT = int(os.environ.get("PRISM_PI_RUN_LOG_LIMIT", "500"))
_ERROR_MAX = 1024

# The purposes the audit view groups by. Free-form strings are allowed
# (the ledger never rejects a row over vocabulary) — these are the
# canonical three.
KNOWN_PURPOSES = ("reflect", "panel-bridge", "adhoc")


def _truncate(s: str, limit: int) -> str:
    if not s:
        return ""
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _clean_tools(tools_used: object) -> list[dict]:
    """Normalize tool receipts to [{name, ms, ok}] — drop anything that
    isn't dict-shaped so a malformed runner payload can't poison the
    manifest line."""
    out: list[dict] = []
    for t in tools_used or []:
        if not isinstance(t, dict):
            continue
        try:
            out.append({
                "name": str(t.get("name", "")),
                "ms": float(t.get("ms", 0) or 0),
                "ok": bool(t.get("ok", False)),
            })
        except (TypeError, ValueError):
            continue
    return out


def record_run(
    *,
    backend: str,
    model: str,
    purpose: str = "adhoc",
    project: str = "",
    prompt_chars: int = 0,
    tools_used: Optional[list] = None,
    duration_ms: float = 0.0,
    tokens: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    turns: int = 0,
    task_id: str = "",
    ok: bool = True,
    error: str = "",
    ts_start: Optional[float] = None,
    ts_end: Optional[float] = None,
) -> Optional[str]:
    """Append one manifest line; trim older runs. Best-effort: NEVER
    raises into the inference caller path — returns the run_id on
    success, None on failure."""
    try:
        _RUNS_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        start = float(ts_start) if ts_start is not None else now
        end = float(ts_end) if ts_end is not None else now
        run_id = uuid.uuid4().hex[:12]
        entry = {
            "run_id": run_id,
            "ts": start,
            "ts_end": end,
            "duration_ms": max(0.0, float(duration_ms)),
            "backend": str(backend or ""),
            "model": str(model or ""),
            "purpose": str(purpose or "adhoc"),
            "project": str(project or ""),
            "prompt_chars": int(prompt_chars or 0),
            "tools_used": _clean_tools(tools_used),
            "turns": int(turns or 0),
            "tokens": int(tokens or 0),
            # Additive input/output split (task d1d4fe00) — the fields
            # the claude_runs ledger carried, preserved on the move.
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            # Task-attribution (task fc08da8d): the task this run acted on,
            # so the conductor tile burn + task detail can attribute the PI
            # agent's tokens to the driven task. Additive, default "".
            "task_id": str(task_id or ""),
            "ok": bool(ok),
            "error": _truncate(str(error or ""), _ERROR_MAX),
        }
        with _MANIFEST.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        _trim_to_limit()
        return run_id
    except Exception as e:
        # Logging is best-effort — never let a ledger failure break
        # reflection or any other internal inference path.
        print(
            f"[pi_run_log] record_run failed: {type(e).__name__}: {e}",
            file=sys.stderr, flush=True,
        )
        return None


def _trim_to_limit() -> None:
    """If the manifest exceeds RUN_LOG_LIMIT lines, drop the oldest.
    Cheap because the manifest is bounded and line-oriented."""
    if not _MANIFEST.exists():
        return
    try:
        lines = _MANIFEST.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(lines) <= RUN_LOG_LIMIT:
        return
    kept = lines[len(lines) - RUN_LOG_LIMIT:]
    tmp = _MANIFEST.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(kept) + "\n", encoding="utf-8")
    tmp.replace(_MANIFEST)


def _read_filtered(project: Optional[str] = None,
                   task_id: Optional[str] = None) -> list[dict]:
    """Read the whole manifest (bounded by RUN_LOG_LIMIT) once and return
    the rows NEWEST FIRST, filtered by project (and, when given, task_id).
    Shared by list_recent and count_recent so a page and its grand total
    come from one read."""
    if not _MANIFEST.exists():
        return []
    out: list[dict] = []
    try:
        for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if project and entry.get("project") != project:
                continue
            if task_id and entry.get("task_id") != task_id:
                continue
            out.append(entry)
    except OSError:
        return []
    out.reverse()  # newest first
    return out


def list_recent(limit: int = 50, project: Optional[str] = None,
                offset: int = 0, task_id: Optional[str] = None) -> list[dict]:
    """Return one newest-first PAGE of runs. `offset` skips that many
    rows before taking `limit` (an offset past the end yields []); both
    default to the full-first-page behavior. `task_id` (task fc08da8d)
    narrows to the runs attributed to one driven task. Pair with
    count_recent for the grand total when paginating."""
    off = max(0, int(offset))
    return _read_filtered(project, task_id=task_id)[off:off + limit]


def count_recent(project: Optional[str] = None) -> int:
    """Full count of runs (project-filtered) — the grand total the API
    reports alongside a page so a client can paginate."""
    return len(_read_filtered(project))


def get_run(run_id: str) -> Optional[dict]:
    """Return the manifest entry for a specific run_id (or None)."""
    if not run_id or not _MANIFEST.exists():
        return None
    try:
        for line in _MANIFEST.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("run_id") == run_id:
                return entry
    except OSError:
        return None
    return None


def infer_purpose(purpose: str, system: str) -> str:
    """Resolve the recorded purpose: an explicit purpose wins; otherwise
    the reflection engine is recognized by its system-prompt marker
    (reflection_runner passes no purpose — it is owned by another
    surface); everything else is adhoc."""
    if purpose:
        return purpose
    if "reflection engine" in (system or "").lower():
        return "reflect"
    return "adhoc"
