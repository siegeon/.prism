"""Codified GATHER step for review_previous_notes (task cd33263f).

Owner: "how can we level up more nodes moving faster programmatically,
finish tasks faster with less tokens as you find issues" — the old
review-previous-notes-loop asked a model to find its OWN citations with
only Read/Glob/Grep (claude_cli.READ_ONLY_TOOLS), which means "review the
prior notes" actually meant "grep the repo and hope", one tool-call round
trip per citation. This module does that retrieval itself, deterministically,
so the agentic step only JUDGES which already-resolved facts are load
bearing.

PURE and CHEAP by construction: every source is an in-process service
already backed by a local sqlite read --
  memory_svc.recall     -> Brain FTS5 / keyword fallback (memory_service.py)
  task_svc.history/list  -> plain SELECT on tasks.db (task_service.py)
  brain_svc.find_symbol  -> exact entity_name lookup on brain.db (brain_engine.py)
Never a model call. Never a git/worktree operation or repo lock — the
2026-08-29 daemon wedge (a status endpoint running `git worktree add` in a
request handler) is exactly the failure mode this module must not
reproduce, so it touches no git state at all.

Every citation returned is one this module ACTUALLY resolved from a real
row — an empty result is reported honestly (see the caller's `reason`
field in api/workflows.py), never papered over with an invented one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Backtick-quoted identifiers (high confidence — the task author already
# named a real symbol/path) OR a bare snake_case/CamelCase token of at
# least 4 chars. Kept intentionally simple: a false-positive candidate
# just fails to resolve via find_symbol and is silently dropped — it can
# never produce a bad citation, only a missed one.
_IDENTIFIER_RE = re.compile(
    r'`([A-Za-z_][A-Za-z0-9_./-]{3,})`'
    r'|\b([A-Z][a-zA-Z0-9]*[a-z][A-Za-z0-9]*|[a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b'
)

_DECISION_ACTIONS = ("advance_task", "gate_decide")


@dataclass(frozen=True)
class GatheredFact:
    kind: str      # "memory" | "decision" | "symbol"
    text: str      # the claim, human-readable
    citation: str  # a citation form premise_grounded's regexes accept


def _candidate_symbols(text: str, limit: int) -> list[str]:
    seen: list[str] = []
    for m in _IDENTIFIER_RE.finditer(text or ""):
        tok = m.group(1) or m.group(2)
        if tok and tok not in seen:
            seen.append(tok)
        if len(seen) >= limit:
            break
    return seen


def _gather_memories(task, memory_svc, limit: int) -> list[GatheredFact]:
    if memory_svc is None:
        return []
    query = (task.title or task.description or "").strip()
    if not query:
        return []
    try:
        entries = memory_svc.recall(query, limit=limit)
    except Exception:
        return []
    out = []
    for e in entries:
        snippet = (getattr(e, "description", "") or "").strip()
        if len(snippet) > 140:
            snippet = snippet[:140].rstrip() + "..."
        name = getattr(e, "name", "") or getattr(e, "id", "")
        if not name:
            continue
        out.append(GatheredFact(
            kind="memory",
            text=f"Memory '{name}': {snippet}" if snippet else f"Memory '{name}'",
            # backtick output form (_CLAIM_OUTPUT_RE: whitespace inside
            # backticks) — this literally reports the retrieval call made.
            citation=f"`memory_recall(\"{query[:60]}\") -> {name}`",
        ))
    return out


def _gather_decisions(task, task_svc, history_limit: int, neighbour_limit: int) -> list[GatheredFact]:
    if task_svc is None:
        return []
    out: list[GatheredFact] = []
    try:
        rows = [h for h in task_svc.history(task.id) if h.action in _DECISION_ACTIONS]
    except Exception:
        rows = []
    for h in rows[-history_limit:]:
        detail = (h.details or "").strip()
        if len(detail) > 120:
            detail = detail[:120].rstrip() + "..."
        out.append(GatheredFact(
            kind="decision",
            text=f"{h.action} on this task by {h.actor}: {detail}" if detail
                 else f"{h.action} on this task by {h.actor}",
            citation=f"`task_history({task.id[:8]}) -> {h.action} at {h.timestamp}`",
        ))

    neighbours = []
    try:
        if task.parent_id:
            neighbours = [t for t in task_svc.list(parent_id=task.parent_id) if t.id != task.id]
        elif task.tags:
            neighbours = [t for t in task_svc.list(tag=task.tags[0]) if t.id != task.id]
    except Exception:
        neighbours = []
    for n in neighbours[:neighbour_limit]:
        try:
            n_rows = [h for h in task_svc.history(n.id) if h.action in _DECISION_ACTIONS]
        except Exception:
            n_rows = []
        for h in n_rows[-1:]:
            detail = (h.details or "").strip()
            if len(detail) > 100:
                detail = detail[:100].rstrip() + "..."
            out.append(GatheredFact(
                kind="decision",
                text=f"Neighbour task {n.id[:8]} ({n.title}): {h.action}"
                     + (f" - {detail}" if detail else ""),
                citation=f"`task_history({n.id[:8]}) -> {h.action} at {h.timestamp}`",
            ))
    return out


def _gather_symbols(task, brain_svc, limit: int) -> list[GatheredFact]:
    if brain_svc is None:
        return []
    blob = f"{task.title}\n{task.description}"
    out = []
    for tok in _candidate_symbols(blob, limit):
        try:
            rows = brain_svc.find_symbol(tok, limit=1)
        except Exception:
            rows = []
        if not rows:
            continue
        row = rows[0]
        src = row.get("source_file") or ""
        line = row.get("line_start") or 0
        if not src or not line:
            continue
        out.append(GatheredFact(
            kind="symbol",
            text=f"'{tok}' is defined at {src}:{line}",
            citation=f"{src}:{line}",
        ))
    return out


def gather(
    task,
    memory_svc=None,
    task_svc=None,
    brain_svc=None,
    memory_limit: int = 5,
    history_limit: int = 3,
    neighbour_limit: int = 3,
    symbol_limit: int = 10,
    max_facts: int = 15,
) -> list[GatheredFact]:
    """Return grounded facts for `task` — deterministic, no model call.

    Every element's `citation` already satisfies one of
    arc_governance's grounding regexes (file:line, or backtick output),
    so an agentic judge that reuses a fact's citation verbatim will
    always pass premise_grounded's citation tooth."""
    facts: list[GatheredFact] = []
    facts.extend(_gather_memories(task, memory_svc, memory_limit))
    facts.extend(_gather_decisions(task, task_svc, history_limit, neighbour_limit))
    facts.extend(_gather_symbols(task, brain_svc, symbol_limit))
    return facts[:max_facts]


# ----------------------------------------------------------------------
# Codified CHECK step (task cd33263f)
# ----------------------------------------------------------------------
# arc_governance.py is a control_plane.POLICY_FILES entry (stop_if: "the
# slice edits any file in control_plane.POLICY_FILES") — this module reuses
# its grounding predicates by IMPORT, read-only, rather than adding a new
# public function there. Same regexes as score_premise_grounded, so this
# verdict can never drift from what the real story_gate rubric decides;
# score_premise_grounded itself is untouched.

def citation_check(notes_md: str, claims_section: str = "premises") -> dict:
    """Report which claim bullets under `claims_section` carry no citation
    and no REFUTED/UNVERIFIED/UNRESOLVED marker. Pure regex, no model call.

    Returns {"ok": bool, "section_present": bool, "claims_checked": int,
    "failing": [{"claim": str, "reason": str}], "reason": str}."""
    from prism_service.services.arc_governance import (
        _claim_is_grounded, _claim_lines, _find_section, _sections,
    )

    notes = str(notes_md or "")
    sections = _sections(notes) if notes.strip() else {}
    section_body = _find_section(sections, claims_section)
    if section_body is None:
        return {"ok": False, "section_present": False, "claims_checked": 0,
                "failing": [],
                "reason": f"citation_check: no '{claims_section}' section present"}
    claims = _claim_lines(section_body)
    if not claims:
        return {"ok": False, "section_present": True, "claims_checked": 0,
                "failing": [],
                "reason": (f"citation_check: '{claims_section}' section "
                           "has no recognised claim bullet")}
    failing = [{"claim": c[:200],
                "reason": ("no file:line, run/PR/commit/issue id, backtick "
                           "command output, or REFUTED/UNVERIFIED/UNRESOLVED marker")}
               for c in claims if not _claim_is_grounded(c)]
    ok = not failing
    reason = (f"citation_check: {len(claims)} claim(s), all grounded" if ok
              else f"citation_check: {len(failing)} of {len(claims)} claim(s) ungrounded")
    return {"ok": ok, "section_present": True, "claims_checked": len(claims),
            "failing": failing, "reason": reason}
