"""Claude Code auto-memory -> PRISM Memory bridge (service layer).

Extracted from api/memory.py (v6.0.8) so the same import logic can run
both from the manual POST /api/memory/import-claude-memories endpoint
AND automatically from the transcript-import poller (v6.2.16). Each
Claude Code memory .md carries YAML frontmatter (name, description,
metadata.type); we map metadata.type to PRISM's (type, classification,
memory_type, importance) tuple. Importance leans high for feedback
because those are "do/don't" rules the model must obey.

Memory-dir resolution order (v6.2.16):
  1. explicit `override_dir` arg (poller/endpoint may pass one)
  2. per-project `claude_memory_path` in understand_state.json
  3. auto: <claude_home>/projects/<slug>/memory
"""

from __future__ import annotations

import re
from pathlib import Path

from prism_service.data_dir import resolve_claude_home
from prism_service.services.claude_transcripts import path_to_slug

# metadata.type -> (type, classification, memory_type, importance)
_CC_TYPE_MAP = {
    "feedback":  ("convention", "tactical",     "procedural", 7),
    "project":   ("decision",   "strategic",    "episodic",   6),
    "user":      ("decision",   "foundational", "semantic",   5),
    "reference": ("convention", "foundational", "semantic",   5),
}


def parse_claude_memory(path: Path) -> dict | None:
    """Extract {name, description, body, metatype} from a Claude Code
    memory .md. Returns None if frontmatter is missing or malformed."""
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


def auto_memory_dir(
    project: str,
    *,
    source_path: str | None = None,
    claude_home: Path | None = None,
) -> Path:
    """The slug-derived default memory dir, ignoring any configured
    override. <claude_home>/projects/<slug>/memory, where the slug comes
    from the project's source_path (falling back to the project id)."""
    sp = (source_path or "").strip()
    if not sp:
        try:
            from prism_service.engines import understand_engine as ue
            sp = (ue._read_state(project).get("source_path") or "").strip()
        except Exception:
            sp = ""
    slug = path_to_slug(sp) if sp else project
    home = claude_home or resolve_claude_home()
    return home / "projects" / slug / "memory"


def configured_project_dir(project: str) -> str:
    """The per-project `claude_project_dir` override (an explicit
    ~/.claude/projects/<slug> folder), or '' if none is set. Both the
    session importer and the memory importer honour it."""
    try:
        from prism_service.engines import understand_engine as ue
        return (ue._read_state(project).get("claude_project_dir") or "").strip()
    except Exception:
        return ""


def resolve_memory_dir(
    project: str,
    *,
    override: str | None = None,
    claude_home: Path | None = None,
) -> Path:
    """Resolve the dir holding a project's Claude Code memory .md files,
    honouring (in order) an explicit override, the per-project
    `claude_project_dir` config (its memory/ subdir), then the
    slug-derived auto path."""
    if override and override.strip():
        return Path(override.strip())
    cpd = configured_project_dir(project)
    if cpd:
        return Path(cpd) / "memory"
    return auto_memory_dir(project, claude_home=claude_home)


def import_project_memories(
    project: str,
    memory_svc,
    *,
    override_dir: str | None = None,
    claude_home: Path | None = None,
) -> dict:
    """Backfill PRISM expertise from Claude Code's auto-memory markdown.

    Idempotent: skips entries whose name + full description already match
    an active entry, so re-running (the 60s poller or a button click)
    doesn't churn the supersede chain. Returns counts + per-file details.
    """
    mem_dir = resolve_memory_dir(
        project, override=override_dir, claude_home=claude_home,
    )
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
        parsed = parse_claude_memory(path)
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
        # Skip if an active entry with this name + identical description
        # already exists, else svc.store would supersede it with a
        # content-identical generation (Graphiti pattern) -> archive noise.
        existing = [
            e for e in memory_svc.list_entries(domain=mtype, status_filter="active")
            if e.name == parsed["name"] and not e.invalid_at
        ]
        if existing and existing[0].description == full_desc:
            skipped += 1
            details.append({"file": path.name, "result": "skip-unchanged",
                            "id": existing[0].id})
            continue
        try:
            entry = memory_svc.store(
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
