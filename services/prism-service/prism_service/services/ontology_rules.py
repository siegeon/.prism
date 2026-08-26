"""ontology_rules — the rules are SHACL shapes that can fail (task
8eeb3e65, epic 3efbcd89, owner: "the rules are SHACL shapes that can
fail"). Retires arc_governance.PROTOTYPE_AXIOMS as the axioms() source
(arc_governance.py's own PROTOTYPE_AXIOMS section carries a comment
pointing here) in favor of real SHACL shapes over the o: model
(prism_service/ontology/shapes.ttl), run with owlrl RDFS entailment first
so a rule targeting a superclass (e.g. o:QueueItem) also catches
o:Signal instances.

Keyed the way ontology-SKILL.md's "Adding to the model / A new rule"
prescribes: the property (or SPARQLConstraint) shape carries the rule's
own IRI, the node shape is <rule>.target. rule_catalog() reads that
straight off shapes.ttl via SPARQL — the shapes ARE the rule catalog,
never a second hand-kept list that can drift from what actually
validates ("the failure mode to watch for": one concept, two
representations).

validate(project) builds a scratch rdflib.Graph from the project's own
OntologyGraph (TBox + ABox), runs owlrl RDFS closure, then pyshacl in
advanced mode (SPARQLConstraint requires it), and PERSISTS the report as
triples into urn:prism:<project>/report — replacing whatever was there
before, the same remove_graph+bulk_load discipline OntologyGraph.rebuild
uses for the ABox itself (a report is a snapshot, never appended to).
evaluate(project) reads that report back; OntologyGraph.axioms() calls
evaluate() (never re-validates on a read path), and OntologyGraph.rebuild()
calls validate() at the end so every rebuild re-validates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import owlrl
import pyshacl
import rdflib

from prism_service.services.ontology_graph import NS, OntologyGraph

_SHAPES_TTL = Path(__file__).resolve().parent.parent / "ontology" / "shapes.ttl"
_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
_RDFS = rdflib.RDFS
_RDF = rdflib.RDF
_FOCUS_CAP = 100  # per-rule focus nodes kept in the persisted report
_DETAIL_CAP = 5  # per-rule focus nodes shown in evaluate()'s human detail


def _local_name(iri: str) -> str:
    return iri[len(NS):] if iri.startswith(NS) else iri


def _shapes_graph() -> rdflib.Graph:
    g = rdflib.Graph()
    g.parse(str(_SHAPES_TTL), format="turtle")
    return g


def rule_catalog() -> list[dict]:
    """Rule metadata (name/description/message/target_class), read
    straight off shapes.ttl: one row per <rule>.target node shape. This
    is the ONLY place rule names/descriptions live — never re-declared in
    Python, so shapes.ttl cannot silently drift from what this module
    reports."""
    g = _shapes_graph()
    q = f"""
        PREFIX sh: <{_SH}>
        PREFIX rdfs: <{_RDFS}>
        SELECT ?rule ?target ?desc ?msg WHERE {{
            ?node a sh:NodeShape ; sh:targetClass ?target .
            {{ ?node sh:property ?rule }} UNION {{ ?node sh:sparql ?rule }}
            OPTIONAL {{ ?rule rdfs:comment ?desc }}
            OPTIONAL {{ ?rule sh:message ?msg }}
        }}
    """
    out = []
    for row in g.query(q):
        out.append({
            "name": _local_name(str(row.rule)), "rule_iri": str(row.rule),
            "target_class": str(row.target),
            "description": str(row.desc) if row.desc else "",
            "message": str(row.msg) if row.msg else "",
        })
    out.sort(key=lambda r: r["name"])
    return out


def run_shapes(data_graph: rdflib.Graph) -> tuple[rdflib.Graph, dict[str, list[str]]]:
    """Run owlrl RDFS closure + pyshacl SHACL validation of shapes.ttl
    over a COPY of `data_graph` (the caller's own graph is left
    untouched). Pure — no store I/O. Returns (inferred_graph, violations)
    where violations maps rule name -> [focus node IRI, ...]; a rule
    absent from the dict had zero violations for this run — a rule that
    cannot appear here at all is decoration, not a rule."""
    g = rdflib.Graph()
    g += data_graph
    owlrl.DeductiveClosure(owlrl.RDFS_Semantics).expand(g)

    _conforms, report_graph, _text = pyshacl.validate(
        data_graph=g, shacl_graph=str(_SHAPES_TTL),
        data_graph_format=None, shacl_graph_format="turtle",
        advanced=True, meta_shacl=False, inference="none",
    )
    violations: dict[str, list[str]] = {}
    for result in report_graph.subjects(_RDF.type, _SH.ValidationResult):
        # SPARQLConstraint violations carry sh:sourceConstraint == the
        # rule IRI itself; plain property-shape violations (minCount)
        # carry no sourceConstraint and their sh:sourceShape IS the rule
        # IRI (the property shape, keyed per ontology-SKILL.md). Either
        # way this resolves to the rule, never the node shape.
        source = (report_graph.value(result, _SH.sourceConstraint)
                  or report_graph.value(result, _SH.sourceShape))
        focus = report_graph.value(result, _SH.focusNode)
        if source is None or focus is None:
            continue
        name = _local_name(str(source))
        if name.endswith(".target"):
            name = name[: -len(".target")]
        violations.setdefault(name, []).append(str(focus))
    return g, violations


def looked_at_counts(data_graph: rdflib.Graph, catalog: list[dict]) -> dict[str, int]:
    """Rule name -> count of instances of its node shape's sh:targetClass
    in `data_graph` — "SHACL reports what broke, never how many it looked
    at" (ontology-SKILL.md), so this is computed separately."""
    counts: dict[str, int] = {}
    for r in catalog:
        cls = rdflib.URIRef(r["target_class"])
        counts[r["name"]] = sum(1 for _ in data_graph.subjects(_RDF.type, cls))
    return counts


def _format_detail(row: dict) -> str:
    focus = row["focus"]
    shown = [f.rsplit("/", 1)[-1] for f in focus[:_DETAIL_CAP]]
    more = len(focus) - len(shown)
    suffix = f" (+{more} more)" if more > 0 else ""
    msg = row["message"] or "violated"
    return f"{msg}: {', '.join(shown)}{suffix}"


def _to_evaluate_shape(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        n_violations = len(r["focus"])
        out.append({
            "name": r["name"], "description": r["description"],
            "state": "violated" if n_violations else "quiet",
            "looked_at": r["looked_at"], "violations": n_violations,
            "detail": _format_detail(r) if n_violations else "",
        })
    return out


def validate(project: str) -> list[dict]:
    """Run the full SHACL pass for `project` and PERSIST the report,
    replacing whatever was on file. Returns the same shape evaluate()
    returns, computed directly (no extra read-back)."""
    graph = OntologyGraph(project)
    base = graph.to_rdflib()
    catalog = rule_catalog()
    inferred, violations = run_shapes(base)
    looked_at = looked_at_counts(inferred, catalog)

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in catalog:
        rows.append({
            "name": r["name"], "description": r["description"],
            "message": r["message"], "looked_at": looked_at.get(r["name"], 0),
            "focus": violations.get(r["name"], []), "validated_at": now,
        })
    _persist(graph, project, rows)
    return _to_evaluate_shape(rows)


def _persist(graph: OntologyGraph, project: str, rows: list[dict]) -> None:
    g = rdflib.Graph()
    o = rdflib.Namespace(NS)
    for r in rows:
        ru = rdflib.URIRef(f"{NS}rule/{quote(r['name'], safe='')}")
        g.add((ru, _RDF.type, o.Rule))
        g.add((ru, _RDFS.label, rdflib.Literal(r["name"])))
        g.add((ru, o.description, rdflib.Literal(r["description"])))
        g.add((ru, o.lookedAt, rdflib.Literal(r["looked_at"])))
        g.add((ru, o.violations, rdflib.Literal(len(r["focus"]))))
        g.add((ru, o.message, rdflib.Literal(r["message"])))
        g.add((ru, o.validatedAt,
               rdflib.Literal(r["validated_at"], datatype=rdflib.XSD.dateTime)))
        for f in r["focus"][:_FOCUS_CAP]:
            g.add((ru, o.focus, rdflib.URIRef(f)))
    graph.replace_graph(f"urn:prism:{project}/report", g.serialize(format="nt"))


def _read_report(project: str) -> list[dict]:
    graph = OntologyGraph(project)
    triples = graph.read_graph(f"urn:prism:{project}/report")
    by_rule: dict[str, dict] = {}
    for s, p, o in triples:
        row = by_rule.setdefault(s, {"focus": []})
        if p == str(_RDFS.label):
            row["name"] = o
        elif p == f"{NS}description":
            row["description"] = o
        elif p == f"{NS}lookedAt":
            row["looked_at"] = int(o)
        elif p == f"{NS}message":
            row["message"] = o
        elif p == f"{NS}validatedAt":
            row["validated_at"] = o
        elif p == f"{NS}focus":
            row["focus"].append(o)
    out = []
    for row in by_rule.values():
        if "name" not in row:
            continue
        row.setdefault("description", "")
        row.setdefault("looked_at", 0)
        row.setdefault("message", "")
        row.setdefault("validated_at", "")
        out.append(row)
    out.sort(key=lambda r: r["name"])
    return out


def evaluate(project: str) -> list[dict]:
    """Read the persisted SHACL report back — never re-validates. If no
    report is on file yet for this project (rebuild()/validate() never
    ran), validate() once so this never silently reports nothing."""
    rows = _read_report(project)
    if not rows:
        return validate(project)
    return _to_evaluate_shape(rows)


def full_report(project: str) -> dict:
    """GET /api/okf/ontology/rules — the whole persisted report, per rule:
    focus nodes capped at 20."""
    rows = _read_report(project)
    if not rows:
        validate(project)
        rows = _read_report(project)
    rules = []
    for r in rows:
        rules.append({
            "name": r["name"], "description": r["description"],
            "message": r["message"], "looked_at": r["looked_at"],
            "violations": len(r["focus"]), "focus": r["focus"][:20],
            "validated_at": r["validated_at"],
        })
    return {"rules": rules}
