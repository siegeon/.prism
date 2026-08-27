"""ontology_rules — the rules are SHACL shapes that can fail (task
8eeb3e65, epic 3efbcd89, owner: "the rules are SHACL shapes that can
fail"). Retires arc_governance.PROTOTYPE_AXIOMS as the axioms() source
(arc_governance.py's own PROTOTYPE_AXIOMS section carries a comment
pointing here) in favor of real SHACL shapes over the o: model
(prism_service/ontology/shapes.ttl), run with a targeted rdfs:subClassOf
type expansion first (task 6503d7f8 — see _expand_subclass_types) so a
rule targeting a superclass (e.g. o:QueueItem) also catches o:Signal
instances.

Keyed the way ontology-SKILL.md's "Adding to the model / A new rule"
prescribes: the property (or SPARQLConstraint) shape carries the rule's
own IRI, the node shape is <rule>.target. rule_catalog() reads that
straight off shapes.ttl via SPARQL — the shapes ARE the rule catalog,
never a second hand-kept list that can drift from what actually
validates ("the failure mode to watch for": one concept, two
representations).

validate(project) builds a scratch rdflib.Graph from the project's own
OntologyGraph (TBox + ABox), runs a targeted rdfs:subClassOf type
expansion (task 6503d7f8 — see _expand_subclass_types), then pyshacl in
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


# ----------------------------------------------------------------------
# Targeted rdfs:subClassOf type expansion (task 6503d7f8 — "make
# validation itself fast"). Measured 2026-08-27: owlrl's full RDFS
# closure over the prism project's real code graph (56518 -> 106656
# triples) cost 34s of validate()'s ~65s total. No shape in shapes.ttl /
# shapes-knowledge.ttl reads rdfs:domain/range or rdfs:subPropertyOf
# entailment — every rule's SPARQL select or sh:path walks a property
# straight off an already-typed instance. The ONLY RDFS behaviour any
# shape actually depends on is: a shape targeting a superclass
# (o:Concept, o:Ask, ...) must still catch instances the emitter typed
# with a subclass only (o:Decision, o:AskForDecision, ...) — see
# services/ontology_graph.py's _emit_memories/_emit_extraction_joins,
# which type every instance with its LEAF class, never the ancestor
# chain. So this replaces owlrl with plain Python: read the
# rdfs:subClassOf tree straight off the graph already at hand (to_
# rdflib() already merges model.ttl's TBox into every data graph — no
# second parse), close it transitively, and add one rdf:type triple per
# (instance, ancestor) pair. o:Code and its subclasses are excluded
# (_skippable_code_classes) UNLESS `project` has a rule that targets one
# of them — the code graph is the overwhelming majority of instances,
# and ordinarily no shape targets o:Code, so expanding it would be pure
# cost with zero shapes benefiting from it; but a project's own promoted
# rule (law_promotion) can target o:Code directly, so the exclusion is
# computed fresh per project on every call, never assumed.
# ----------------------------------------------------------------------

_CODE_CLASS = rdflib.URIRef(NS + "Code")


def _code_descendant_classes(g: rdflib.Graph) -> set:
    """o:Code plus every class that is a (transitive) rdfs:subClassOf
    descendant of it, computed from g's own TBox triples — never a
    hardcoded class-name list, so a new o:Code subclass added to
    model.ttl is picked up automatically."""
    children: dict = {}
    for child, _, parent in g.triples((None, _RDFS.subClassOf, None)):
        children.setdefault(parent, set()).add(child)
    out = {_CODE_CLASS}
    stack = [_CODE_CLASS]
    while stack:
        cls = stack.pop()
        for kid in children.get(cls, ()):
            if kid not in out:
                out.add(kid)
                stack.append(kid)
    return out


def _targeted_classes(project: str | None) -> set:
    """Every target_class IRI (as rdflib.URIRef) that SOME rule in
    rule_catalog(project) actually targets — built-in shapes*.ttl PLUS
    this project's own promoted-shapes.ttl. Read fresh on every call,
    never cached: law_promotion.install_pending can install a brand new
    rule into a project's promoted-shapes.ttl at any time, and a
    code-architecture rule (a real, shipped example: 'prism-service-api-
    must-not-depend-on-prism-service-engines') can target o:Code or one
    of its subclasses directly."""
    out = set()
    for r in rule_catalog(project):
        for cls in (r.get("target_classes") or [r["target_class"]]):
            out.add(rdflib.URIRef(cls))
    return out


def _skippable_code_classes(g: rdflib.Graph, project: str | None) -> set:
    """o:Code and its subclasses that NO active rule targets (task
    6503d7f8). This is the set _expand_subclass_types/_mark_twin_classes/
    _without_code_instances are allowed to exclude — never the full
    _code_descendant_classes(g) unconditionally, because a project's own
    promoted rule can target o:Code or a subclass of it (see
    _targeted_classes). If a shape ever adds a genuine o:Code target,
    this set shrinks to match on its own, with no code change needed
    here."""
    return _code_descendant_classes(g) - _targeted_classes(project)


def _ancestor_map(g: rdflib.Graph) -> dict:
    """class -> the full set of its rdfs:subClassOf ancestors, read off
    g's own TBox triples and closed transitively in plain Python — the
    class tree is small (model.ttl declares a few dozen classes), so
    this is negligible next to the ABox-sized cost owlrl paid."""
    direct: dict = {}
    for child, _, parent in g.triples((None, _RDFS.subClassOf, None)):
        direct.setdefault(child, set()).add(parent)
    memo: dict = {}

    def _close(cls):
        if cls in memo:
            return memo[cls]
        seen: set = set()
        stack = list(direct.get(cls, ()))
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            stack.extend(direct.get(p, ()))
        memo[cls] = seen
        return seen

    for cls in list(direct):
        _close(cls)
    return memo


def _expand_subclass_types(g: rdflib.Graph, project: str | None = None) -> None:
    """Mutates `g` in place: for every (instance, rdf:type, class)
    triple, add (instance, rdf:type, ancestor) for each of class's
    rdfs:subClassOf ancestors — skipping o:Code and its subclasses,
    UNLESS `project` has a rule that targets one of them (see
    _skippable_code_classes). This is the entire replacement for
    owlrl's RDFS closure; see the module note above this function."""
    ancestors = _ancestor_map(g)
    skip = _skippable_code_classes(g, project)
    to_add = []
    for s, _, o in g.triples((None, _RDF.type, None)):
        if o in skip:
            continue
        for anc in ancestors.get(o, ()):
            if anc in skip:
                continue
            to_add.append((s, _RDF.type, anc))
    for t in to_add:
        g.add(t)


# ----------------------------------------------------------------------
# twin-classes precompute (task 6503d7f8). Even after shapes.ttl's
# twin-classes select excludes o:Code and its subclasses from being
# $this/?other, pyshacl/rdflib still evaluate the rule's nested
# FILTER EXISTS { ... FILTER NOT EXISTS { ... } } once PER SURVIVING
# CANDIDATE CLASS, and rdflib's SPARQL engine re-walks graph-sized
# structures inside each nested EXISTS rather than a pushed-down index
# lookup — measured cost stayed dominant (~27s of a 35s run) on a
# 60k-triple seeded graph even with the SPARQL-level exclusion in
# place, because the cost scales with overall graph size, not with how
# many classes are left as candidates. Fix: compute the actual answer
# in plain Python (one pass grouping every non-Code class by its
# frozenset of direct instances) and hand the shape a single scratch
# marker triple to read — turns an O(candidates x graph) nested SPARQL
# scan into an O(1)-per-focus-node property lookup.
# ----------------------------------------------------------------------

_TWIN_MARKER = rdflib.URIRef(f"{NS}internal/isTwinClass")


def _mark_twin_classes(g: rdflib.Graph, project: str | None = None) -> None:
    """Mutates `g` in place: for every group of 2+ o: classes (excluding
    o:Code and its subclasses, UNLESS `project` has a rule targeting one
    of them — see _skippable_code_classes) that share an identical
    NON-EMPTY set of direct instances, add (class, _TWIN_MARKER, true)
    to every class in that group. shapes.ttl's twin-classes rule reads
    this marker directly instead of computing it in SPARQL.
    _TWIN_MARKER is a scratch predicate: it exists only in this
    throwaway validation graph, never in a project's persisted ABox or
    TBox, and carries no rdfs: declaration of its own for that reason."""
    skip = _skippable_code_classes(g, project)
    classes = {c for c in g.subjects(_RDF.type, _RDFS.Class) if c not in skip}
    # task cacfb628 (owner 2026-08-27 via Extract Superclass): a parent that
    # DECLARES two or more subclasses is not a twin of the one child that
    # happens to hold instances today - the other siblings are unpopulated,
    # not redundant (Work/JiraIssue+PullRequest, Party/Person+Group+...,
    # Ask/five kinds all fired on the prism project for that reason). A
    # twin is a parent with exactly one declared child, or two unrelated
    # classes, sharing every instance.
    declared_children: dict = {}
    for child, parent in g.subject_objects(_RDFS.subClassOf):
        declared_children.setdefault(parent, set()).add(child)
    by_instances: dict = {}
    for cls in classes:
        if len(declared_children.get(cls, ())) >= 2:
            continue
        instances = frozenset(g.subjects(_RDF.type, cls))
        if not instances:
            continue
        by_instances.setdefault(instances, []).append(cls)
    for group in by_instances.values():
        if len(group) > 1:
            for cls in group:
                g.add((cls, _TWIN_MARKER, rdflib.Literal(True)))


# PROCESS ISOLATION for the type-expansion + pyshacl pass. After the lock, the LRU,
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


# ----------------------------------------------------------------------
# Prepared-query cache for pyshacl's SPARQLConstraint evaluation (task
# 6503d7f8). Measured on a copy of the prism project's real ontology
# store (60951 triples, 3395 combined focus nodes across the ~18
# SPARQLConstraint rules in shapes*.ttl): after the owlrl replacement
# and the twin-classes precompute above, pyshacl.validate() still took
# ~28s wall-clock, ~27s of it inside rdflib's pyparsing SPARQL parser
# (cProfile: parseQuery -> 3395 calls). pyshacl's own
# pre_bind_variables() (helper/sparql_query_helper.py) does NOT
# string-substitute $this per focus node — it passes the SAME query
# TEXT for every focus node of a rule and threads $this through
# rdflib's initBindings — but rdflib's SPARQLProcessor.query() still
# re-parses and re-translates that identical text from scratch on
# every single call. rdflib's own documented "prepared query" pattern
# (prepareQuery(), then execute the SAME compiled Query object many
# times with different initBindings) is exactly the reuse this needs;
# this patches SPARQLProcessor.query() to do that reuse automatically,
# caching by (query text, base, initNs) — a pure function of those
# three inputs, so cross-caller reuse is safe. Deliberately confined to
# _run_shapes_impl's own process: this module runs the whole pyshacl
# pass in an isolated, short-lived child interpreter (see the note
# above), so the cache never accumulates across unrelated daemon
# requests. Falls back to rdflib's own, unpatched behaviour on any
# error — a caching bug must never turn into a validation failure.
# ----------------------------------------------------------------------

_PREPARED_QUERY_CACHE: dict = {}
_SPARQL_CACHE_PATCHED = False


def _patch_sparql_query_cache() -> None:
    try:
        from rdflib.plugins.sparql import processor as _sparql_processor
        from rdflib.plugins.sparql.algebra import translateQuery
        from rdflib.plugins.sparql.evaluate import evalQuery
        from rdflib.plugins.sparql.parser import parseQuery

        global _SPARQL_CACHE_PATCHED
        if _SPARQL_CACHE_PATCHED:
            return

        def _prepared(query_text, base, init_ns):
            key = (query_text, base,
                   tuple(sorted(init_ns.items())) if init_ns else None)
            q = _PREPARED_QUERY_CACHE.get(key)
            if q is None:
                q = translateQuery(parseQuery(query_text), base, init_ns)
                _PREPARED_QUERY_CACHE[key] = q
            return q

        def _cached_query(self, strOrQuery, initBindings=None, initNs=None,
                           base=None, DEBUG=False):
            if isinstance(strOrQuery, str):
                strOrQuery = _prepared(strOrQuery, base, initNs)
            return evalQuery(self.graph, strOrQuery, initBindings, base)

        _sparql_processor.SPARQLProcessor.query = _cached_query
        _SPARQL_CACHE_PATCHED = True
    except Exception:  # noqa: BLE001 - never let a caching optimization break validation
        pass


def _without_code_instances(g: rdflib.Graph, project: str | None = None) -> rdflib.Graph:
    """A COPY of `g` with every triple about an o:Code instance dropped —
    UNLESS `project` has a rule targeting o:Code or one of its
    subclasses (see _skippable_code_classes; a real, shipped example is
    the code-architecture rule 'prism-service-api-must-not-depend-on-
    prism-service-engines' that law_promotion can install into a
    project's own promoted-shapes.ttl). Task 6503d7f8: when nothing
    targets o:Code, this data can never change a validation result, so
    it only affects how many bytes cross the subprocess boundary.
    Measured on the prism project's real ontology store: the code graph
    is ~50k of ~61k total triples, so stripping it before serialising
    roughly quarters the payload each way (input AND output). This is a
    belt-and-braces optimisation ON TOP OF _expand_subclass_types/
    _mark_twin_classes's own Code exclusions (which stay in place as
    the correctness guard for every OTHER caller of this module —
    PRISM_SHACL_IN_PROCESS=1, and tests that call _run_shapes_impl
    directly with a graph that still has its Code triples)."""
    skip = _skippable_code_classes(g, project)
    code_instances = {s for s, _, o in g.triples((None, _RDF.type, None)) if o in skip}
    out = rdflib.Graph()
    for s, p, o in g:
        if s not in code_instances:
            out.add((s, p, o))
    return out


def run_shapes(
    data_graph: rdflib.Graph, project: str | None = None,
) -> tuple[rdflib.Graph, dict[str, list[str]]]:
    """Targeted subclass type expansion + pyshacl over shapes*.ttl in an
    isolated child process (see the note above) — the tests call this
    directly with fixture graphs. `project`, when given, also merges
    that project's own promoted-shapes.ttl (task c5650403)."""
    if os.environ.get("PRISM_SHACL_IN_PROCESS") == "1":
        return _run_with_big_stack(_run_shapes_impl, data_graph, project)
    nt = _without_code_instances(data_graph, project).serialize(format="nt")
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
    # task 6503d7f8: drop every triple about an o:Code instance before
    # crossing back to the parent, UNLESS `project` has a rule that
    # targets o:Code or a subclass of it (see _skippable_code_classes).
    # Measured on the prism project's real ontology store: the code
    # graph is ~50k of ~61k total triples, and a full-codebase grep of
    # every run_shapes() caller (production and tests) shows NOTHING
    # reads the returned inferred graph's content except
    # _validate_impl's looked_at_counts() -- which only counts instances
    # of each rule's target class. Round-tripping those triples through
    # N-Triples twice (serialize here, parse in the parent) cost real
    # wall-clock time for zero benefit when nothing targets o:Code.
    skip = _skippable_code_classes(inferred, project)
    code_instances = {
        s for s, _, o in inferred.triples((None, _RDF.type, None)) if o in skip
    }
    out = rdflib.Graph()
    for s, p, o in inferred:
        # Defensive: rdflib can hold a literal as a triple's subject in
        # memory but no RDF syntax can carry one, so the parent's
        # N-Triples parser would reject the line. _expand_subclass_types
        # never types a literal itself, but this guard is cheap and
        # stays in case a caller's own data_graph ever carries one.
        if isinstance(s, rdflib.Literal) or s in code_instances:
            continue
        out.add((s, p, o))
    sys.stdout.write(json.dumps({"inferred_nt": out.serialize(format="nt"),
                                 "violations": violations}))
    sys.stdout.flush()
    return 0


def _run_shapes_impl(
    data_graph: rdflib.Graph, project: str | None = None,
) -> tuple[rdflib.Graph, dict[str, list[str]]]:
    """Run the targeted subclass type expansion + pyshacl SHACL validation
    of shapes.ttl (plus `project`'s own promoted-shapes.ttl, when given)
    over a COPY of `data_graph` (the caller's own graph is left
    untouched). Pure — no
    store I/O. Returns (inferred_graph, violations) where violations maps
    rule name -> [focus node IRI, ...]; a rule absent from the dict had
    zero violations for this run — a rule that cannot appear here at all
    is decoration, not a rule."""
    g = rdflib.Graph()
    g += data_graph
    _expand_subclass_types(g, project)
    _mark_twin_classes(g, project)
    _patch_sparql_query_cache()

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


# A firing rule becomes a decision on the Queue (task b1971944, epic
# 61821448). Listeners registered here run at the end of every
# _validate_impl, with (project, report_rows) -- report_rows is the SAME
# list _persist writes from, before any focus cap is applied. A listener
# that raises never breaks validate() itself.
_ON_VALIDATED: list = []


def on_validated(callback) -> None:
    """Register `callback(project, report_rows)` to run after every
    validate() persists its report. report_rows is a list of
    {"name","title","description","message","looked_at","focus","validated_at"}
    dicts, one per rule in the catalog (a quiet rule has an empty
    "focus")."""
    _ON_VALIDATED.append(callback)


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
    for _cb in _ON_VALIDATED:
        try:
            _cb(project, rows)
        except Exception:
            pass
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
# Validation after a rebuild (task f9e0745e). A full code graph USED TO
# make validate() cost about a minute (owlrl closure plus pyshacl over
# 56k triples on the prism project) — task 6503d7f8 replaced the owlrl
# closure with _expand_subclass_types, a targeted expansion that skips
# o:Code entirely, so this cost is now the exception rather than the
# norm. Above ASYNC_VALIDATE_TRIPLES the work still runs on one
# background thread per project as a safety net; a rebuild that lands
# while a validation is running marks it and the thread runs once more
# when it finishes, so the report always reflects the last ABox. Below
# the threshold (every test fixture) validation stays inline.
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
