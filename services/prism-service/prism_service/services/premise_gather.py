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

import re as _re

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


# Words that carry no topic. A query built from a whole sentence embeds
# into generic-workflow space and returns the same globally-popular
# memories for every task (measured 2026-09-05: four unrelated tasks got
# the same five feedback-* entries, two identical across all four).
_QUERY_STOPWORDS = frozenset("""
about after against all also always among another any anything are because
been before being below between both call called comes could does done down
each either else enough even ever every everything from full gets give given
goes gone have here into itself just keep kept known leave leaves left less
like made make makes many more most much must never next none only onto open
other over própria same shall should show shows since some something still
such take taken than that the their them themselves then there these they
thing things this those through thus together under until upon used uses
using very what when where which while whose will with within without would
your yours localhost http https task node runs step steps
""".split())

_MEMORY_TERM_LIMIT = 4          # how many topical terms we ask about
_MEMORY_PER_TERM = 3            # how many candidates each term may offer
_MEMORY_KEEP_LIMIT = 4          # HARD CAP on memories that reach the report


def _query_terms(text: str, limit: int = _MEMORY_TERM_LIMIT) -> list[str]:
    """The distinctive topical words in `text`, longest-lived first.

    Recall is asked ONE TERM AT A TIME rather than one long sentence: the
    corpus does hold task-specific memories, but a full title/oracle embeds
    into generic space and never reaches them, while the single word
    "planner" returns a-split-is-unsafe-until-the-planner-proves-it
    immediately (both measured live).
    """
    out: list[str] = []
    for m in _re.finditer(r"[A-Za-z][A-Za-z0-9_]+", text or ""):
        w = m.group(0).lower()
        if len(w) <= 4 or w in _QUERY_STOPWORDS or w in out:
            continue
        out.append(w)
        if len(out) >= limit:
            break
    return out


def _term_is_present(term: str, entry) -> bool:
    """True when the term that FOUND this memory actually appears in it.

    THE RELEVANCE FLOOR. A vector recall always returns its nearest
    neighbours, so a term with no real match still yields entries -- that
    is how "vocabulary" produced signal-loss-pill-live-verified, and how
    unrelated memories ended up cited as a task's own premises. Requiring
    the term to appear turns "nothing matched" into an honest empty
    result instead of confident noise.
    """
    stem = term[:-1] if term.endswith("s") and len(term) > 4 else term
    hay = " ".join([
        str(getattr(entry, "name", "") or ""),
        str(getattr(entry, "summary", "") or ""),
        str(getattr(entry, "description", "") or "")[:400],
    ]).lower()
    return stem in hay


def _gather_memories(task, memory_svc, limit: int) -> list[GatheredFact]:
    if memory_svc is None:
        return []
    subject = " ".join([str(getattr(task, "title", "") or ""),
                        str(getattr(task, "oracle", "") or "")]).strip()
    terms = _query_terms(subject) or _query_terms(
        str(getattr(task, "description", "") or ""))
    if not terms:
        return []

    entries, seen, query = [], set(), ""
    for term in terms:
        if len(entries) >= _MEMORY_KEEP_LIMIT:
            break
        try:
            found = memory_svc.recall(term, limit=_MEMORY_PER_TERM)
        except Exception:
            continue
        for e in found:
            name = getattr(e, "name", "") or getattr(e, "id", "")
            if not name or name in seen:
                continue
            if not _term_is_present(term, e):
                continue        # nearest neighbour, not a real match
            seen.add(name)
            entries.append((term, e))
            if len(entries) >= _MEMORY_KEEP_LIMIT:
                break

    out = []
    for query, e in entries:
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
            citation=f"`memory_recall(\"{query}\") -> {name}`",
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


# ----------------------------------------------------------------------
# Codified RENDER step
# ----------------------------------------------------------------------
# THE DOMINANT LOSS CONDITION, measured 2026-09-05: premise_grounded refused
# 273 advances across 141 distinct tasks (cdb8e365 82 times, 4c9b39e5 66).
# Every refusal burned a whole `claude -p` drive and returned the task to the
# step it started on. The facts were in hand each time -- gather() above
# guarantees each citation satisfies arc_governance's grounding regexes, and
# the runner puts them in the model's prompt -- but the model was asked to
# RETYPE them into a `## Premises` section, and when it didn't, the step
# parked. Same shape as the red-test-ids stall: PRISM holds the fact, asks a
# model to restate it, refuses when it doesn't.
#
# This renders that section instead. It is a FLOOR, never a replacement for
# real analysis: it asserts only what was actually gathered, and it names an
# oracle clause nothing engages as a gap rather than padding it.

def render_premises(task, facts, oracle_word_min_len: int = 5,
                    oracle_min_shared_words: int = 2) -> str:
    """A `## Premises` section built from `facts` -- deterministic, no model.

    Every bullet carries a citation that came from a real GatheredFact, so
    the section passes premise_grounded's citation tooth by construction.
    Each clause of `task.oracle` that the gathered text genuinely engages is
    left to stand on that evidence; a clause nothing engages is marked
    ``clause N: UNRESOLVED, <why>`` -- the exact marker
    arc_governance.score_oracle_engagement names for a real gap.

    Returns "" when nothing was gathered: with no facts there is nothing
    honest to assert, and inventing claims would be worse than the stall.
    """
    facts = list(facts or [])
    if not facts:
        return ""

    from prism_service.services.arc_governance import (
        _clause_words, oracle_clauses,
    )

    lines = ["## Premises", ""]
    for f in facts:
        lines.append(f"- {f.text} ({f.citation})")

    oracle = str(getattr(task, "oracle", "") or "").strip()
    if oracle:
        # Measured against the SAME words the real tooth compares, so a
        # clause is called engaged here only when the tooth would agree.
        gathered_text = " ".join(f"{f.text} {f.citation}" for f in facts)
        have = _clause_words(gathered_text, oracle_word_min_len)
        gaps = []
        for idx, clause in enumerate(oracle_clauses(oracle), start=1):
            words = _clause_words(clause, oracle_word_min_len)
            if not words:
                continue
            needed = min(oracle_min_shared_words, len(words))
            if len(words & have) < needed:
                gaps.append((idx, clause))
        if gaps:
            lines.append("")
            for idx, clause in gaps:
                lines.append(
                    f"- clause {idx}: UNRESOLVED, no gathered fact engages "
                    f"this clause of the oracle (\"{clause[:80].strip()}\") "
                    "-- it needs evidence this step did not have")
    return "\n".join(lines) + "\n"


# ----------------------------------------------------------------------
# Codified SELECT step (task 6738006b)
# ----------------------------------------------------------------------
# THE REFUSAL THIS REPLACES. task_runner._codified_step_proof returned ""
# whenever the gather resolved more than _SHORTCUT_MAX_FACTS facts, and
# handed the step to the paid agentic judge. Its stated reason was right --
# "a wide set still needs SELECTING, and that is the one thing a formatter
# cannot do" -- but it refused on COUNT and never asked whether the render
# would pass. Measured 2026-09-08 over the 10 tasks blocked at
# review_previous_notes: 7 were refused by that cap, and 6 of the 7 render
# a section arc_governance.score_premise_grounded ACCEPTS, at zero tokens.
# Task 1bcb2b24 was one of them, and the judge it fell through to then
# failed 6 dispatches and parked the task 3 times.
#
# So SELECT, rather than raise the cap. Raising it would only delay the
# same refusal, and dropping it entirely is the 7.13.245 regression that
# made every retrieved fact a premise and turned a throughput fix into a
# noise generator. The bound stays; what changes is that the facts inside
# it are CHOSEN.

# The load-bearing few. Bounded on purpose: `gather` returns up to 15
# facts, and a Premises section that recites all of them is the noise
# generator above. Coverage-first ordering is what makes the bound safe --
# the facts that carry oracle engagement are kept FIRST, so the ones this
# drops are the ones that were adding text rather than evidence.
DEFAULT_KEEP_MAX = 5


@dataclass(frozen=True)
class Selection:
    """Which gathered facts to render, and which to leave out."""

    kept: list          # list[GatheredFact], in render order
    dropped: list       # list[GatheredFact], everything past the bound
    reason: str         # why this split, in one line


def _fact_words(fact, word_min_len: int) -> set:
    """The scoring vocabulary of one fact, tokenized by the SAME function
    the oracle tooth uses, so a word this counts is a word that tooth
    would count."""
    from prism_service.services.arc_governance import _clause_words

    return _clause_words(f"{fact.text} {fact.citation}", word_min_len)


def _topic_words(task, word_min_len: int) -> set:
    """The task's own subject vocabulary -- oracle, title and description.

    Relevance is measured against what the TICKET is about, never against
    the other facts, so a tight cluster of off-topic rows cannot vote
    itself load-bearing.
    """
    from prism_service.services.arc_governance import _clause_words

    blob = " ".join(str(getattr(task, name, "") or "")
                    for name in ("oracle", "title", "description"))
    return _clause_words(blob, word_min_len)


def select(task, facts, keep_max: int = DEFAULT_KEEP_MAX,
           word_min_len: int = 5, min_shared_words: int = 2) -> Selection:
    """Choose the load-bearing facts to render -- deterministic, no model.

    COVERAGE FIRST. A fact earns its slot by engaging an oracle clause no
    already-kept fact engages, measured with arc_governance's own
    `_clause_words`/`oracle_clauses` against the same shared-word threshold
    the real tooth applies. That is the ordering the recorded misfire asks
    for: dropping a fact must never cost a clause the citation that was
    engaging it, so the facts carrying engagement are taken before any
    fact is dropped.

    RELEVANCE SECOND. Once no remaining fact adds clause coverage, the
    leftover slots go to the facts sharing the most vocabulary with the
    ticket itself, ties broken by gather order so the result is stable.

    Returns every fact when there are no more than `keep_max`: selection is
    for the wide set, and a tight one is already the load-bearing few.
    """
    from prism_service.services.arc_governance import (
        _clause_words, oracle_clauses,
    )

    facts = list(facts or [])
    if len(facts) <= keep_max:
        return Selection(kept=facts, dropped=[],
                         reason=(f"{len(facts)} fact(s), at or under the "
                                 f"bound of {keep_max}: all kept"))

    topic = _topic_words(task, word_min_len)
    words = [_fact_words(f, word_min_len) for f in facts]
    relevance = [len(w & topic) for w in words]

    clauses = [_clause_words(c, word_min_len)
               for c in oracle_clauses(str(getattr(task, "oracle", "") or ""))]
    clauses = [c for c in clauses if c]

    remaining = set(range(len(facts)))
    chosen: list = []
    have: set = set()

    def _uncovered() -> list:
        return [c for c in clauses
                if len(c & have) < min(min_shared_words, len(c))]

    while len(chosen) < keep_max:
        gaps = _uncovered()
        if not gaps:
            break
        best, best_key = None, None
        for i in sorted(remaining):
            after = have | words[i]
            gained = sum(1 for c in gaps
                         if len(c & after) >= min(min_shared_words, len(c)))
            key = (gained, relevance[i], -i)
            if gained and (best_key is None or key > best_key):
                best, best_key = i, key
        if best is None:
            break
        chosen.append(best)
        remaining.discard(best)
        have |= words[best]

    covering = len(chosen)
    for i in sorted(remaining, key=lambda j: (-relevance[j], j)):
        if len(chosen) >= keep_max:
            break
        chosen.append(i)
        remaining.discard(i)

    keep_idx = sorted(chosen)
    return Selection(
        kept=[facts[i] for i in keep_idx],
        dropped=[facts[i] for i in sorted(remaining)],
        reason=(f"kept {len(keep_idx)} of {len(facts)} fact(s): {covering} "
                f"for oracle-clause coverage, {len(keep_idx) - covering} by "
                f"topical relevance; bound is {keep_max}"))
