"""Project PRISM's stores into a live, conformant OKF bundle (READ-ONLY).

Builds an in-memory Bundle from the existing memory (JSONL ExpertiseEntry) and
brain (docs) stores so the daemon can serve PRISM's knowledge as an OKF wiki
over MCP / HTTP / the /okf UI. This is the "host, read path" slice.

AUGMENTATION, NOT REPLACEMENT: this only READS the substrate. brain.db vectors,
graph.db, and the LSP tools are untouched; okf_search (later slice) routes
through the existing hybrid retrieval rather than reimplementing it.

A small content-signature cache avoids rebuilding the bundle on every request
(SPEC perf budget: never a full re-walk per read).
"""

from __future__ import annotations

import re

from prism_service.okf.bundle import Bundle
from prism_service.okf.concept import Concept
from prism_service.okf.links import wikilinks_to_md

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_BRAIN_CAP = 400  # bound the brain projection for perf on large indexes


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
        cites.append(f"- `{p}`")
    if evidence.get("commit"):
        cites.append(f"- commit `{evidence['commit']}`")
    if evidence.get("pr"):
        cites.append(f"- PR {evidence['pr']}")
    if cites:
        parts.append("# Citations\n" + "\n".join(cites))
    return "\n\n".join(parts) or "(no body)"


def _memory_concepts(memory_svc) -> tuple[list[Concept], dict[str, str]]:
    """Project active memory entries -> OKF concepts + a name->path link map."""
    rows: list[tuple[str, str, object]] = []
    name_to_path: dict[str, str] = {}
    for domain in memory_svc.list_domains():
        for e in memory_svc.list_entries(domain):  # active only by default
            path = f"/memory/{_slug(domain)}/{_slug(e.name or e.id)}.md"
            rows.append((path, domain, e))
            if e.name:
                name_to_path[e.name] = path
    concepts: list[Concept] = []
    for path, domain, e in rows:
        evidence = e.evidence if isinstance(e.evidence, dict) else {}
        body = wikilinks_to_md(_memory_body(e, evidence), name_to_path.get)
        fm = {
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
    return concepts, name_to_path


def _brain_concepts(brain_svc, cap: int = _BRAIN_CAP) -> list[Concept]:
    """Project indexed brain docs -> one OKF concept per source file (bounded).

    doc_id is "<source_file>::<entity>"; we group entities under their file so
    the projection is a navigable file listing, not 1000s of chunk fragments.
    """
    try:
        docs = brain_svc.list_docs(limit=cap)
    except Exception:
        return []
    by_file: dict[str, dict] = {}
    for d in docs:
        doc_id = str(d.get("doc_id", ""))
        src = doc_id.split("::", 1)[0] or doc_id
        ent = by_file.setdefault(src, {"domain": d.get("domain") or "code", "entities": []})
        tail = doc_id.split("::", 1)[1] if "::" in doc_id else ""
        if tail and tail != "main":
            ent["entities"].append(tail)
    concepts: list[Concept] = []
    for src, info in by_file.items():
        ents = info["entities"]
        schema = "\n".join(f"- `{e}`" for e in ents[:50]) if ents else "(file-level chunk)"
        body = f"Indexed source file. Entities:\n\n# Schema\n{schema}"
        fm = {
            "type": info["domain"],
            "title": src.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            "description": f"{len(ents)} indexed entit{'y' if len(ents) == 1 else 'ies'} from {src}",
            "resource": src,
            "tags": ["brain", info["domain"]],
        }
        concepts.append(Concept(path=f"/brain/{_slug(src)}.md", frontmatter=fm, body=body))
    return concepts


def build_bundle(memory_svc, brain_svc) -> Bundle:
    """Assemble the read-only projected OKF bundle from the project's stores."""
    bundle = Bundle()
    mem_concepts, _ = _memory_concepts(memory_svc)
    for c in mem_concepts:
        bundle.add(c)
    for c in _brain_concepts(brain_svc):
        bundle.add(c)
    return bundle


class OkfHost:
    """Per-project read-only OKF projection with a content-signature cache.

    The cache avoids rebuilding on every request; it invalidates when the
    cheap signature (memory domain count + brain doc count) changes, so writes
    elsewhere refresh the view without a full re-walk on the hot path.
    """

    def __init__(self, memory_svc, brain_svc):
        self._memory_svc = memory_svc
        self._brain_svc = brain_svc
        self._sig: tuple | None = None
        self._bundle: Bundle | None = None

    def _signature(self) -> tuple:
        try:
            domains = tuple(sorted(self._memory_svc.list_domains()))
        except Exception:
            domains = ()
        try:
            ndocs = len(self._brain_svc.list_docs(limit=_BRAIN_CAP))
        except Exception:
            ndocs = 0
        return (domains, ndocs)

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
