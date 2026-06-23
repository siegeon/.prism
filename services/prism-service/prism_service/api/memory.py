"""Memory API — expertise domains, filtered entries, domain stats."""

import re
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.data_dir import resolve_claude_home
from prism_service.project_context import get_project
from prism_service.services.claude_transcripts import path_to_slug
from prism_service.services.memory_service import MemoryService

router = APIRouter()


# v6.0.8 — bridge Claude Code's auto-memory markdown into PRISM. Each
# .md has YAML frontmatter (name, description, metadata.type); we map
# the metadata.type to PRISM's (type, classification, memory_type,
# importance) tuple. Importance lean: feedback rules score high
# because they're "do/don't do" guidance the model must obey.
_CC_TYPE_MAP = {
    "feedback":  ("convention", "tactical",     "procedural", 7),
    "project":   ("decision",   "strategic",    "episodic",   6),
    "user":      ("decision",   "foundational", "semantic",   5),
    "reference": ("convention", "foundational", "semantic",   5),
}


def _svc(project: str):
    try:
        return get_project(project).memory_svc
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")


def _parse_claude_memory(path: Path) -> dict | None:
    """Extract {name, description, body, metadata.type} from a Claude
    Code memory .md. Returns None if frontmatter is missing or malformed."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    m = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if not m:
        return None
    fm_raw, body = m.group(1), m.group(2).strip()
    name = description = mtype = ""
    for line in fm_raw.splitlines():
        s = line.strip()
        if s.startswith("name:"):
            name = s.split(":", 1)[1].strip()
        elif s.startswith("description:"):
            description = s.split(":", 1)[1].strip().strip('"').strip("'")
        elif s.startswith("type:"):
            mtype = s.split(":", 1)[1].strip()
    if not name:
        return None
    return {"name": name, "description": description, "body": body,
            "metatype": mtype}


@router.get("/domains")
def domains(project: str = Query("default")) -> dict:
    svc = _svc(project)
    return {"domains": svc.list_domains(), "stats": svc.domain_stats()}


def _with_activation(entry, now=None) -> dict:
    """Serialize an ExpertiseEntry to a dict with the activation score
    computed at query time — the decay-aware selection prior the SPA and
    the pruner both read. No new inference: pure arithmetic."""
    d = asdict(entry)
    d["activation"] = round(MemoryService.compute_activation(entry, now=now), 3)
    return d


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


@router.post("/entry/{entry_id}/action")
def entry_action(
    entry_id: str,
    project: str = Query("default"),
    body: dict = Body(...),
) -> dict:
    """Operate on a memory entry from the SPA: edit / retire / supersede.

    * edit      — patch description/importance/name in place (update_entry).
    * retire    — mark the entry status='retired' (kept for the audit trail).
    * supersede — store a NEW generation under the same name; the existing
                  active entry is auto-archived by MemoryService.store's
                  temporal-dedup path.
    """
    svc = _svc(project)
    entry = svc.get_entry(entry_id)
    if entry is None:
        raise HTTPException(404, f"unknown entry: {entry_id}")
    action = (body.get("action") or "").strip()

    if action == "retire":
        updated = svc.update_entry(entry_id, status="retired")
        return {"ok": updated is not None, "action": "retire", "entry": updated}

    if action == "edit":
        patch = {}
        for k in ("description", "name", "importance"):
            if k in body and body[k] is not None:
                patch[k] = body[k]
        if not patch:
            raise HTTPException(422, "edit requires at least one of description/name/importance")
        updated = svc.update_entry(entry_id, **patch)
        return {"ok": updated is not None, "action": "edit", "entry": updated}

    if action == "supersede":
        desc = body.get("description")
        if not desc:
            raise HTTPException(422, "supersede requires a new description")
        new_entry = svc.store(
            domain=entry.domain,
            name=entry.name,
            description=desc,
            type=entry.type,
            classification=entry.classification,
            evidence=dict(entry.evidence or {}),
            importance=int(body.get("importance", entry.importance)),
            memory_type=entry.memory_type,
        )
        return {"ok": True, "action": "supersede", "entry": new_entry}

    raise HTTPException(422, f"unknown action {action!r}; expected edit|retire|supersede")


@router.get("/claude-config")
def get_claude_config(project: str = Query("default")) -> dict:
    """Return the per-project Claude transcript source dir.

    ``source`` is 'explicit' when a claude_project_dir is persisted (reported
    by Claude via register_claude_source, or set in Settings), else 'auto'
    (PRISM falls back to slug auto-discovery)."""
    from prism_service.engines import understand_engine as ue
    try:
        state = ue._read_state(project)
    except Exception:
        state = {}
    cpd = (state.get("claude_project_dir") or "").strip()
    return {
        "project": project,
        "claude_project_dir": cpd,
        "source": "explicit" if cpd else "auto",
    }


@router.post("/claude-config")
def set_claude_config(
    project: str = Query("default"),
    body: dict = Body(...),
) -> dict:
    """Persist the per-project Claude transcript source dir via the same
    understand_state.json writer the MCP tool uses. Blank clears it (back to
    auto slug discovery)."""
    from prism_service.engines import understand_engine as ue
    new_dir = (body.get("claude_project_dir") or "").strip()
    state = ue._read_state(project)
    if new_dir:
        state["claude_project_dir"] = new_dir
    else:
        state.pop("claude_project_dir", None)
    ue._write_state(project, state)
    return {
        "project": project,
        "claude_project_dir": new_dir,
        "source": "explicit" if new_dir else "auto",
    }


@router.post("/import-claude-memories")
def import_claude_memories(project: str = Query("default")) -> dict:
    """Backfill PRISM expertise from Claude Code's auto-memory markdown
    files at ~/.claude/projects/<slug>/memory/*.md. Idempotent:
    memory_store dedupes on name + 85% description similarity, so
    re-running is safe (existing entries are invalidated and
    superseded with bumped generation, not duplicated)."""
    svc = _svc(project)
    ctx = get_project(project)
    # Resolve the slug from the project's understand source_path, falling
    # back to the project id; either way maps to a slug under
    # ~/.claude/projects/.
    from prism_service.engines import understand_engine as ue
    try:
        state = ue._read_state(project)
        source_path = (state.get("source_path") or "").strip()
    except Exception:
        source_path = ""
    slug = path_to_slug(source_path) if source_path else project
    mem_dir = resolve_claude_home() / "projects" / slug / "memory"
    if not mem_dir.is_dir():
        return {
            "imported": 0, "skipped": 0, "failed": 0,
            "memory_dir": str(mem_dir),
            "reason": "no memory directory for this project",
        }
    imported = skipped = failed = 0
    details: list[dict] = []
    for path in sorted(mem_dir.glob("*.md")):
        if path.name == "MEMORY.md" or path.name.startswith("_"):
            skipped += 1
            continue
        parsed = _parse_claude_memory(path)
        if not parsed:
            skipped += 1
            details.append({"file": path.name, "result": "skip-no-frontmatter"})
            continue
        mtype = parsed["metatype"] or "feedback"
        if mtype not in _CC_TYPE_MAP:
            skipped += 1
            details.append({"file": path.name, "result": f"skip-type-{mtype}"})
            continue
        ptype, classification, memtype, importance = _CC_TYPE_MAP[mtype]
        full_desc = parsed["description"]
        if parsed["body"]:
            full_desc = (
                f"{full_desc}\n\n{parsed['body']}" if full_desc else parsed["body"]
            )
        # Skip if an active entry with this name and identical description
        # already exists — otherwise svc.store would supersede the old
        # with a content-identical new one (Graphiti pattern), generating
        # noise in the archive on every click.
        existing = [
            e for e in svc.list_entries(domain=mtype, status_filter="active")
            if e.name == parsed["name"] and not e.invalid_at
        ]
        if existing and existing[0].description == full_desc:
            skipped += 1
            details.append({"file": path.name, "result": "skip-unchanged",
                            "id": existing[0].id})
            continue
        try:
            entry = svc.store(
                domain=mtype, name=parsed["name"], description=full_desc,
                type=ptype, classification=classification,
                memory_type=memtype, importance=importance,
                evidence={"source_file": str(path)},
            )
            imported += 1
            details.append({"file": path.name, "result": "ok", "id": entry.id})
        except Exception as exc:
            failed += 1
            details.append({"file": path.name, "result": f"fail-{exc}"})
    return {
        "imported": imported, "skipped": skipped, "failed": failed,
        "memory_dir": str(mem_dir), "details": details,
    }


@router.post("/backfill-summaries")
def backfill_summaries(project: str = Query("default")) -> dict:
    """Enqueue every active, summary-less memory onto the learning
    pipeline's memory.written bus so the budget-governed consumer pool
    mints a one-line summary (haiku) for the backlog.

    Why this exists: the standalone memory_summary_worker is retired —
    summaries are now minted only in REACTION to a memory.written event
    (event_handlers.handle_memory_written). Memories written before that
    pipeline have no event to drain, so they stay summary='' forever.
    This re-emits the event for each. Idempotent: only active +
    summary-less entries enqueue, so once the pipeline fills a summary
    that entry drops out; the pipeline's input-hash + near-dup gates
    make re-running safe.
    """
    svc = _svc(project)
    from prism_service.services import event_pool as ep
    bus = ep.get_bus()
    queued = 0
    for d in svc.list_domains():
        for e in svc.list_entries(domain=d, status_filter="active"):
            if e.summary or e.invalid_at:
                continue
            bus.emit(ep.Event(
                type=ep.MEMORY_WRITTEN,
                payload={"memory_id": e.id, "project": project},
            ))
            queued += 1
    return {"queued": queued, "project": project}
