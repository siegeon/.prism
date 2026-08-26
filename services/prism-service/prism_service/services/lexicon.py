"""lexicon — the canonical terms a task write aligns on (task 2ee65e14,
epic df0eed4a, owner decision 2026-08-26: "the act is called ALIGN, not
converge — ontology alignment").

``load_lexicon`` parses ontology/model-lexicon.ttl into a small,
process-cached list of ``Term`` rows. ``synonyms`` flattens that into a
lowercase-synonym -> canonical-label lookup. ``align`` rewrites free
text: every whole-word synonym (singular or a plain plural) becomes its
canonical label, case-insensitively, everywhere EXCEPT the protected
spans services.ste already knows not to touch (code, URLs, quotes, ids,
file paths) — this module imports and reuses ste's own span finder
rather than re-inventing span protection.

This is a separate, narrower pass than ste.normalize: normalize fixes
STYLE (contractions, filler, marketing words); align fixes VOCABULARY
(a synonym becomes the one word the rest of the project already uses
for that concept). services/ste.py's apply() runs both.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from pathlib import Path

import rdflib

from prism_service.services import ste

_ONTOLOGY_DIR = Path(__file__).resolve().parent.parent / "ontology"
_LEXICON_TTL = _ONTOLOGY_DIR / "model-lexicon.ttl"

_NS = "urn:prism:onto:"
_O = rdflib.Namespace(_NS)
_RDF = rdflib.RDF
_RDFS = rdflib.RDFS


@dataclass(frozen=True)
class Term:
    """One canonical term: its preferred label, its one-sentence
    definition, the synonyms it replaces, and the local name of the
    class it names (empty when the term names no class in the model)."""

    label: str
    definition: str
    alt_labels: tuple[str, ...]
    denotes: str


@functools.lru_cache(maxsize=1)
def load_lexicon() -> list[Term]:
    """Parse ontology/model-lexicon.ttl into the canonical Term list,
    sorted by label. Cached per process — the file changes only with a
    deploy, never at runtime."""
    g = rdflib.Graph()
    g.parse(str(_LEXICON_TTL), format="turtle")

    terms: list[Term] = []
    for subj in g.subjects(_RDF.type, _O.Term):
        label = str(g.value(subj, _RDFS.label) or "")
        definition = str(g.value(subj, _RDFS.comment) or "")
        alt_labels = tuple(sorted(str(o) for o in g.objects(subj, _O.altLabel)))
        denotes_obj = g.value(subj, _O.denotes)
        denotes = ""
        if denotes_obj is not None:
            denotes = str(denotes_obj)
            if denotes.startswith(_NS):
                denotes = denotes[len(_NS):]
        terms.append(Term(label=label, definition=definition,
                           alt_labels=alt_labels, denotes=denotes))
    terms.sort(key=lambda t: t.label)
    return terms


def synonyms() -> dict[str, str]:
    """{alt.lower(): label} across the whole lexicon."""
    out: dict[str, str] = {}
    for term in load_lexicon():
        for alt in term.alt_labels:
            out[alt.lower()] = term.label
    return out


def _pluralize(phrase: str) -> str:
    """A plain-English plural of the LAST word in `phrase` — the same
    simple rule for a synonym ("ticket" -> "tickets", "story" ->
    "stories") and for the canonical label it aligns to ("Task" ->
    "Tasks", "PullRequest" -> "PullRequests")."""
    words = phrase.split(" ")
    last = words[-1]
    if re.search(r"(?:[sxz]|ch|sh)$", last, re.IGNORECASE):
        plural_last = last + "es"
    elif re.search(r"[^aeiouAEIOU]y$", last):
        plural_last = last[:-1] + "ies"
    else:
        plural_last = last + "s"
    return " ".join(words[:-1] + [plural_last])


@functools.lru_cache(maxsize=1)
def _candidates() -> tuple[tuple[str, str], ...]:
    """(surface form, canonical label) for every synonym AND its plural,
    longest surface form first so a multi-word synonym (and a longer
    plural) wins over a shorter one at the same text position. A
    surface form that collides with an earlier, longer one keeps the
    first (longest) mapping."""
    raw: list[tuple[str, str]] = []
    for term in load_lexicon():
        for alt in term.alt_labels:
            raw.append((alt, term.label))
            raw.append((_pluralize(alt), _pluralize(term.label)))

    raw.sort(key=lambda c: len(c[0]), reverse=True)

    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for surface, label in raw:
        key = surface.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append((surface, label))
    return tuple(ordered)


@functools.lru_cache(maxsize=1)
def _pattern_and_lookup() -> tuple[re.Pattern, dict[str, str]]:
    ordered = _candidates()
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(surface) for surface, _ in ordered) + r")\b",
        re.IGNORECASE,
    )
    lookup = {surface.lower(): label for surface, label in ordered}
    return pattern, lookup


def align(text: str) -> tuple[str, list[dict]]:
    """Replace every whole-word synonym (singular or plain plural) with
    its canonical label, case-insensitively, everywhere except a
    protected span (ste._protected_spans — code, a URL, a file path, a
    quoted string, a task/memory id). Returns the aligned text and the
    replacements applied, in order: [{"from": matched_text, "to":
    label}, ...]."""
    if not text:
        return text, []
    if not load_lexicon():
        return text, []

    pattern, lookup = _pattern_and_lookup()

    protected = ste._protected_spans(text)
    segments = ste._segments(text, protected)

    applied: list[dict] = []
    out_parts: list[str] = []
    for is_protected, chunk in segments:
        if is_protected:
            out_parts.append(chunk)
            continue

        def _repl(m: re.Match, applied=applied, lookup=lookup) -> str:
            matched = m.group(0)
            label = lookup[matched.lower()]
            applied.append({"from": matched, "to": label})
            return label

        out_parts.append(pattern.sub(_repl, chunk))

    return "".join(out_parts), applied
