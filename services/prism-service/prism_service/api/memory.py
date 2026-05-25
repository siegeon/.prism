"""Memory API — expertise domains, filtered entries, domain stats."""

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from prism_service.data_dir import resolve_claude_home
from prism_service.project_context import get_project
from prism_service.services.claude_transcripts import path_to_slug

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
    return {"entries": rows}


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
