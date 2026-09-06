"""What EVERY subsystem knows about one entity, in one answer.

PRISM stores what it knows in several places, and a person looking at a thing
in Explore saw only what the mesh carried: a degree, a timestamp, and an
ontology class that usually read "unclassified". The rest was reachable, but
only by knowing which page to open next.

TWO RULES SHAPE THIS FILE.

1. EVERY SECTION REPORTS ITS SOURCE. Each carries `source` (the store it
   read) and, when it could not answer, `reason`. A subsystem that is empty,
   unbuilt or locked is VISIBLE as such rather than rendering as an absence
   the reader must interpret.

2. THE SECTIONS DEPEND ON WHAT THE THING IS. An earlier cut asked only the
   code questions — file, symbol, callers — so selecting a TASK, which is
   what this page opens on, produced four empty sections in a row: "resolved
   to no source file", "no symbol to look up", "nothing written about this
   yet", "carries no ontology class yet". A task has contents; they were
   simply never asked for. A task is now described by its own facts, the
   knowledge it pulled in, and the work under it; code is described by the
   code graph and the symbol index. Sections are therefore a LIST, not fixed
   keys, so a new kind describes itself without pretending to be code.
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

# What the mesh's resolved kind IS, in the ontology's own words. Without this
# a task reported "carries no ontology class yet" while the ontology held 924
# instances of Task.
_KIND_TO_CLASS = {
    "task": "Task",
    "concept": "Concept",
    "session": "Session",
    "gate": "Gate",
    "file": "Document",
    "test": "Test",
}


def _row(label: str, text: str, href: str = "") -> dict:
    return {"label": label, "text": text, "href": href}


def _section(key: str, title: str, source: str, rows: list[dict]) -> dict:
    return {"key": key, "title": title, "source": source,
            "ok": True, "reason": "", "rows": rows}


def _unavailable(key: str, title: str, source: str, reason: str) -> dict:
    """A section that could not answer says so. Never an empty success."""
    return {"key": key, "title": title, "source": source,
            "ok": False, "reason": reason[:200], "rows": []}


# ─────────────────────────────────────────────────────────── code readings

def _code_graph(graph_svc, file: str) -> dict:
    key, title, source = "code_graph", "Code graph", "graph.db"
    if graph_svc is None:
        return _unavailable(key, title, source, "this project has no code graph")
    if not file:
        return _unavailable(key, title, source, "this entity has no source file")
    try:
        detail = graph_svc.file_detail(file) or {}
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, title, source, f"file_detail failed: {exc}")

    entities = detail.get("entities") or []
    inbound = detail.get("in_edges") or detail.get("inbound") or []
    outbound = detail.get("out_edges") or detail.get("outbound") or []
    community: Optional[Any] = None
    try:
        community = (graph_svc.file_communities([file]) or {}).get(file)
    except Exception:  # noqa: BLE001 - a missing community is not a failure
        community = None

    return _section(key, title, source, [
        _row("File", file, f"/artifact?focus={file}"),
        _row("Community", "—" if community is None else str(community)),
        _row("Edges", f"{len(inbound)} in · {len(outbound)} out · "
                      f"{len(entities)} in file"),
    ])


def _symbols(brain_svc, name: str) -> dict:
    key, title = "symbols", "Symbols"
    source = "brain.db symbols (tree-sitter)"
    if brain_svc is None or not name:
        return _unavailable(key, title, source, "this entity is not a code symbol")
    try:
        definitions = brain_svc.find_symbol(name, limit=3) or []
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, title, source, f"find_symbol failed: {exc}")

    def _chain(direction: str) -> list[dict]:
        """The OTHER party in each call, plus where the call is written.

        A row is {from, to, call_site_file, call_site_location, confidence}:
        for callers the counterpart is `from`, for callees it is `to`. The
        call site travels with it because a reader checking a claim wants the
        line, and the confidence because these edges are INFERRED.
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
            site = str(r.get("call_site_file") or "")
            out.append(_row(
                "", f"{counterpart}  {r.get('call_site_location') or ''}".strip(),
                f"/artifact?focus={site}" if site else ""))
        return out

    rows = [
        _row("Defined", f"{d.get('entity_name') or name} · {d.get('entity_kind') or ''}",
             f"/artifact?focus={d.get('source_file') or ''}")
        for d in definitions
    ]
    callers, callees = _chain("callers"), _chain("callees")
    if callers:
        rows.append(_row("Called by", "", ""))
        rows.extend(callers)
    if callees:
        rows.append(_row("Calls", "", ""))
        rows.extend(callees[:5])
    return _section(key, title, source, rows)


# ────────────────────────────────────────────────────────── task readings

def _task_facts(task_svc, task_id: str) -> dict:
    """The task's own row: where it is in the workflow and what proves it."""
    key, title, source = "task", "Task", "tasks.db"
    if task_svc is None or not task_id:
        return _unavailable(key, title, source, "no task to read")
    try:
        t = task_svc.get(task_id)
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, title, source, f"lookup failed: {exc}")
    if not t:
        return _unavailable(key, title, source, "no task with this id")

    def f(name: str) -> str:
        return str(getattr(t, name, "") or "")

    rows = [
        _row("Status", f("status")),
        _row("Step", f("workflow_step") or "—"),
        _row("Gate", f("gate_state") or "none"),
        _row("Workflow", f("workflow") or "—"),
    ]
    if f("proof_type"):
        rows.append(_row("Proof", f("proof_type")))
    if f("parent_id"):
        rows.append(_row("Parent", f("parent_id")[:8], f"/tasks/{f('parent_id')}"))
    if f("oracle"):
        rows.append(_row("Oracle", f("oracle")[:160]))
    return _section(key, title, source, rows)


def _task_knowledge(memory_svc, brain_svc, task_id: str) -> dict:
    """WHAT PRISM PULLED IN to build this task's context — the concepts it
    recalled. This is the reason a person opens a task in the graph."""
    key, title = "knowledge", "Knowledge it pulled in"
    source = "curated memory (recall log)"
    if memory_svc is None or not task_id:
        return _unavailable(key, title, source, "no task to read")
    try:
        from prism_service.services.okf_host import OkfHost
        concepts = OkfHost(memory_svc, brain_svc).task_concepts(task_id) or []
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, title, source, f"recall lookup failed: {exc}")
    if not concepts:
        return _section(key, title, source,
                        [_row("", "this task recalled no concepts")])
    return _section(key, title, source, [
        _row(str(c.get("type") or ""), str(c.get("title") or c.get("id") or ""),
             f"/understand?concept={c.get('id')}")
        for c in concepts[:_LIMIT]
    ])


def _task_work(task_svc, task_id: str) -> dict:
    """The work under it: the children a drive split it into."""
    key, title, source = "work", "Work under it", "tasks.db"
    if task_svc is None or not task_id:
        return _unavailable(key, title, source, "no task to read")
    try:
        children = task_svc.list(parent_id=task_id) or []
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, title, source, f"child lookup failed: {exc}")
    if not children:
        return _section(key, title, source, [_row("", "no child tasks")])
    rows = []
    for c in children[:_LIMIT]:
        cid = str(getattr(c, "id", "") or (c.get("id") if isinstance(c, dict) else ""))
        ttl = str(getattr(c, "title", "") or (c.get("title") if isinstance(c, dict) else ""))
        st = str(getattr(c, "status", "") or (c.get("status") if isinstance(c, dict) else ""))
        rows.append(_row(st, ttl or cid[:8], f"/tasks/{cid}" if cid else ""))
    return _section(key, title, source, rows)


# ─────────────────────────────────────────────────────── shared readings

def _brain(brain_svc, token: str) -> dict:
    """What has been WRITTEN about the entity — prose, not more code.

    A plain search returns mostly code rows, which restated the symbol index
    sitting directly above: the reader saw the same names twice and learned
    nothing. A document or memory has a title; a code entity has title=None
    and an entity_kind. The domain is a second filter, not the only one — it
    is the language ("py") as often as the generic "code".
    """
    key, title = "brain", "Written about it"
    source = "brain.db documents and memory"
    if brain_svc is None or not token:
        return _unavailable(key, title, source, "no token to search for")
    try:
        hits = brain_svc.search(token, limit=_LIMIT * 4) or []
    except Exception as exc:  # noqa: BLE001
        return _unavailable(key, title, source, f"search failed: {exc}")

    rows, seen = [], set()
    for h in hits:
        name = str(h.get("title") or "").strip()
        if not name or h.get("entity_kind") or name in seen:
            continue
        if str(h.get("domain") or "") in _CODE_DOMAINS:
            continue
        seen.add(name)
        path = str(h.get("source_file") or h.get("path") or "")
        rows.append(_row(str(h.get("domain") or ""), name,
                         f"/artifact?focus={path}" if path else ""))
        if len(rows) >= _LIMIT:
            break
    if not rows:
        return _section(key, title, source,
                        [_row("", f"nothing written about this "
                                  f"(searched {len(hits)} index rows)")])
    return _section(key, title, source, rows)


def _ontology(project: str, ontology_class: str) -> dict:
    """The class this is an instance of, and the rules that constrain it."""
    key, title, source = "ontology", "Ontology", "ontology store"
    if not ontology_class:
        return _unavailable(key, title, source,
                            "this entity carries no ontology class yet")
    try:
        from prism_service.services.ontology_graph import OntologyGraph
        graph = OntologyGraph(project)
        classes = graph.classes() or []
        properties = graph.properties() or []
    except Exception as exc:  # noqa: BLE001 - single-writer store may be busy
        return _unavailable(key, title, source,
                            f"the ontology could not be read: {exc}")

    match = next((c for c in classes
                  if str(c.get("id")) == ontology_class
                  or str(c.get("name")) == ontology_class), None)
    rows = [_row("Class",
                 f"{ontology_class} · {int((match or {}).get('instance_count') or 0)} instances",
                 f"/ontology?tab=structure&class={ontology_class}")]

    relations = [f"{p.get('name')} → {p.get('range_class')}"
                 for p in properties
                 if str(p.get("domain_class") or "") == ontology_class][:_LIMIT]
    if relations:
        rows.append(_row("Relates", "", ""))
        rows.extend(_row("", r) for r in relations)

    try:
        from prism_service.services import rule_decisions
        for r in rule_decisions.decorated_report(project).get("rules", [])[:40]:
            text = f"{r.get('title', '')} {r.get('description', '')}"
            if ontology_class.lower() not in text.lower():
                continue
            violations = int(r.get("violations") or 0)
            rows.append(_row(
                "Rule",
                f"{r.get('title') or r.get('name')}"
                + (f" · {violations} failing" if violations else ""),
                "/ontology?tab=rules"))
    except Exception:  # noqa: BLE001 - rules are enrichment, never required
        pass

    return _section(key, title, source, rows)


# ───────────────────────────────────────────────────────────── the dossier

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
    """Every subsystem's reading of `token`, each labelled with its source.

    Which readings apply depends on WHAT THE THING IS: a task is described by
    its own row, the knowledge it pulled in and the work under it; code by the
    code graph and the symbol index. Everything gets the ontology.
    """
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
    href = str(resolved.get("href") or "")

    file = ""
    if kind == "symbol" and "focus=" in href:
        file = href.split("focus=", 1)[1].split("&", 1)[0]
    elif kind == "file":
        file = label

    ontology_class = str(resolved.get("ontology_class") or "") or _KIND_TO_CLASS.get(kind, "")
    if not ontology_class and kind == "symbol":
        try:
            from prism_service.api.xref import _resolve_symbol
            sym = _resolve_symbol(brain_svc, token) or {}
            ontology_class = str(sym.get("entity_kind") or "").capitalize()
        except Exception:  # noqa: BLE001
            ontology_class = ""

    sections: list[dict] = []
    if kind == "task":
        sections.append(_task_facts(task_svc, token))
        sections.append(_task_knowledge(memory_svc, brain_svc, token))
        sections.append(_task_work(task_svc, token))
    elif kind in ("symbol", "file"):
        sections.append(_code_graph(graph_svc, file))
        sections.append(_symbols(brain_svc, label if kind == "symbol" else ""))
    sections.append(_brain(brain_svc, token))
    sections.append(_ontology(project, ontology_class))

    return {"token": token, "kind": kind, "label": label,
            "href": resolved.get("href"), "sections": sections}
