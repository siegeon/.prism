"""Per-project Claude source persistence + auto-memory bridge (v6.3.16).

The transcript-import poller (claude_transcripts.start_transcript_importer)
references ``claude_memory.configured_project_dir(pid)`` to resolve a
per-project ``override_dir`` and ``claude_memory.import_project_memories``
to bridge Claude Code auto-memory into PRISM. Until now the module did not
exist, so ``from prism_service.services import claude_memory`` always
failed -> cm=None -> the override branch never fired and customers whose
Claude project slug did not match their PRISM host path never imported.

This module is the persistence seam. The stored dir lives under the
project's ``understand_state.json`` ``claude_project_dir`` key — the SAME
key the Settings editor / register_claude_source MCP tool write via
``understand_engine._write_state``. ``configured_project_dir`` reads it back.
"""

from __future__ import annotations


def configured_project_dir(project_id: str) -> str | None:
    """Return the persisted ``claude_project_dir`` for ``project_id`` (the
    explicit Claude transcript dir reported by Claude / set in Settings), or
    ``None`` when unconfigured so the poller falls back to slug auto-discovery.

    Reads understand_state.json via the canonical reader the Settings editor
    uses, so the value the MCP tool / POST endpoint wrote is exactly what the
    poller reads — no second store, no drift.
    """
    from prism_service.engines import understand_engine as ue

    try:
        state = ue._read_state(project_id)
    except Exception:
        return None
    val = (state.get("claude_project_dir") or "").strip()
    return val or None


def import_project_memories(project_id: str, memory_svc=None, claude_home=None) -> dict:
    """Bridge Claude Code auto-memory markdown into PRISM Memory for a project.

    Best-effort, idempotent — delegates to the existing markdown importer in
    api/memory.py so the poller and the /memory button share ONE code path.
    Returns ``{imported, skipped, failed, ...}``; never raises (the poller
    treats a memory-import failure as non-fatal to transcript import).
    """
    try:
        from prism_service.api.memory import import_claude_memories
        return import_claude_memories(project=project_id)
    except Exception as exc:  # pragma: no cover - defensive
        return {"imported": 0, "skipped": 0, "failed": 0, "error": str(exc)}
