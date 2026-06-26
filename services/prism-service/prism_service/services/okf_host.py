"""Project PRISM's curated MEMORY into a live, conformant OKF bundle (READ-ONLY).

Memory, OKF, and Understand are the SAME knowledge: OKF is the curated memory
expressed as a navigable wiki, and the brain/understand graph is the visual of
those same connections. So the bundle projects MEMORY ENTRIES ONLY — every
concept is a real ExpertiseEntry. Clicking a concept opens its real
/memory/<id> detail page; a [[wikilink]] rewrites to the target entry's
/memory/<id> route; and code references (evidence file_paths) link into the
/understand graph. No brain-file / fixture / empty junk concepts.

AUGMENTATION, NOT REPLACEMENT: this only READS the substrate. brain.db vectors,
graph.db, and the LSP tools are untouched.

A small content-signature cache avoids rebuilding the bundle on every request
(SPEC perf budget: never a full re-walk per read).
"""

from __future__ import annotations

import re

from prism_service.okf.bundle import Bundle
from prism_service.okf.concept import Concept
from prism_service.okf.links import wikilinks_to_md

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "untitled"


def _first_sentence(text: str, limit: int = 200) -> str:
    text = (text or "").strip().replace("\n", " ")
    head = text.split(". ", 1)[0]
    return (head[:limit] + "…") if len(head) > limit else head


def _memory_body(entry, evidence: dict) -> str:
    """Render an ExpertiseEntry as an OKF concept body (structured, not raw)."""
    parts: list[str] = []
    if entry.summary:
        parts.append(entry.summary)
    if entry.description:
        parts.append(entry.description)
    cites: list[str] = []
    for p in (evidence.get("file_paths") or []):
        # Code references navigate into the existing /understand graph (the
        # visual of the same connections) rather than fabricating /brain md.
        cites.append(f"- [`{p}`](/understand)")
    if evidence.get("commit"):
        cites.append(f"- commit `{evidence['commit']}`")
    if evidence.get("pr"):
        cites.append(f"- PR {evidence['pr']}")
    if cites:
        parts.append("# Citations\n" + "\n".join(cites))
    return "\n\n".join(parts) or "(no body)"


def _name_resolver(name_to_id: dict[str, str]):
    """Resolve a [[wikilink]] target name to its real /memory/<id> route.

    Match the raw name first, then a slugged form (authors mix dash/underscore
    spellings of the same entry name). Unknown names return None so the link
    stays literal `[[name]]` text — tolerant, never an error.
    """

    def resolve(name: str) -> str | None:
        target = name_to_id.get(name) or name_to_id.get(_slug(name))
        return f"/memory/{target}" if target else None

    return resolve


def _memory_concepts(memory_svc) -> tuple[list[Concept], dict[str, str]]:
    """Project active memory entries -> OKF concepts + a name->id link map."""
    rows: list[tuple[str, str, object]] = []
    name_to_id: dict[str, str] = {}
    for domain in memory_svc.list_domains():
        for e in memory_svc.list_entries(domain):  # active only by default
            path = f"/memory/{_slug(domain)}/{_slug(e.name or e.id)}.md"
            rows.append((path, domain, e))
            if e.name and e.id:
                # Index both the raw and slugged name so cross-spelling
                # wikilinks still resolve to the real /memory/<id> page.
                name_to_id[e.name] = e.id
                name_to_id.setdefault(_slug(e.name), e.id)
    resolve = _name_resolver(name_to_id)
    concepts: list[Concept] = []
    for path, domain, e in rows:
        evidence = e.evidence if isinstance(e.evidence, dict) else {}
        body = wikilinks_to_md(_memory_body(e, evidence), resolve)
        fm = {
            "id": e.id,  # the real ExpertiseEntry id -> /memory/<id> detail route
            "type": e.type or "note",
            "title": e.name or e.id,
            "description": e.summary or _first_sentence(e.description),
            "tags": [t for t in (domain, e.classification, e.memory_type) if t],
            "timestamp": e.recorded_at,
            "importance": e.importance,
        }
        if evidence.get("pr"):
            fm["resource"] = str(evidence["pr"])
        concepts.append(Concept(path=path, frontmatter=fm, body=body))
    return concepts, name_to_id


def build_bundle(memory_svc, brain_svc=None) -> Bundle:
    """Assemble the read-only projected OKF bundle — MEMORY ENTRIES ONLY.

    `brain_svc` is accepted (and ignored) for call-site compatibility: the brain
    graph is surfaced as code-reference links into /understand, never as its own
    junk concepts.
    """
    bundle = Bundle()
    mem_concepts, _ = _memory_concepts(memory_svc)
    for c in mem_concepts:
        bundle.add(c)
    return bundle


class OkfHost:
    """Per-project read-only OKF projection with a content-signature cache.

    The cache avoids rebuilding on every request; it invalidates when the cheap
    memory signature (per-domain active entry counts) changes, so writes
    elsewhere refresh the view without a full re-walk on the hot path.
    """

    def __init__(self, memory_svc, brain_svc=None):
        self._memory_svc = memory_svc
        self._brain_svc = brain_svc  # kept for API compat; not projected
        self._sig: tuple | None = None
        self._bundle: Bundle | None = None

    def _signature(self) -> tuple:
        try:
            domains = sorted(self._memory_svc.list_domains())
            return tuple(
                (d, len(self._memory_svc.list_entries(d))) for d in domains
            )
        except Exception:
            return ()

    def bundle(self) -> Bundle:
        sig = self._signature()
        if self._bundle is None or sig != self._sig:
            self._bundle = build_bundle(self._memory_svc, self._brain_svc)
            self._sig = sig
        return self._bundle

    def index(self) -> dict:
        b = self.bundle()
        # Compact per-concept manifest so the /okf UI can render type-colored
        # frontmatter cards without one request per concept; body stays lazy
        # (fetched via .get on expand, progressive disclosure).
        concepts = []
        for path in b.paths():
            c = b.concepts[path]
            concepts.append({
                "path": path,
                # The real ExpertiseEntry id -> the UI opens /memory/<id>.
                "id": str(c.frontmatter.get("id", "") or ""),
                "section": path.strip("/").split("/")[0],
                "type": c.type,
                "title": c.title or path.rsplit("/", 1)[-1],
                "description": str(c.frontmatter.get("description", "") or ""),
            })
        return {
            "okf_version": b.okf_version,
            "sections": b.sections(),
            "concept_count": len(b.concepts),
            "index_md": b.render_index_md(),
            "paths": b.paths(),
            "concepts": concepts,
        }

    def get(self, path: str) -> dict | None:
        if not path.startswith("/"):
            path = "/" + path
        c = self.bundle().get(path)
        if c is None:
            return None
        from prism_service.okf.links import extract_links

        return {
            "path": c.path,
            "type": c.type,
            "frontmatter": c.frontmatter,
            "body": c.body,
            "links": extract_links(c.body),
        }

    def raw(self, path: str) -> str | None:
        if not path.startswith("/"):
            path = "/" + path
        return self.bundle().files().get(path)
