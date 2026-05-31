"""Memory API — expertise domains, filtered entries, domain stats."""

from dataclasses import asdict
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prism_service.engines import understand_engine as ue
from prism_service.project_context import get_project
from prism_service.services import claude_memory as cm
from prism_service.services.memory_service import MemoryService

# v6.2.16 — the Claude-auto-memory bridge moved to services/claude_memory.py
# so the transcript-import poller can run it automatically. Re-exported here
# for backward compat with anything importing them off this module.
_CC_TYPE_MAP = cm._CC_TYPE_MAP
_parse_claude_memory = cm.parse_claude_memory

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).memory_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


@router.get("/domains")
def domains(project: str = Query("default")) -> dict:
    svc = _svc(project)
    return {"domains": svc.list_domains(), "stats": svc.domain_stats()}


@router.get("/entries")
def entries(
    project: str = Query("default"),
    domain: str | None = Query(None),
    type: str | None = Query(None),
    classification: str | None = Query(None),
    status: str | None = Query(None),
) -> dict:
    """List expertise entries. When `domain` is omitted, aggregate across
    every domain — the service's list_entries requires a domain string,
    so we fan out and stitch."""
    svc = _svc(project)
    # The service treats status_filter="" / falsy as "all statuses",
    # but expects a real string when filtering. Pass through unchanged
    # so the empty-status case ("all") returns everything.
    kwargs = {
        "type_filter": type,
        "classification_filter": classification,
        "status_filter": status if status else "",
    }
    if domain:
        rows = svc.list_entries(domain=domain, **kwargs)
    else:
        rows = []
        for d in svc.list_domains():
            rows.extend(svc.list_entries(domain=d, **kwargs))
    return {"entries": [_with_activation(r) for r in rows]}


def _with_activation(entry, now=None) -> dict:
    """Serialize an ExpertiseEntry to a dict with the activation score
    computed at query time — the decay-aware selection prior the SPA and
    the pruner both read. No new inference: pure arithmetic."""
    d = asdict(entry)
    d["activation"] = round(MemoryService.compute_activation(entry, now=now), 3)
    return d


@router.get("/fading")
def fading(
    project: str = Query("default"),
    limit: int = Query(20),
    threshold: float = Query(6.0),
) -> dict:
    """Low-activation entries ordered ascending by activation (coldest
    first) — the selection prior the background pruner reads. Default
    threshold surfaces entries whose decay has pulled them well below a
    mid-importance, never-recalled baseline."""
    svc = _svc(project)
    now = datetime.now(timezone.utc)
    faders = svc.fading_entries(limit=limit, threshold=threshold, now=now)
    return {"entries": [_with_activation(e, now=now) for e in faders]}


@router.get("/entry/{entry_id}")
def entry_detail(entry_id: str, project: str = Query("default")) -> dict:
    """Fetch a single memory entry + its supersede chain.

    The chain is every entry (any status) sharing the same `name` within
    the same domain, ordered by `generation`. Lets the detail page show
    'gen 1 → gen 2 (current)' history without a second round-trip.
    """
    svc = _svc(project)
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(404, f"unknown entry: {entry_id}")
    chain = [
        e for e in svc.list_entries(domain=entry.domain, status_filter="")
        if e.name == entry.name
    ]
    chain.sort(key=lambda e: e.generation)
    return {"entry": entry, "chain": chain}


@router.post("/import-claude-memories")
def import_claude_memories(project: str = Query("default")) -> dict:
    """Backfill PRISM expertise from Claude Code's auto-memory markdown.

    Delegates to services.claude_memory, which resolves the memory dir
    via (1) per-project `claude_memory_path` override, then (2) the
    slug-derived ~/.claude/projects/<slug>/memory default. Idempotent —
    skips entries whose name + description already match. The same import
    also runs automatically on the transcript-import poller (v6.2.16);
    this endpoint stays for an on-demand backfill from the SPA."""
    svc = _svc(project)
    return cm.import_project_memories(project, svc)


class ClaudeMemoryConfigBody(BaseModel):
    # Absolute path to this project's ~/.claude/projects/<slug> folder.
    # Sessions read <dir>/*.jsonl, memories read <dir>/memory/*.md.
    # Empty / null clears the override and reverts to auto-discovery.
    claude_project_dir: str | None = None


def _memory_config(project: str) -> dict:
    """Current per-project claude_project_dir override + what auto-discovery
    would resolve to, so the SPA can show 'auto' vs 'overridden' for both
    the session and memory importers."""
    try:
        cpd = (ue._read_state(project).get("claude_project_dir") or "").strip()
    except Exception:
        cpd = ""
    mem_dir = cm.resolve_memory_dir(project)
    return {
        "project": project,
        "claude_project_dir": cpd,
        "memory_dir": str(mem_dir),
        "auto_memory_dir": str(cm.auto_memory_dir(project)),
        "sessions_dir": cpd or "(auto: slug scan of ~/.claude/projects)",
        "memory_exists": mem_dir.is_dir(),
    }


@router.get("/claude-config")
def get_claude_memory_config(project: str = Query("default")) -> dict:
    return _memory_config(project)


@router.post("/claude-config")
def set_claude_memory_config(
    body: ClaudeMemoryConfigBody, project: str = Query("default"),
) -> dict:
    """Set (or clear) the per-project Claude project-dir override, then run
    a session + memory import immediately so the user sees the effect. A
    non-empty path must exist and be a directory."""
    from pathlib import Path
    from prism_service.services import claude_transcripts as ct
    svc = _svc(project)  # validates project exists
    ctx = get_project(project)
    raw = (body.claude_project_dir or "").strip()
    if raw and not Path(raw).is_dir():
        raise HTTPException(
            400,
            f"claude_project_dir {raw!r} does not exist or is not a directory. "
            "Paste the absolute path to this project's folder under "
            "~/.claude/projects (it holds the *.jsonl transcripts and a "
            "memory/ subdir), or leave it blank to auto-detect.",
        )
    try:
        state = ue._read_state(project)
    except Exception:
        state = {}
    if raw:
        state["claude_project_dir"] = raw
    else:
        state.pop("claude_project_dir", None)
    ue._write_state(project, state)
    mem = cm.import_project_memories(project, svc)
    scores_db = str(ctx._data_dir / "scores.db")
    sp = (state.get("source_path") or "").strip()
    sessions = ct.import_unseen(scores_db, sp, override_dir=raw or None)
    return {**_memory_config(project),
            "import_memories": mem, "import_sessions": sessions}
