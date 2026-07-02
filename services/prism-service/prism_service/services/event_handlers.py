"""Phase 3 (epic 4fd1e6b4) — the REAL learning handlers that run on the
event bus, replacing the Phase-1 wrap/no-op layer in event_pool and the
five polling timer daemons (Reflection / Memory Summary / Memory Ops
Merge / Transcript->candidate / policy-nudge).

Each handler is deterministic-gated, coalesced, and model-right-sized:

  session.imported  -> reflect. READ existing knowledge first; reinforce a
                       matching memory instead of minting a duplicate. A
                       deterministic noise filter (reflection_worker.
                       _is_noise_candidate) gates the claude -p call, and a
                       burst of imports coalesces into ONE reflection pass.
  memory.written    -> dedup/supersede DETECTION is deterministic (string-
                       sim + entity/predicate match); only a true novelty is
                       summarized via claude --model haiku. Re-emitting the
                       same write is input-hash skipped.
  memory.recalled+outcome -> ZERO LLM credit assignment: bump utility on the
                       SPECIFIC memories in this recall's recall_log; the
                       global knob nudge is a secondary output only.

Wired into the process-singleton bus by event_pool.default_registry(); the
two claude -p handlers register inference=True so the BudgetGovernor charges
and circuit-breaks them.
"""

from __future__ import annotations

import sqlite3
import sys
from difflib import SequenceMatcher

# Deterministic near-dup threshold — mirrors MemoryService.store's 0.85
# supersede gate so detection here agrees with the write path.
_NEAR_DUP_RATIO = 0.85

# Per-(project, input-hash) guard so re-emitting the SAME write does not
# re-run inference (criterion 8). Process-local set; the daemon is one
# process so this is the right scope.
_SEEN_WRITE_HASHES: set[str] = set()


def _log(msg: str) -> None:
    print(f"[event_handlers] {msg}", file=sys.stderr, flush=True)


def _ctx_for(payload: dict):
    """Resolve the project_context for an event payload. Falls back to the
    default project when the payload omits one (the memory.written /
    recalled+outcome emit seams carry only ids)."""
    from prism_service.config import list_projects
    from prism_service.project_context import get_project

    proj = payload.get("project")
    if proj:
        return get_project(proj)
    # The emit seams (mcp/tools.py) run inside a single project context but
    # don't stamp it on the payload; in the daemon there is exactly one
    # active project per data dir, so the first known project is correct.
    projects = list_projects()
    return get_project(projects[0]) if projects else None


# ======================================================================
# memory.recalled+outcome — ZERO LLM credit assignment.
# ======================================================================
def handle_recalled_outcome(event) -> None:
    """Bump per-memory utility on the SPECIFIC memories recalled into this
    task's outcome (criterion 9). Pure arithmetic over recall_log — never
    shells claude. The global policy-knob nudge is a SECONDARY output."""
    payload = event.payload or {}
    task_id = payload.get("task_id")
    if not task_id:
        return
    ctx = _ctx_for(payload)
    mem = getattr(ctx, "memory_svc", None) if ctx else None
    if mem is None:
        return
    # The recall_log already carries the outcome (record_outcome ran on the
    # task transition upstream of the emit). Recompute effectiveness for ONLY
    # the entries this task recalled — un-recalled memories are never touched.
    rows = mem._recall_db.execute(
        "SELECT DISTINCT entry_id FROM recall_log WHERE task_id=?",
        (task_id,),
    ).fetchall()
    for (entry_id,) in rows:
        try:
            mem._recalculate_effectiveness(entry_id)
        except Exception as exc:  # noqa: BLE001
            _log(f"recalc effectiveness({entry_id}) failed: {exc}")
    # SECONDARY output only: nudge the global policy knob off the aggregate
    # signal. Best-effort and never an LLM call.
    _emit_global_knob_nudge(ctx, payload.get("outcome", ""))


def _emit_global_knob_nudge(ctx, outcome: str) -> None:
    """Secondary, best-effort global knob nudge from the recall->outcome
    signal. Deliberately tiny: the per-memory credit above is the PRIMARY
    output; this only records that a nudge was considered."""
    try:
        _log(f"knob-nudge considered (outcome={outcome!r})")
    except Exception:
        pass


# ======================================================================
# memory.written — deterministic dedup DETECTION + haiku summarize.
# ======================================================================
def _entity_predicate_match(a: str, b: str) -> bool:
    """Cheap deterministic entity/predicate agreement: the two memories
    share most of their significant word stems. Combined with the string-
    sim ratio this distinguishes a near-duplicate from two unrelated
    memories that happen to share boilerplate phrasing."""
    def _stems(s: str) -> set[str]:
        toks = "".join(c.lower() if c.isalnum() else " " for c in s).split()
        return {t[:5] for t in toks if len(t) > 2}
    sa, sb = _stems(a), _stems(b)
    if not sa or not sb:
        return False
    overlap = len(sa & sb) / max(1, min(len(sa), len(sb)))
    return overlap >= 0.6


def _find_near_dup(mem, entry):
    """Deterministically locate an existing memory the given entry is a
    near-duplicate of. Scans the same domain (any status, excluding self),
    requiring BOTH a high string-sim ratio AND entity/predicate agreement —
    no claude. Returns the matched entry or None."""
    best = None
    best_ratio = 0.0
    for other in mem._read_entries(entry.domain):
        if other.id == entry.id:
            continue
        ratio = SequenceMatcher(
            None, other.description.lower(), entry.description.lower()
        ).ratio()
        if ratio < _NEAR_DUP_RATIO:
            continue
        if not _entity_predicate_match(other.description, entry.description):
            continue
        if ratio > best_ratio:
            best_ratio, best = ratio, other
    return best


def handle_memory_written(event) -> None:
    """dedup/supersede DETECTION is deterministic; only a true novelty is
    summarized via claude --model haiku (criteria 6-8)."""
    payload = event.payload or {}
    mid = payload.get("memory_id")
    if not mid:
        return
    ctx = _ctx_for(payload)
    mem = getattr(ctx, "memory_svc", None) if ctx else None
    if mem is None:
        return
    entry = mem.get_entry(mid)
    if entry is None:
        return

    # Criterion 8 — input-hash skip: a re-emit of the SAME write is a no-op
    # (no detection, no inference). Hash the user-supplied content only.
    ihash = f"{entry.domain}:{entry.name}:{hash(entry.description)}"
    if ihash in _SEEN_WRITE_HASHES:
        return
    _SEEN_WRITE_HASHES.add(ihash)

    # Criterion 6 — deterministic near-dup detection (zero claude). On a
    # match, reinforce the original (bump its recall/signal) and STOP — no
    # summarize, no merge-rewrite escalation for a clean duplicate.
    match = _find_near_dup(mem, entry)
    if match is not None:
        try:
            mem.record_recall(match.id)
        except Exception as exc:  # noqa: BLE001
            _log(f"reinforce({match.id}) failed: {exc}")
        return

    # Novelty — summarize with the small model. Criterion 7 pins --model haiku.
    _summarize_haiku(ctx, mem, entry)


def _summarize_haiku(ctx, mem, entry) -> None:
    """Summarize a novel memory and persist it. Routed through the
    memory-summary backend seam (task cba42d6b, claude-p-exit epic):
    the default stays `claude --model haiku` exactly as before;
    PRISM_MEMORY_SUMMARY_BACKEND=local serves the same distillation
    prompt from the local micro model and records a claude_runs ledger
    row (backend='local' + token counts). Best-effort: an auth/parse
    failure leaves summary empty for a later retry, never crashes the
    pool."""
    from prism_service.services.memory_summary_worker import (
        _invoke_backend, render_prompt)

    work_dir = str(ctx._data_dir) if ctx is not None else "."
    project = str(getattr(ctx, "project_id", "") or "") if ctx else ""
    try:
        result = _invoke_backend(
            prompt=render_prompt(entry.name, entry.description),
            work_dir=work_dir,
            project=project,
            purpose="memory_summary",
            model="haiku",   # criterion 7 pin on the claude default path
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"summarize failed: {exc}")
        return
    text = (result.final_text() or "").strip() if result.exit_code == 0 else ""
    if text:
        try:
            mem.update_entry(entry.id, summary=text)
        except Exception as exc:  # noqa: BLE001
            _log(f"update_entry summary failed: {exc}")


# ======================================================================
# session.imported — coalesced reflect, read-before-write, noise-gated.
# ======================================================================
def _project_of_import(payload: dict) -> str:
    """Resolve the project a session.imported event belongs to."""
    proj = payload.get("project")
    if proj:
        return proj
    from prism_service.config import list_projects
    projects = list_projects()
    return projects[0] if projects else ""


def _disposition(scores_db: str, candidate_ids: list[str]) -> None:
    """Move processed/skipped candidates OUT of 'pending' so the burst
    can't re-trigger a reflection pass (coalesce) and noise isn't re-claimed
    forever. Deterministic, no inference."""
    if not candidate_ids:
        return
    try:
        conn = sqlite3.connect(scores_db)
        try:
            conn.executemany(
                "UPDATE consolidation_candidates SET status='completed' "
                "WHERE id=?",
                [(cid,) for cid in candidate_ids],
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _log(f"disposition failed: {exc}")


def handle_session_imported(event) -> None:
    """Reflect on the project's pending candidates as ONE coalesced pass
    (criterion 4). Deterministic noise candidates are skipped without claude
    (criterion 5); proposed memories that match existing knowledge REINFORCE
    instead of minting a duplicate (criterion 3)."""
    from prism_service.project_context import get_project
    from prism_service.services.reflection_worker import _is_noise_candidate

    payload = event.payload or {}
    project = _project_of_import(payload)
    if not project:
        return
    try:
        ctx = get_project(project)
    except Exception as exc:  # noqa: BLE001
        _log(f"get_project({project}) failed: {exc}")
        return
    scores_db = str(ctx._data_dir / "scores.db")

    try:
        conn = sqlite3.connect(scores_db)
        try:
            rows = conn.execute(
                "SELECT id FROM consolidation_candidates "
                "WHERE status='pending' ORDER BY queued_at ASC"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return
    pending = [r[0] for r in rows]
    if not pending:
        return  # burst already coalesced — nothing to reflect

    noise = [c for c in pending if _is_noise_candidate(scores_db, c)]
    real = [c for c in pending if c not in noise]
    # Noise is deterministically dispositioned out of pending, never reflected.
    _disposition(scores_db, noise)
    if not real:
        return  # only noise — zero claude calls (criterion 5)

    # ONE coalesced reflection pass over the real-signal candidates
    # (criterion 4): reflect the head, then disposition the whole burst.
    verdict = _reflect_once(project, ctx, scores_db, real[0])
    _persist_reflection(ctx, verdict)
    _disposition(scores_db, real)


def _reflect_once(project: str, ctx, scores_db: str, candidate_id: str) -> dict:
    """Build the reflect prompt for one candidate and run ONE inference
    through reflection_runner's backend seam (task cba42d6b): the default
    stays the claude CLI with the exact prior arguments; PRISM_REFLECTION_
    BACKEND=local|pi routes the same brief at the micro-model backends.
    Returns the parsed verdict ({} on any failure)."""
    from prism_service.services import reflection_runner as rr
    from prism_service.services import source_service as ss

    conn = sqlite3.connect(scores_db)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, task_id, session_id, scope_json, trigger, status "
            "FROM consolidation_candidates WHERE id=?", (candidate_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    import json as _json
    try:
        scope = _json.loads(row["scope_json"] or "{}")
    except Exception:
        scope = {}
    try:
        source_dir = ss.source_dir_for(project)
    except Exception:
        source_dir = ctx._data_dir
    prompt = rr._build_prompt(project, str(source_dir), candidate_id, row, scope)
    try:
        result = rr._invoke_backend(
            prompt=prompt, work_dir=str(source_dir), plugin_dir=str(source_dir),
            max_turns=15, allowed_tools=rr.DEFAULT_REFLECT_TOOLS,
            project=project, purpose="prism-reflect",
        )
    except Exception as exc:  # noqa: BLE001
        _log(f"reflect invoke failed: {exc}")
        return {}
    raw = result.final_text() or ""
    try:
        return _json.loads(rr._strip_fence(raw))
    except Exception:
        return {}


def _persist_reflection(ctx, verdict: dict) -> None:
    """Persist verdict.new_memories with READ-BEFORE-WRITE: recall/brain_search
    first and REINFORCE a matching memory (record_recall) rather than mint a
    duplicate row (criterion 3)."""
    mem = getattr(ctx, "memory_svc", None)
    if mem is None or not isinstance(verdict, dict):
        return
    for nm in verdict.get("new_memories") or []:
        if not isinstance(nm, dict):
            continue
        if not all(nm.get(k) for k in
                   ("domain", "name", "description", "type", "classification")):
            continue
        match = _recall_match(mem, nm)
        if match is not None:
            try:
                mem.record_recall(match.id)  # reinforce, no duplicate row
            except Exception as exc:  # noqa: BLE001
                _log(f"reinforce({match.id}) failed: {exc}")
            continue
        try:
            mem.store(
                domain=nm["domain"], name=nm["name"],
                description=nm["description"], type=nm["type"],
                classification=nm["classification"],
                memory_type=nm.get("memory_type", "episodic"),
                importance=int(nm.get("importance", 5)),
                evidence={"source": "session.imported"},
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"store new memory failed: {exc}")


def _recall_match(mem, nm: dict):
    """READ existing knowledge (memory_recall) and return the existing entry
    the proposed memory near-duplicates, or None. Deterministic confirm via
    string-sim so a loose recall hit doesn't suppress a genuine novelty."""
    desc = nm.get("description", "")
    try:
        hits = mem.recall(f"{nm.get('name', '')} {desc}",
                          domain=nm.get("domain"), limit=5)
    except Exception:
        hits = []
    for cand in hits:
        ratio = SequenceMatcher(
            None, cand.description.lower(), desc.lower()
        ).ratio()
        if ratio >= _NEAR_DUP_RATIO:
            return cand
    return None
