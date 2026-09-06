"""What EVERY subsystem knows about one entity, in one answer.

PRISM stores what it knows in four places, and until now a person looking at
a thing in Explore saw only what the mesh happened to carry: a degree, a
timestamp, and an ontology class that usually read "unclassified". The rest
was reachable, but only by knowing which page to open next.

A dossier gathers all four and SAYS WHERE EACH PART CAME FROM:

  * CODE GRAPH (graph.db)  - the file it lives in, the community it belongs
    to, how central it is, and how many relationships touch it.
  * SYMBOLS (brain.db, tree-sitter)  - its definition, what calls it, what it
    calls. The LSP-shaped view of the same entity.
  * BRAIN (brain.db docs + curated memory)  - the documents and concepts that
    mention it, which is how a reader learns WHY it exists.
  * ONTOLOGY (the RDF store)  - the class it is an instance of, and the rules
    that constrain that class.

EVERY SECTION REPORTS ITSELF. Each carries `source` (the store it read) and,
when it could not answer, `reason`. A subsystem that is empty, unbuilt, or
locked is therefore VISIBLE as such rather than rendering as an absence the
reader must interpret — the whole point being that a person can see which
part of the system knows what.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How many rows any one section returns. A dossier is read at a glance beside
# a graph, not scrolled.
_LIMIT = 8

# Domains that name CODE rather than writing. brain.search labels a row with
# either the generic "code" or the language ("py", "ts"), so both forms have
# to be named here — filtering only "code" let every "py" row through.
_CODE_DOMAINS = frozenset({
    "code", "py", "ts", "tsx", "js", "jsx", "go", "rs", "java", "cs", "sql",
})


def _section(source: str, **payload: Any) -> dict:
    return {"ok": True, "source": source, "reason": "", **payload}


def _unavailable(source: str, reason: str) -> dict:
    """A section that could not answer says so. Never an empty success."""
    return {"ok": False, "source": source, "reason": reason[:200]}


def _code_graph(graph_svc, file: str) -> dict:
    """graph.db: where the entity sits and what it is joined to."""
    source = "graph.db"
    if graph_svc is None:
        return _unavailable(source, "this project has no code graph")
    if not file:
        return _unavailable(source, "the entity resolved to no source file")
    try:
        detail = graph_svc.file_detail(file) or {}
    except Exception as exc:  # noqa: BLE001
        return _unavailable(source, f"file_detail failed: {exc}")

    entities = detail.get("entities") or []
    inbound = detail.get("in_edges") or detail.get("inbound") or []
    outbound = detail.get("out_edges") or detail.get("outbound") or []
    community = None
    try:
        communities = graph_svc.file_communities([file]) or {}
        community = communities.get(file)
    except Exception:  # noqa: BLE001 - a missing community is not a failure
        community = None

    return _section(
        source,
        file=file,
        community=community,
        entities=len(entities),
        inbound=len(inbound),
        outbound=len(outbound),
        neighbours=[
            {"file": str(e.get("from") or e.get("to") or ""),
             "weight": int(e.get("weight") or 1)}
            for e in (list(inbound) + list(outbound))[:_LIMIT]
        ],
    )


def _symbols(brain_svc, name: str) -> dict:
    """brain.db's tree-sitter index: the LSP-shaped reading of the entity."""
    source = "brain.db symbols (tree-sitter)"
    if brain_svc is None or not name:
        return _unavailable(source, "no symbol to look up")
    try:
        definitions = brain_svc.find_symbol(name, limit=3) or []
    except Exception as exc:  # noqa: BLE001
        return _unavailable(source, f"find_symbol failed: {exc}")

    def _chain(direction: str) -> list[dict]:
        """The OTHER party in each call, plus where the call is written.

        A row is {from, to, relation, confidence, call_site_file,
        call_site_location}: for callers the counterpart is `from`, for
        callees it is `to`. The call site travels with it because a reader
        checking a claim wants the line, and the confidence travels with it
        because these edges are INFERRED, not proven.
        """
        try:
            rows = brain_svc.call_chain(
                name, depth=1, limit=_LIMIT, direction=direction) or []
        except Exception:  # noqa: BLE001 - one direction failing is not fatal
            return []
        other = "from" if direction == "callers" else "to"
        out = []
        for r in rows[:_LIMIT]:
            counterpart = str(r.get(other) or "")
            if not counterpart:
                continue
            out.append({
                "name": counterpart,
                "file": str(r.get("call_site_file") or ""),
                "line": str(r.get("call_site_location") or ""),
                "confidence": str(r.get("confidence") or ""),
            })
        return out

    return _section(
        source,
        definitions=[
            {"name": str(d.get("entity_name") or name),
             "file": str(d.get("source_file") or ""),
             "kind": str(d.get("entity_kind") or "")}
            for d in definitions
        ],
        callers=_chain("callers"),
        callees=_chain("callees"),
    )


def _brain(brain_svc, token: str) -> dict:
    """What has been WRITTEN about the entity — prose, not more code.

    A plain search returns mostly `domain="code"` rows, which restated the
    symbol index sitting directly above this section: a reader saw the same
    function names twice and learned nothing new. The SYMBOLS section owns
    code. This one keeps the documents and curated memory that say WHY the
    thing exists, and reports honestly when there are none.
    """
    source = "brain.db documents and memory"
    if brain_svc is None or not token:
        return _unavailable(source, "no token to search for")
    try:
        hits = brain_svc.search(token, limit=_LIMIT * 4) or []
    except Exception as exc:  # noqa: BLE001
        return _unavailable(source, f"search failed: {exc}")

    mentions: list[dict] = []
    seen: set[str] = set()
    for h in hits:
        # A DOCUMENT OR MEMORY HAS A TITLE; A CODE ENTITY DOES NOT. Live rows
        # show `title: null` with an `entity_kind` for code, so falling back
        # to entity_name was what smuggled function names in here. The domain
        # is a second filter, not the only one: it is the language ("py",
        # "ts") as often as it is "code".
        title = str(h.get("title") or "").strip()
        if not title or h.get("entity_kind"):
            continue
        domain = str(h.get("domain") or "")
        if domain in _CODE_DOMAINS:
            continue  # the SYMBOLS section already says this, and better
        path = str(h.get("source_file") or h.get("path") or "")
        if title in seen:
            continue
        seen.add(title)
        mentions.append({"title": title, "path": path, "domain": domain})
        if len(mentions) >= _LIMIT:
            break

    return _section(source, mentions=mentions,
                    searched=len(hits), prose_only=True)


def _ontology(project: str, ontology_class: str) -> dict:
    """The RDF store: the class this is an instance of, and its rules."""
    source = "ontology store"
    if not ontology_class:
        return _unavailable(
            source, "this entity carries no ontology class yet")
    try:
        from prism_service.services.ontology_graph import OntologyGraph
        graph = OntologyGraph(project)
        classes = graph.classes() or []
        properties = graph.properties() or []
    except Exception as exc:  # noqa: BLE001 - single-writer store may be busy
        return _unavailable(source, f"the ontology could not be read: {exc}")

    match = next(
        (c for c in classes
         if str(c.get("id")) == ontology_class
         or str(c.get("name")) == ontology_class), None)

    rules: list[dict] = []
    try:
        from prism_service.services import rule_decisions
        for r in rule_decisions.decorated_report(project).get("rules", []):
            text = f"{r.get('title', '')} {r.get('description', '')}"
            if ontology_class.lower() in text.lower():
                rules.append({"name": str(r.get("name") or ""),
                              "title": str(r.get("title") or ""),
                              "violations": int(r.get("violations") or 0)})
    except Exception:  # noqa: BLE001 - rules are enrichment, never required
        rules = []

    return _section(
        source,
        klass=ontology_class,
        described=bool(match),
        instances=int((match or {}).get("instance_count") or 0),
        description=str((match or {}).get("description") or ""),
        relations=[
            {"name": str(p.get("name") or ""),
             "to": str(p.get("range_class") or "")}
            for p in properties
            if str(p.get("domain_class") or "") == ontology_class
        ][:_LIMIT],
        rules=rules[:_LIMIT],
    )


def dossier(
    token: str,
    project: str,
    *,
    memory_svc=None,
    brain_svc=None,
    graph_svc=None,
    task_svc=None,
    conductor_svc=None,
) -> dict:
    """Every subsystem's reading of `token`, each labelled with its source."""
    from prism_service.api.xref import resolve_token

    try:
        resolved = resolve_token(
            token, memory_svc, brain_svc, graph_svc=graph_svc,
            task_svc=task_svc, conductor_svc=conductor_svc)
    except Exception as exc:  # noqa: BLE001
        resolved = {"kind": "unresolved", "label": token, "href": None,
                    "error": str(exc)[:200]}

    kind = str(resolved.get("kind") or "unresolved")
    label = str(resolved.get("label") or token)

    # A symbol resolves to a file through the same ladder the mesh uses.
    file = ""
    name = label
    if kind == "symbol":
        href = str(resolved.get("href") or "")
        if "focus=" in href:
            file = href.split("focus=", 1)[1].split("&", 1)[0]
    elif kind == "file":
        file = label

    ontology_class = str(resolved.get("ontology_class") or "")
    if not ontology_class and kind == "symbol":
        # The mesh derives the class from the symbol's entity_kind; reuse the
        # same mapping rather than inventing a second one.
        try:
            from prism_service.api.xref import _resolve_symbol
            sym = _resolve_symbol(brain_svc, token) or {}
            ontology_class = str(sym.get("entity_kind") or "").capitalize()
        except Exception:  # noqa: BLE001
            ontology_class = ""

    return {
        "token": token,
        "kind": kind,
        "label": label,
        "href": resolved.get("href"),
        "code_graph": _code_graph(graph_svc, file),
        "symbols": _symbols(brain_svc, name if kind == "symbol" else ""),
        "brain": _brain(brain_svc, token),
        "ontology": _ontology(project, ontology_class),
    }
