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

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import owlrl
import pyshacl
import rdflib

from prism_service.services.ontology_graph import NS, OntologyGraph

_ONTOLOGY_DIR = Path(__file__).resolve().parent.parent / "ontology"
_SHAPES_TTL = _ONTOLOGY_DIR / "shapes.ttl"
_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
_RDFS = rdflib.RDFS
_RDF = rdflib.RDF
_FOCUS_CAP = 100  # per-rule focus nodes kept in the persisted report
_DETAIL_CAP = 5  # per-rule focus nodes shown in evaluate()'s human detail


# CPython 3.11 has no C-stack guard: pyparsing's recursive descent (rdflib's
# SPARQL parser, which pyshacl drives for every SPARQL-constraint shape and
# rule_catalog drives over shapes.ttl) burns far more native stack per Python
# frame than sys.getrecursionlimit() accounts for, and the default 8 MiB main
# thread stack overflows into a plain SIGSEGV instead of a RecursionError —
# the unit suite died that way (rc=139, faulthandler top frame
# pyparsing/core.py _parseNoCache) on 2026-08-25. Run that work on a thread
# whose stack is big enough that the recursion limit is the binding limit.
_BIG_STACK_BYTES = 512 * 1024 * 1024


def _run_with_big_stack(fn, *args, **kwargs):
    import threading

    result: dict = {}

    def _target() -> None:
        try:
            result["value"] = fn(*args, **kwargs)
        except BaseException as exc:  # re-raised on the caller's thread
            result["error"] = exc

    old = threading.stack_size()
    try:
        threading.stack_size(_BIG_STACK_BYTES)
        t = threading.Thread(target=_target, name="ontology-rules-bigstack", daemon=True)
        t.start()
    finally:
        threading.stack_size(old)
    t.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _local_name(iri: str) -> str:
    return iri[len(NS):] if iri.startswith(NS) else iri


def _project_shapes_path(project: str | None) -> Path | None:
    """<project data dir>/ontology/promoted-shapes.ttl when `project` is
    given and the file exists -- rules services/law_promotion.py installs
    after an owner approves them (task c5650403). None (never a path that
    doesn't exist) so callers can just skip it."""
    if not project:
        return None
    from prism_service.config import project_data_dir
    path = project_data_dir(project) / "ontology" / "promoted-shapes.ttl"
    return path if path.exists() else None


def _shapes_paths(project: str | None = None) -> list[Path]:
    """Every rule file: shapes.ttl plus any ontology/shapes*.ttl extension
    (task f5352fa1's shapes-knowledge.ttl) -- sorted so load order is
    deterministic across runs. Also `project`'s own promoted-shapes.ttl,
    appended last, when given and on file."""
    paths = sorted(_ONTOLOGY_DIR.glob("shapes*.ttl"))
    extra = _project_shapes_path(project)
    if extra is not None:
        paths.append(extra)
    return paths


def _shapes_graph(project: str | None = None) -> rdflib.Graph:
    """Every shapes*.ttl file (plus `project`'s own promoted-shapes.ttl,
    when given) merged into one graph -- rule_catalog() and run_shapes()
    both read this, so a new rule file is picked up by both without a
    second load path that could drift."""
    g = rdflib.Graph()
    for path in _shapes_paths(project):
        g.parse(str(path), format="turtle")
    return g


def _derived_from_local(iri: str) -> str:
    """<urn:prism:onto:instance/memory/mx-...> -> "mx-..." -- the memory
    id a promoted rule or term's o:derivedFrom points at (task c5650403).
    Any other IRI shape passes through unchanged."""
    prefix = f"{NS}instance/memory/"
    return iri[len(prefix):] if iri.startswith(prefix) else iri


def rule_catalog(project: str | None = None) -> list[dict]:
    """The rules declared in shapes*.ttl, plus `project`'s own promoted
    rules when given (read via rdflib SPARQL — pyparsing recursion, hence
    the big-stack thread; see _run_with_big_stack). Omitting `project`
    keeps the original, project-agnostic behaviour."""
    return _run_with_big_stack(_rule_catalog_impl, project)


def _rule_catalog_impl(project: str | None = None) -> list[dict]:
    """Rule metadata (name/description/message/target_class/derived_from),
    read straight off shapes.ttl: one row per <rule>.target node shape.
    This is the ONLY place rule names/descriptions live — never re-declared
    in Python, so shapes.ttl cannot silently drift from what this module
    reports."""
    g = _shapes_graph(project)
    q = f"""
        PREFIX sh: <{_SH}>
        PREFIX rdfs: <{_RDFS}>
        PREFIX o: <{NS}>
        SELECT ?rule ?target ?name ?desc ?msg ?derived WHERE {{
            ?node a sh:NodeShape ; sh:targetClass ?target .
            {{ ?node sh:property ?rule }} UNION {{ ?node sh:sparql ?rule }}
            OPTIONAL {{ ?rule sh:name ?name }}
            OPTIONAL {{ ?rule sh:description ?desc }}
            OPTIONAL {{ ?rule sh:message ?msg }}
            OPTIONAL {{ ?rule o:derivedFrom ?derived }}
        }}
    """
    # One row per RULE. A constraint shared by several node shapes (the
    # text-is-plain rule targets Task, Decision, Term and Agent, task
    # 5ac5d04c) collects every target class on that one row instead of
    # repeating the rule once per class on the Rules tab.
    by_name: dict[str, dict] = {}
    for row in g.query(q):
        name = _local_name(str(row.rule))
        entry = by_name.get(name)
        if entry is None:
            entry = by_name[name] = {
                "name": name, "rule_iri": str(row.rule),
                "target_class": str(row.target), "target_classes": [],
                "title": str(row.name) if row.name else "",
                "description": str(row.desc) if row.desc else "",
                "message": str(row.msg) if row.msg else "",
                "derived_from": _derived_from_local(str(row.derived)) if row.derived else "",
            }
        if str(row.target) not in entry["target_classes"]:
            entry["target_classes"].append(str(row.target))
    out = sorted(by_name.values(), key=lambda r: r["name"])
    return out


# PROCESS ISOLATION for the owlrl + pyshacl pass. After the lock, the LRU,
# store-independent term copies and a 512 MiB thread, the unit suite still
# died with SIGSEGV inside rdflib's SPARQL machinery on the validation
# thread (2026-08-25, task aa7fd8fb) — a native fault nobody could pin to a
# Python frame (faulthandler's dump itself dies walking that thread). The
# validation is pure (graph in, graph + violations out), so it runs in a
# child interpreter over N-Triples on stdin/stdout: whatever the fault is,
# it can no longer take the daemon or the test process down — it becomes a
# RuntimeError with the child's stderr attached. Set
# PRISM_SHACL_IN_PROCESS=1 to run in-process (debugging only).
_WORKER_TIMEOUT_S = 900


def run_shapes(
    data_graph: rdflib.Graph, project: str | None = None,
) -> tuple[rdflib.Graph, dict[str, list[str]]]:
    """owlrl closure + pyshacl over shapes*.ttl in an isolated child process
    (see the note above) — the tests call this directly with fixture
    graphs. `project`, when given, also merges that project's own
    promoted-shapes.ttl (task c5650403)."""
    if os.environ.get("PRISM_SHACL_IN_PROCESS") == "1":
        return _run_with_big_stack(_run_shapes_impl, data_graph, project)
    nt = data_graph.serialize(format="nt")
    argv = [sys.executable, "-m", "prism_service.services.ontology_rules", "--worker"]
    if project:
        argv.append(project)
    proc = subprocess.run(
        argv, input=nt.encode("utf-8"), capture_output=True,
        timeout=_WORKER_TIMEOUT_S, env=os.environ.copy(),
    )
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace")[-4000:]
        raise RuntimeError(f"SHACL worker failed rc={proc.returncode}: {tail}")
    payload = json.loads(proc.stdout.decode("utf-8"))
    inferred = rdflib.Graph()
    inferred.parse(data=payload["inferred_nt"], format="nt")
    return inferred, {k: list(v) for k, v in payload["violations"].items()}


def _worker_main() -> int:
    """Child-process entry: N-Triples on stdin -> JSON on stdout. An
    optional project name on argv[2] merges that project's own
    promoted-shapes.ttl (task c5650403)."""
    data = rdflib.Graph()
    data.parse(data=sys.stdin.read(), format="nt")
    project = sys.argv[2] if len(sys.argv) > 2 else None
    inferred, violations = _run_with_big_stack(_run_shapes_impl, data, project)
    # owlrl's RDFS closure asserts `<literal> rdf:type rdfs:Resource`; rdflib
    # holds literal subjects in memory but no RDF syntax can carry them, and
    # the parent's N-Triples parser rejects the line — drop them here.
    out = rdflib.Graph()
    for s, p, o in inferred:
        if not isinstance(s, rdflib.Literal):
            out.add((s, p, o))
    sys.stdout.write(json.dumps({"inferred_nt": out.serialize(format="nt"),
                                 "violations": violations}))
    sys.stdout.flush()
    return 0


def _run_shapes_impl(
    data_graph: rdflib.Graph, project: str | None = None,
) -> tuple[rdflib.Graph, dict[str, list[str]]]:
    """Run owlrl RDFS closure + pyshacl SHACL validation of shapes.ttl
    (plus `project`'s own promoted-shapes.ttl, when given) over a COPY of
    `data_graph` (the caller's own graph is left untouched). Pure — no
    store I/O. Returns (inferred_graph, violations) where violations maps
    rule name -> [focus node IRI, ...]; a rule absent from the dict had
    zero violations for this run — a rule that cannot appear here at all
    is decoration, not a rule."""
    g = rdflib.Graph()
    g += data_graph
    owlrl.DeductiveClosure(owlrl.RDFS_Semantics).expand(g)

    _conforms, report_graph, _text = pyshacl.validate(
        data_graph=g, shacl_graph=_shapes_graph(project),
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


if __name__ == "__main__" and "--worker" in sys.argv:
    sys.exit(_worker_main())


def looked_at_counts(data_graph: rdflib.Graph, catalog: list[dict]) -> dict[str, int]:
    """Rule name -> count of instances of its node shape's sh:targetClass
    in `data_graph` — "SHACL reports what broke, never how many it looked
    at" (ontology-SKILL.md), so this is computed separately."""
    counts: dict[str, int] = {}
    for r in catalog:
        classes = r.get("target_classes") or [r["target_class"]]
        seen: set = set()
        for cls in classes:
            seen.update(data_graph.subjects(_RDF.type, rdflib.URIRef(cls)))
        counts[r["name"]] = len(seen)
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
    returns, computed directly (no extra read-back). Runs on a big-stack
    thread — see _run_with_big_stack."""
    return _run_with_big_stack(_validate_impl, project)


def _validate_impl(project: str) -> list[dict]:
    graph = OntologyGraph(project)
    base = graph.to_rdflib()
    catalog = rule_catalog(project)
    inferred, violations = run_shapes(base, project)
    looked_at = looked_at_counts(inferred, catalog)

    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in catalog:
        rows.append({
            "name": r["name"], "title": r["title"], "description": r["description"],
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
        g.add((ru, o.title, rdflib.Literal(r.get("title", ""))))
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
        elif p == f"{NS}title":
            row["title"] = o
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
        row.setdefault("title", "")
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


def last_validated_at(project: str) -> str:
    """The persisted report's own validated_at (task 7dbb242f) — reused by
    structure()/records() as their 'last rebuilt' timestamp, since
    rebuild() always ends with validate() writing this report. Runs
    validate() once if no report exists yet, same fallback evaluate() and
    full_report() use."""
    rows = _read_report(project)
    if not rows:
        validate(project)
        rows = _read_report(project)
    return rows[0]["validated_at"] if rows else ""


def full_report(project: str) -> dict:
    """GET /api/okf/ontology/rules — the whole persisted report, per rule:
    title (sh:name), description, focus as [{iri, label}] capped at 20
    (labels via rdfs:label off the live graph), derived_from (task
    c5650403: which memory produced a promoted rule, empty for every
    built-in rule), and need_decision/total — task 7dbb242f."""
    rows = _read_report(project)
    if not rows:
        validate(project)
        rows = _read_report(project)

    graph = OntologyGraph(project)
    derived_by_name = {r["name"]: r.get("derived_from", "")
                       for r in rule_catalog(project)}
    rules = []
    need_decision = 0
    validated_at = ""
    for r in rows:
        focus_iris = r["focus"][:20]
        n_violations = len(r["focus"])
        if n_violations:
            need_decision += 1
        validated_at = r.get("validated_at", "") or validated_at
        rules.append({
            "name": r["name"], "title": r.get("title", ""),
            "description": r["description"], "message": r["message"],
            "looked_at": r["looked_at"], "violations": n_violations,
            "focus": [{"iri": iri, "label": graph.label_of(iri)} for iri in focus_iris],
            "validated_at": r["validated_at"],
            "derived_from": derived_by_name.get(r["name"], ""),
        })
    return {"rules": rules, "need_decision": need_decision,
            "total": len(rules), "validated_at": validated_at}


# ----------------------------------------------------------------------
# Validation after a rebuild (task f9e0745e). A full code graph makes
# validate() cost about a minute (owlrl closure plus pyshacl over 56k
# triples on the prism project). Above ASYNC_VALIDATE_TRIPLES the work
# runs on one background thread per project; a rebuild that lands while
# a validation is running marks it and the thread runs once more when it
# finishes, so the report always reflects the last ABox. Below the
# threshold (every test fixture) validation stays inline. Task 6503d7f8
# makes validation itself fast.
# ----------------------------------------------------------------------

ASYNC_VALIDATE_TRIPLES = 20_000
_ASYNC_LOCK = __import__("threading").Lock()
_ASYNC_RUNNING: set = set()
_ASYNC_PENDING: set = set()


def validate_after_rebuild(project: str, triple_count: int) -> str:
    """Validate inline for a small graph. For a large one, validate on a
    background thread and return "scheduled" (or "queued" when a run is
    already in flight). Returns "inline" when it ran synchronously."""
    import threading

    if triple_count < ASYNC_VALIDATE_TRIPLES or os.environ.get("PRISM_ONTOLOGY_VALIDATE_SYNC") == "1":
        validate(project)
        return "inline"
    with _ASYNC_LOCK:
        if project in _ASYNC_RUNNING:
            _ASYNC_PENDING.add(project)
            return "queued"
        _ASYNC_RUNNING.add(project)

    def _run() -> None:
        try:
            while True:
                try:
                    validate(project)
                except Exception:  # noqa: BLE001 - a failed validation must not kill the thread
                    pass
                with _ASYNC_LOCK:
                    if project in _ASYNC_PENDING:
                        _ASYNC_PENDING.discard(project)
                        continue
                    _ASYNC_RUNNING.discard(project)
                    return
        finally:
            with _ASYNC_LOCK:
                _ASYNC_RUNNING.discard(project)

    threading.Thread(target=_run, name=f"ontology-validate-{project}", daemon=True).start()
    return "scheduled"


def validation_in_flight(project: str) -> bool:
    with _ASYNC_LOCK:
        return project in _ASYNC_RUNNING
