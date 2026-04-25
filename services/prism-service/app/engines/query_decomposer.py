"""Rules-based query decomposer for Brain candidate generation.

Splits compound questions into atomic sub-queries so each can be
embedded/searched independently and the per-index hits unioned before
RRF. v1 is pure-Python rules — no LLM hop, no external dependency.

[Source: docs/stories/PLAT-0042-retrieval-query-decomposition.story.md]
[Used by: app.engines.brain_engine.Brain.search when PRISM_QUERY_DECOMP=on]
"""

from __future__ import annotations

import re

# Connectives that mark a sub-query boundary. Order matters only for
# stable splits; we collapse them into one regex below.
_CONNECTIVES = (r"\s+and\s+", r"\s+then\s+", r";")
_CONNECTIVE_RE = re.compile("|".join(_CONNECTIVES), re.IGNORECASE)

# Filler that adds no retrieval signal once a fragment is isolated.
_FILLERS = (
    "what was", "what is", "what were", "what are",
    "tell me about", "tell me", "do you know",
    "can you find", "can you",
    "how did", "how do", "how does",
)
_FILLER_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(f) for f in _FILLERS) + r")\s+",
    re.IGNORECASE,
)

_TRIVIAL_TOKEN_CAP = 6
_DECOMP_TOKEN_TRIGGER = 12
_MIN_SUB_TOKENS = 2


def _has_connective(q: str) -> bool:
    return bool(_CONNECTIVE_RE.search(q))


def _strip_filler(s: str) -> str:
    out = s
    # Apply repeatedly so layered fillers ("tell me what was X") collapse.
    for _ in range(3):
        new = _FILLER_RE.sub("", out, count=1)
        if new == out:
            break
        out = new
    return out.strip()


def decompose_query(q: str, max_subs: int = 4) -> list[str]:
    """Return ≥1 sub-queries for ``q``; the raw ``q`` is always preserved.

    Trivial queries (≤6 tokens AND no connective) return ``[q]`` only,
    so the caller's existing single-query path runs unchanged on the
    common case.

    Args:
        q: Raw user query.
        max_subs: Cap on output length (raw + sub-queries combined).

    Returns:
        Ordered list with ``q`` first, followed by deduped sub-queries.
    """
    if not q:
        return [""]

    tokens = q.split()
    has_conn = _has_connective(q)
    long_query = len(tokens) > _DECOMP_TOKEN_TRIGGER

    # AC-1 trigger: trivial inputs return [q] only.
    if not has_conn and not long_query and len(tokens) <= _TRIVIAL_TOKEN_CAP:
        return [q]

    # Split on connectives. If none, fall back to a midpoint split for
    # long queries so the trigger above still produces ≥2 sub-queries.
    if has_conn:
        parts = [p.strip() for p in _CONNECTIVE_RE.split(q) if p and p.strip()]
    elif long_query:
        mid = len(tokens) // 2
        parts = [" ".join(tokens[:mid]), " ".join(tokens[mid:])]
    else:
        parts = [q]

    cleaned: list[str] = []
    for p in parts:
        s = _strip_filler(p)
        if len(s.split()) >= _MIN_SUB_TOKENS:
            cleaned.append(s)

    # Always include the raw query so downstream still gets the
    # user's original phrasing in the candidate pool.
    out: list[str] = [q]
    seen_lower = {q.lower()}
    for s in cleaned:
        key = s.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        out.append(s)
        if len(out) >= max_subs:
            break

    return out
