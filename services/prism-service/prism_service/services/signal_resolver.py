"""Signal resolver -- resolves a signal against PRISM's ontology on arrival
(task 785bb4ce).

Ontology today is service-level facts (TaskService rows, the memory service,
the brain engine), NOT the sqlite ontology_store.py classes/instances table --
a sibling epic is moving that store onto an RDF graph, so this resolver reads
through the same in-process services every other consumer uses (never HTTP,
never the ontology tables directly):

  - related tasks: TaskService.list() (channel_ref / title overlap / an
    id-like token in the subject), with a brain-search top-up when available.
  - concepts: MemoryService.recall() (memory_recall's own dispatch target).
  - code: BrainService.search() (brain_search's own dispatch target).
  - people: ActorService.resolve() (the same resolver Task/TaskHistory
    actors go through).

Every leg is wrapped so an absent/empty index degrades to [] plus a stated
reason under matches["reasons"] -- never an exception; resolution is
best-effort and must never fail signal intake.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any

from prism_service.models.signal import Signal

_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "to", "of", "in", "on", "is",
    "at", "by", "with", "about", "re", "fwd",
    "can", "you", "before", "after", "it", "this", "that", "we", "our",
    "please", "should", "would", "could", "will", "be", "are", "was",
    "were", "from", "as", "do", "does",
}

_CAP = 5

# related_tasks title-overlap scoring (task 461b7985): idf floor keeps a
# tiny board (even N=1) from zeroing out every token's weight, and the
# threshold requires more than one floor-weighted token's worth of score
# so a lone, board-wide-common word can't match alone -- while a genuinely
# rare token (real idf) still can.
_MIN_TOKEN_IDF = 0.1
_RELATED_SCORE_THRESHOLD = 0.15


def resolve(project: str, signal: Signal) -> dict[str, Any]:
    """Resolve `signal` against real PRISM service-level facts. Never
    raises -- callers (POST /api/signals create, POST .../resolve) treat
    this as best-effort and persist whatever comes back."""
    reasons: dict[str, str] = {}

    # task 31b737fb: parse() once here -- the graph projection reads this
    # persisted extraction back off the signal, never re-parses.
    extraction = _safe(lambda: _parse_signal(signal), None)
    extraction_dict = extraction.model_dump() if extraction is not None else {}

    channel = _channel_match(signal)
    related_tasks = _safe(
        lambda: _related_tasks(project, signal, extraction_dict), [])
    concepts, concepts_reason = _concepts(project, signal)
    if concepts_reason:
        reasons["concepts"] = concepts_reason
    code, code_reason = _code_matches(project, signal)
    if code_reason:
        reasons["code"] = code_reason
    has_deadline = bool(extraction_dict.get("deadlines"))
    match_subject, match_body = _match_text(signal)
    ask = _classify_ask(match_subject, match_body, has_deadline=has_deadline)
    people, people_reason = _people(signal, extraction_dict)
    if people_reason:
        reasons["people"] = people_reason

    matches: dict[str, Any] = {
        "channel": channel,
        "related_tasks": related_tasks,
        "concepts": concepts,
        "code": code,
        "ask": ask,
        "people": people,
        "extraction": extraction_dict,
        "resolved_at": datetime.now(timezone.utc).isoformat(),
    }
    if reasons:
        matches["reasons"] = reasons
    _apply_enrichment_gate(matches, ask, signal)
    return matches


def _parse_signal(signal: Signal):
    from prism_service.services.signal_parse import parse as parse_signal
    return parse_signal(signal)


def _derive_bucket(ask_kind: str | None) -> str:
    """A plain, testable heuristic feeding gate_enrichment -- the gate
    itself (never this heuristic) is what the vocabulary is checked
    against (task 31b737fb)."""
    if ask_kind in ("decision", "review", "deliverable"):
        return "needs_attention"
    if ask_kind == "fyi":
        return "team_updates"
    return "low_priority"


def _apply_enrichment_gate(matches: dict[str, Any], ask: dict[str, str],
                            signal: Signal) -> None:
    """Best-effort: validate a classifier-shaped view of this signal
    (ask_kind/bucket/channel) through gate_enrichment, persisting whatever
    it holds back onto matches['held_back'] (SignalStore.update persists
    it) -- never raises, never blocks intake."""
    def _run():
        from prism_service.services.signal_parse import HeldBack, gate_enrichment
        raw = {"ask_kind": ask.get("kind"),
               "bucket": _derive_bucket(ask.get("kind")),
               "channel": signal.channel}
        result = gate_enrichment(raw)
        if isinstance(result, HeldBack):
            matches["held_back"] = result.held
        else:
            matches["enrichment"] = result.model_dump()
    _safe(_run, None)


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_-]+", (text or "").lower())
            if t and t not in _STOPWORDS and len(t) > 2}


def _match_text(signal: Signal) -> tuple[str, str]:
    """(subject, body) to match against -- the ALIGNED text (task
    ed034701) when SignalStore.create() produced it, else the raw text
    as it arrived. A task titled with the lexicon's canonical term
    ("Task") should still match a signal that arrived saying "ticket"."""
    subject = signal.aligned_subject or signal.subject
    body = signal.aligned_body or signal.body
    return subject, body


def _channel_match(signal: Signal) -> dict[str, Any]:
    from prism_service.models.task import CHANNELS
    return {"id": signal.channel, "known": signal.channel in CHANNELS}


def _extraction_refs(extraction: dict | None) -> set[str]:
    """Every reference parse() found (ticket keys, PR/issue refs,
    permalinks) as a flat set of strings a task's channel_ref might equal
    (task 31b737fb: related_tasks also matches on what was extracted)."""
    extraction = extraction or {}
    refs: set[str] = {t.get("key") for t in extraction.get("tickets", [])
                       if t.get("key")}
    for c in extraction.get("code_refs", []):
        number = c.get("number")
        if number is None:
            continue
        if c.get("repo"):
            refs.add(f"{c['repo']}#{number}")
        refs.add(f"#{number}")
    refs.update(p for p in extraction.get("permalinks", []) if p)
    return refs


def _related_tasks(project: str, signal: Signal,
                    extraction: dict | None = None) -> list[dict]:
    """Real TASKS rows (TaskService), never the ontology sqlite tables:
    same channel_ref, an extracted reference (ticket key / PR ref /
    permalink) equal to a task's channel_ref, then title/subject token
    overlap incl. an id-like token in the subject, capped at 5."""
    from prism_service.project_context import get_project

    extracted_refs = _extraction_refs(extraction)
    tasks = get_project(project).task_svc.list()
    match_subject, match_body = _match_text(signal)
    subject_text = f"{match_subject} {match_body}".lower()
    subject_tokens = _tokens(match_subject)
    title_tokens = {t.id: _tokens(t.title) for t in tasks}
    n = len(tasks)
    df: dict[str, int] = {}
    for toks in title_tokens.values():
        for tok in toks:
            df[tok] = df.get(tok, 0) + 1

    def _idf(tok: str) -> float:
        return max(math.log(n / (1 + df.get(tok, 0))), _MIN_TOKEN_IDF)

    out: list[dict] = []
    for t in tasks:
        why = ""
        if signal.channel_ref and t.channel_ref and t.channel_ref == signal.channel_ref:
            why = "same channel_ref"
        elif t.channel_ref and t.channel_ref in extracted_refs:
            why = f"matched extracted reference ({t.channel_ref})"
        elif t.id and (t.id.lower() in subject_text or t.id[:8].lower() in subject_text):
            why = f"id-like token match ({t.id[:8]})"
        else:
            overlap = subject_tokens & title_tokens[t.id]
            if overlap:
                score = sum(_idf(tok) for tok in overlap)
                if score > _RELATED_SCORE_THRESHOLD:
                    why = f"title overlap: {', '.join(sorted(overlap))}"
        if why:
            out.append({"id": t.id, "title": t.title, "why": why})
        if len(out) >= _CAP:
            return out

    # Best-effort brain-search top-up over task titles, when the brain
    # index carries them (domain='task') -- absent index is not an error.
    if len(out) < _CAP:
        seen = {r["id"] for r in out}

        def _top_up():
            from prism_service.project_context import get_project as gp
            hits = gp(project).brain_svc.search(
                signal.subject or signal.body, domain="task", limit=_CAP,
            )
            by_id = {t.id: t for t in tasks}
            for h in hits:
                tid = (h.get("doc_id") or "").split("/")[-1].replace("::main", "")
                task = by_id.get(tid)
                if task and task.id not in seen:
                    out.append({"id": task.id, "title": task.title,
                                "why": "brain search"})
                    seen.add(task.id)
                if len(out) >= _CAP:
                    break
        _safe(_top_up, None)
    return out[:_CAP]


def _concepts(project: str, signal: Signal) -> tuple[list[dict], str]:
    """MemoryService.recall() -- memory_recall's own in-process dispatch
    target (mcp/tools.py calls the identical method)."""
    match_subject, match_body = _match_text(signal)
    query = f"{match_subject} {match_body}".strip()
    if not query:
        return [], "signal has no subject or body to search"
    try:
        from prism_service.project_context import get_project
        entries = get_project(project).memory_svc.recall(query=query, limit=_CAP)
    except Exception as exc:  # pragma: no cover - defensive
        return [], f"memory recall failed: {exc}"
    if not entries:
        return [], "no memory entries matched (index empty or no overlap)"
    concepts = [
        {"id": e.id, "title": e.name, "score": round(max(0.0, 1.0 - i * 0.15), 2)}
        for i, e in enumerate(entries[:_CAP])
    ]
    return concepts, ""


def _code_matches(project: str, signal: Signal) -> tuple[list[dict], str]:
    """BrainService.search() -- brain_search's own in-process dispatch
    target (mcp/tools.py calls the identical method)."""
    match_subject, match_body = _match_text(signal)
    query = f"{match_subject} {match_body}".strip()
    if not query:
        return [], "signal has no subject or body to search"
    try:
        from prism_service.project_context import get_project
        hits = get_project(project).brain_svc.search(query=query, limit=_CAP)
    except Exception as exc:  # pragma: no cover - defensive
        return [], f"brain search failed: {exc}"
    if not hits:
        return [], "no brain index results (index empty or no matches)"
    code = [
        {"path": h.get("source_file") or "", "symbol": h.get("entity_name") or "",
         "score": h.get("rrf_score", 0.0)}
        for h in hits[:_CAP]
    ]
    return code, ""


def _word_in(text: str, *words: str) -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def _classify_ask(subject: str, body: str,
                   has_deadline: bool = False) -> dict[str, str]:
    """Documented keyword heuristic, checked in a fixed priority order so a
    subject/body that trips more than one bucket lands on the most
    actionable reading (a decision beats a plain FYI). has_deadline (task
    31b737fb): true when parse()'s own deadline extraction (before
    Friday/EOD tomorrow/ISO dates) found one -- a real deadline is a
    deliverable signal even when 'by <date>' isn't the literal phrasing."""
    text = f"{subject or ''} {body or ''}".lower()

    if _word_in(text, "approve", "decide", "choose"):
        return {"kind": "decision",
                "reason": "contains a decision verb (approve/decide/choose)"}
    if _word_in(text, "review", "pr", "look at") or "look at" in text:
        return {"kind": "review",
                "reason": "asks for a review (review/PR/look at)"}
    if _word_in(text, "deliver", "send") or re.search(r"\bby\s+\w", text):
        return {"kind": "deliverable",
                "reason": "names a deliverable/deadline (deliver/send/by <date>)"}
    if has_deadline:
        return {"kind": "deliverable",
                "reason": "a deadline was resolved from the signal's text"}
    if "?" in text or "can you" in text or "please reply" in text:
        return {"kind": "reply",
                "reason": "asks a question or requests a reply"}
    if _word_in(text, "fyi") or "heads up" in text:
        return {"kind": "fyi", "reason": "informational (fyi/heads up)"}
    return {"kind": "unknown", "reason": "no keyword heuristic matched"}


def _people(signal: Signal,
            extraction: dict | None = None) -> tuple[list[dict], str]:
    """ActorService.resolve() -- the same email/name resolver Task and
    TaskHistory actors go through (services/actor_service.py). Resolves
    by ADDRESS only, never by a display name: signal.sender first, and
    (task 31b737fb) an address parse() found in the body when there is no
    sender -- 'an address is evidence, a name is a guess'."""
    address = signal.sender or next(
        iter((extraction or {}).get("addresses", [])), "")
    if not address:
        return [], "signal has no sender"
    try:
        from prism_service.models.actor import ActorKind
        from prism_service.services.actor_service import get_actor_service
        actor = get_actor_service().resolve(address)
    except Exception as exc:  # pragma: no cover - defensive
        return [], f"actor resolution failed: {exc}"
    if actor.kind != ActorKind.HUMAN:
        return [], f"sender '{address}' did not resolve to a known actor"
    return [{"name": actor.display_name, "actor_id": actor.id}], ""
