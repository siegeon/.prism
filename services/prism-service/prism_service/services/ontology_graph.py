"""OntologyGraph — the ontology as an RDF graph you can query with SPARQL
(task 495d3a69, epic 3efbcd89, owner: "it does not look like you used the
ontology rules and libraries we had in the subsume project").

The Subsume prototype's split (ontology-SKILL.md "The graph store, and why
the model lives in it"): rdflib is the modelling API that emits classes and
instances as RDF, pyoxigraph is the persisted STORE holding one REPLACED
named graph, and SPARQL is how anything asks the model a question. This
class is that store for one PRISM project:

  <project_data_dir>/ontology-graph/   pyoxigraph on-disk Store
  urn:prism:onto:model                  the shared TBox (ontology/model.ttl)
  urn:prism:<project>/model             this project's ABox, replaced whole
                                         on every rebuild() (remove_graph +
                                         bulk_load) — two rebuilds must not
                                         double the triple count

ontology_store.py (sqlite) stays wired as a thin best-effort CACHE — still
populated by ontology_prototype_projection.rebuild() because
tests/unit/test_prototype_axioms.py reads it directly and sits outside this
task's allowed_files — but it is no longer api/okf.py's READ PATH:
classes()/instances()/properties()/axioms() below answer from the graph.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote, unquote

import pyoxigraph as ox

from prism_service.config import project_data_dir

NS = "urn:prism:onto:"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_ONTOLOGY_DIR = Path(__file__).resolve().parent.parent / "ontology"
_MODEL_TTL = _ONTOLOGY_DIR / "model.ttl"

# The prototype's flat class catalog (api/okf.py's response shape, unchanged
# since task 15c06516/c1d0ee70), mapped onto the TBox classes model.ttl
# declares. "cls" is the o: local name instances get typed with; "source"
# groups the OntologyPanel.tsx rail into Prototype vs Code graph.
BASE_CATALOG = [
    {"id": "Channel", "cls": "Channel", "source": "tasks", "bucket": "channel"},
    {"id": "Agent", "cls": "Agent", "source": "workflows", "bucket": "agent"},
    {"id": "Provider", "cls": "Provider", "source": "integrations", "bucket": "provider"},
    {"id": "Task", "cls": "Task", "source": "tasks", "bucket": "task"},
    # QueueItem <- SIGNALS (task 785bb4ce): instances are typed o:Signal, a
    # subclass of o:QueueItem in model.ttl, so both class queries count them.
    {"id": "QueueItem", "cls": "Signal", "source": "signals", "bucket": "signal"},
    {"id": "Document", "cls": "Document", "source": "brain", "bucket": "document"},
    {"id": "Folder", "cls": "Folder", "source": "brain", "bucket": "folder"},
]
_CATALOG_BY_ID = {c["id"]: c for c in BASE_CATALOG}

# Code-graph entity kind (graph.db entities.kind, lowercase) -> o: subclass
# of o:Code. Anything unmapped still gets typed as the generic o:Code.
_CODE_KIND_CLASS = {
    "function": "Function", "method": "Method", "class": "Class",
    "module": "Module", "file": "Module", "interface": "Interface",
    "variable": "Variable", "constant": "Variable",
}
CODE_CLASSES = ["Function", "Class", "Method", "Module", "Interface", "Variable", "Code"]

# Memory ExpertiseEntry.type -> o: subclass of o:Concept (model-knowledge.ttl,
# task f5352fa1). An unmapped/blank type still gets the generic o:Concept.
_MEMORY_TYPE_CLASS = {
    "pattern": "Pattern", "convention": "Convention",
    "failure": "Failure", "decision": "Decision",
}


def _code_class_for_kind(kind: str) -> str:
    return _CODE_KIND_CLASS.get(str(kind).lower(), "Code")


def _iri(bucket: str, key: object) -> str:
    return f"{NS}instance/{bucket}/{quote(str(key), safe='')}"


def _ref_of(bucket: str, iri_value: str) -> str:
    prefix = f"{NS}instance/{bucket}/"
    return unquote(iri_value[len(prefix):]) if iri_value.startswith(prefix) else iri_value


# SPARQL query type sniff (comments/PREFIX/BASE stripped) — the /sparql API
# route rejects anything that isn't a plain SELECT or ASK.
_QUERY_TYPE_RE = re.compile(
    r"^\s*(?:(?:BASE\s*<[^>]*>|PREFIX\s+[A-Za-z_][\w.-]*:\s*<[^>]*>)\s*)*"
    r"(SELECT|ASK)\b", re.IGNORECASE,
)


def read_only_query_type(sparql: str) -> str | None:
    m = _QUERY_TYPE_RE.match(sparql or "")
    return m.group(1).upper() if m else None


def _ox_term_to_rdflib(term):
    """pyoxigraph term -> rdflib term — used by OntologyGraph.to_rdflib()
    (task 8eeb3e65) to hand the store's triples to owlrl/pyshacl, neither
    of which understands pyoxigraph's own term types."""
    import rdflib

    if isinstance(term, ox.NamedNode):
        return rdflib.URIRef(term.value)
    if isinstance(term, ox.BlankNode):
        return rdflib.BNode(term.value)
    if isinstance(term, ox.Literal):
        if term.language:
            return rdflib.Literal(term.value, lang=term.language)
        dt = term.datatype
        if dt is not None and dt.value != "http://www.w3.org/2001/XMLSchema#string":
            return rdflib.Literal(term.value, datatype=rdflib.URIRef(dt.value))
        return rdflib.Literal(term.value)
    raise TypeError(f"unexpected pyoxigraph term: {type(term)}")


# pyoxigraph.Store is an exclusive RocksDB lock on its directory — a second
# Store opened on the SAME path while the first is still alive raises
# "lock hold by current process". OntologyGraph is constructed fresh at
# call sites all over this file's callers (api/okf.py's GET route builds
# one, then calls ontology_prototype_projection.rebuild() which builds
# ANOTHER on the same project), so the Store itself is cached process-wide
# per on-disk path — every OntologyGraph(project) for the same project
# shares the one open handle, however many wrapper instances exist.
_STORES: dict[str, ox.Store] = {}


class OntologyGraph:
    """The pyoxigraph store for one project's ontology (TBox + ABox)."""

    def __init__(self, project: str) -> None:
        self.project = project
        self._model_iri = ox.NamedNode(NS + "model")
        self._abox_iri = ox.NamedNode(f"urn:prism:{project}/model")
        store_path = project_data_dir(project) / "ontology-graph"
        store_path.mkdir(parents=True, exist_ok=True)
        key = str(store_path)
        store = _STORES.get(key)
        if store is None:
            store = ox.Store(key)
            _STORES[key] = store
        self._store = store

    def load_model(self) -> int:
        """Parse model.ttl AND every ontology/model-*.ttl extension (task
        f5352fa1: model-knowledge.ttl's o:Concept/o:Domain layer) into the
        SAME shared TBox named graph, replacing it whole. Returns the
        triple count loaded — always called at the start of rebuild() so
        the TBox never drifts from the files on disk."""
        self._store.remove_graph(self._model_iri)
        ttl = _MODEL_TTL.read_text(encoding="utf-8")
        self._store.load(input=ttl, format=ox.RdfFormat.TURTLE,
                          base_iri=NS, to_graph=self._model_iri)
        for extra in sorted(_ONTOLOGY_DIR.glob("model-*.ttl")):
            extra_ttl = extra.read_text(encoding="utf-8")
            self._store.load(input=extra_ttl, format=ox.RdfFormat.TURTLE,
                              base_iri=NS, to_graph=self._model_iri)
        return sum(1 for _ in self._store.quads_for_pattern(
            None, None, None, self._model_iri))

    def is_empty(self) -> bool:
        q = f"ASK {{ GRAPH <{self._abox_iri.value}> {{ ?s ?p ?o }} }}"
        return not bool(self._store.query(q))

    def to_rdflib(self) -> "rdflib.Graph":
        """TBox (model.ttl, reloaded fresh) + this project's ABox,
        combined into one throwaway rdflib.Graph — the scratch graph
        services.ontology_rules.validate() runs owlrl + pyshacl over
        (task 8eeb3e65). Reads straight off the pyoxigraph store's two
        named graphs; never re-parses model.ttl a second way."""
        import rdflib

        self.load_model()
        g = rdflib.Graph()
        for gi in (self._model_iri, self._abox_iri):
            for s, p, o, _ in self._store.quads_for_pattern(None, None, None, gi):
                g.add((_ox_term_to_rdflib(s), _ox_term_to_rdflib(p),
                        _ox_term_to_rdflib(o)))
        return g

    def replace_graph(self, iri: str, nt: str) -> None:
        """Replace any named graph (by full IRI) atomically — remove_graph
        + bulk_load, the same discipline rebuild() uses for the ABox.
        Used by ontology_rules.py to persist the SHACL report without
        reaching into the store directly."""
        node = ox.NamedNode(iri)
        self._store.remove_graph(node)
        if nt.strip():
            self._store.bulk_load(input=nt, format=ox.RdfFormat.N_TRIPLES,
                                   to_graph=node)

    def read_graph(self, iri: str) -> list[tuple[str, str, str]]:
        """Every (s, p, o) triple in a named graph, as plain strings."""
        node = ox.NamedNode(iri)
        return [(s.value, p.value, o.value) for s, p, o, _ in
                self._store.quads_for_pattern(None, None, None, node)]

    def rebuild(self, rows: dict | None = None, *,
                agent_descriptions: dict[str, str] | None = None,
                signal_arrived_at: dict[str, str] | None = None) -> dict:
        """Build the ABox from the SAME real rows ontology_prototype_
        projection reads (gather(project), reused rather than re-queried
        when the caller already has it) and replace the named ABox graph
        atomically: remove_graph + bulk_load, never an incremental add —
        a second rebuild must not double the triple count. Returns
        {"total_triples", "per_class"} — triple counts per catalog class,
        read straight back off the store after the swap.

        agent_descriptions/signal_arrived_at (task 8eeb3e65's skill-
        description-* and flagged-signal-is-placed rules): real data
        gather()'s own rows don't carry, fetched independently by default
        (self._agent_descriptions/_signal_arrived_at) — never fabricated;
        callers (tests) may pass explicit dicts to control a fixture."""
        if rows is None:
            from prism_service.services import ontology_prototype_projection as proj
            rows = proj.gather(self.project)
        if agent_descriptions is None:
            agent_descriptions = self._agent_descriptions()
        if signal_arrived_at is None:
            signal_arrived_at = self._signal_arrived_at()

        import rdflib

        g = rdflib.Graph()
        RDF, RDFS = rdflib.RDF, rdflib.RDFS

        def U(bucket: str, key: object) -> "rdflib.URIRef":
            return rdflib.URIRef(_iri(bucket, key))

        def CLS(local: str) -> "rdflib.URIRef":
            return rdflib.URIRef(NS + local)

        self._emit_channels_agents_providers(g, rows, U, CLS, RDF, RDFS,
                                              agent_descriptions)
        self._emit_tasks(g, rows, U, CLS, RDF, RDFS)
        self._emit_signals(g, rows, U, CLS, RDF, RDFS, signal_arrived_at)
        self._emit_documents_folders(g, rows, U, CLS, RDF, RDFS)
        self._emit_code_graph(g, rows, U, CLS, RDF, RDFS)
        self._emit_workflow_steps(g, U, CLS, RDF, RDFS)
        self._emit_memories(g, rows, U, CLS, RDF, RDFS)

        nt = g.serialize(format="nt")
        self._store.remove_graph(self._abox_iri)
        self._store.bulk_load(input=nt, format=ox.RdfFormat.N_TRIPLES,
                               to_graph=self._abox_iri)
        self.load_model()

        per_class = {c["id"]: self._count(c["cls"]) for c in BASE_CATALOG}
        for cls_local in CODE_CLASSES:
            n = self._count(cls_local)
            if n:
                per_class[f"CodeGraph::{cls_local}"] = n

        # SHACL re-validation (task 8eeb3e65): every rebuild re-validates,
        # so the persisted report never trails the ABox it describes.
        from prism_service.services import ontology_rules
        ontology_rules.validate(self.project)

        return {"total_triples": len(g), "per_class": per_class}

    def _agent_descriptions(self) -> dict[str, str]:
        """Agent id -> description, from the SAME catalog_entries source
        the skill-description-* SHACL rules mean to check (the workflow/
        behavior catalog's real 'description' text) — never fabricated.
        Best effort: any failure -> {} (rules then read as no comment)."""
        try:
            from prism_service.services import ontology_prototype_projection as proj
            entries = proj.axiom_context(self.project).get("catalog_entries", [])
            return {e.get("id"): e.get("description", "")
                    for e in entries if e.get("id")}
        except Exception:
            return {}

    def _signal_arrived_at(self) -> dict[str, str]:
        """Signal id -> arrived_at ISO timestamp, off the real SignalStore
        (gather()'s own signal rows don't carry it) — used by flagged-
        signal-is-placed's 7-day-old check. Best effort: {} -> the rule
        falls back to 'any open signal', per its own spec."""
        try:
            from prism_service.services.signal_store import SignalStore
            store = SignalStore(self.project)
            try:
                return {s.id: s.arrived_at for s in store.list(limit=2000)}
            finally:
                store.close()
        except Exception:
            return {}

    @staticmethod
    def _emit_channels_agents_providers(g, rows, U, CLS, RDF, RDFS,
                                         agent_descriptions: dict[str, str]) -> None:
        import rdflib
        for name in rows["channels"]:
            u = U("channel", name)
            g.add((u, RDF.type, CLS("Channel")))
            g.add((u, RDFS.label, rdflib.Literal(name)))
        for aid in rows["agents"]:
            u = U("agent", aid)
            g.add((u, RDF.type, CLS("Agent")))
            g.add((u, RDFS.label, rdflib.Literal(aid)))
            desc = agent_descriptions.get(aid, "")
            if desc:
                g.add((u, RDFS.comment, rdflib.Literal(desc)))
        for name in rows["providers"]:
            u = U("provider", name)
            g.add((u, RDF.type, CLS("Provider")))
            g.add((u, RDFS.label, rdflib.Literal(name)))

    @staticmethod
    def _emit_tasks(g, rows, U, CLS, RDF, RDFS) -> None:
        """Task rows -> o:Task, with o:arrivedVia -> its o:Channel — the
        oracle's own SPARQL shape ('?task a o:Task ; o:arrivedVia ?channel').
        A channel not already emitted by _emit_channels_agents_providers
        (e.g. a project's ad-hoc channel string) is created here so no
        task's arrivedVia edge ever dangles."""
        import rdflib

        known = {str(c) for c in rows["channels"]}
        for t in rows["tasks"]:
            u = U("task", t["id"])
            g.add((u, RDF.type, CLS("Task")))
            g.add((u, RDFS.label, rdflib.Literal(t["title"])))
            channel = str(t.get("channel") or "").strip()
            if not channel:
                continue
            cu = U("channel", channel)
            if channel not in known:
                known.add(channel)
                g.add((cu, RDF.type, CLS("Channel")))
                g.add((cu, RDFS.label, rdflib.Literal(channel)))
            g.add((u, CLS("arrivedVia"), cu))

    @staticmethod
    def _emit_signals(g, rows, U, CLS, RDF, RDFS,
                       signal_arrived_at: dict[str, str]) -> None:
        """Signal rows -> o:Signal (rdfs:subClassOf o:QueueItem in model.ttl,
        task 785bb4ce: the Queue holds SIGNALS, not tasks), with
        o:arrivedVia -> its o:Channel, o:state as a literal, o:arrivedAt
        (task 8eeb3e65's flagged-signal-is-placed rule) when known, and
        o:becameTask -> the o:Task it turned into once the owner acted."""
        import rdflib

        for s in rows.get("signals", []):
            u = U("signal", s["id"])
            g.add((u, RDF.type, CLS("Signal")))
            g.add((u, RDFS.label, rdflib.Literal(s["label"])))
            g.add((u, CLS("state"), rdflib.Literal(s.get("state") or "open")))
            at = signal_arrived_at.get(s["id"])
            if at:
                g.add((u, CLS("arrivedAt"),
                       rdflib.Literal(at, datatype=rdflib.XSD.dateTime)))
            channel = str(s.get("channel") or "").strip()
            if channel:
                cu = U("channel", channel)
                g.add((cu, RDF.type, CLS("Channel")))
                g.add((cu, RDFS.label, rdflib.Literal(channel)))
                g.add((u, CLS("arrivedVia"), cu))
            if s.get("task_id"):
                g.add((u, CLS("becameTask"), U("task", s["task_id"])))

    @staticmethod
    def _emit_documents_folders(g, rows, U, CLS, RDF, RDFS) -> None:
        """Document -> o:inFolder -> Folder. A path with no folder
        component (Path(path).parent == '.') is loose in the root and
        gets NO inFolder edge — task 8eeb3e65's no-artifacts-in-the-root
        rule needs that absence to be real, never papered over by a
        fictitious '.' folder (mirrors document_tree.classify's own
        loose_in_root: a path with a single segment)."""
        import rdflib

        seen_folders: set[str] = set()
        for path in rows["documents"]:
            u = U("document", path)
            g.add((u, RDF.type, CLS("Document")))
            g.add((u, RDFS.label, rdflib.Literal(path)))
            parent = str(Path(path).parent)
            if parent in (".", ""):
                continue  # loose in the root — no inFolder edge, by design
            fu = U("folder", parent)
            if parent not in seen_folders:
                seen_folders.add(parent)
                g.add((fu, RDF.type, CLS("Folder")))
                g.add((fu, RDFS.label, rdflib.Literal(parent)))
            g.add((u, CLS("inFolder"), fu))

    @staticmethod
    def _emit_code_graph(g, rows, U, CLS, RDF, RDFS) -> None:
        import rdflib

        for kind, _count, sample in rows["code_kinds"]:
            cls_local = _code_class_for_kind(kind)
            for name in sample:
                u = U("code", f"{kind}/{name}")
                g.add((u, RDF.type, CLS(cls_local)))
                g.add((u, RDFS.label, rdflib.Literal(name)))

    @staticmethod
    def _emit_workflow_steps(g, U, CLS, RDF, RDFS) -> None:
        """Gate-step decidedBy/producedBy facts (task 8eeb3e65's
        agent-delegates-one-tier-down rule) — WORKFLOW_STEPS + STEP_ROLES
        (models/workflow.py, models/roles.py) are the real source of
        truth for who produces a step's work and who decides the gate
        right after it; never fabricated. Only gate steps carry both
        properties — an agent step has no 'decider' distinct from its own
        producer, so it is not this rule's concern."""
        import rdflib
        from prism_service.models.roles import role_for_step
        from prism_service.models.workflow import WORKFLOW_STEPS

        prev_id = None
        for step in WORKFLOW_STEPS:
            sid = step["id"]
            if step.get("type") == "gate" and prev_id is not None:
                u = U("step", sid)
                g.add((u, RDF.type, CLS("Step")))
                g.add((u, RDFS.label, rdflib.Literal(sid)))
                g.add((u, CLS("decidedBy"), rdflib.Literal(role_for_step(sid))))
                g.add((u, CLS("producedBy"), rdflib.Literal(role_for_step(prev_id))))
            prev_id = sid

    @staticmethod
    def _emit_memories(g, rows, U, CLS, RDF, RDFS) -> None:
        """Memory entries -> ontology triples (task f5352fa1, epic 3a652b3b:
        "the ontology is respected throughout the system"). Real rows from
        services.ontology_memory_projection.memory_rows, never fabricated:
        o:Concept subclassed per type (o:Pattern/o:Convention/o:Failure/
        o:Decision under o:Concept in model-knowledge.ttl -- an unmapped
        type still gets the generic o:Concept), o:inDomain -> o:Domain(name),
        o:cites -> another o:Concept (the entry's real [[wikilink]] cross-
        links, already resolved by okf_host -- a dangling link stays
        absent), o:evidencedBy -> o:Task / o:Document. IRI bucket 'memory',
        key = the entry id -- the same shape every other emitter here uses."""
        import rdflib

        for m in rows.get("memories", []):
            u = U("memory", m["id"])
            cls_local = _MEMORY_TYPE_CLASS.get(
                str(m.get("type") or "").strip().lower(), "Concept")
            g.add((u, RDF.type, CLS(cls_local)))
            g.add((u, RDFS.label, rdflib.Literal(m.get("name") or m["id"])))
            domain = str(m.get("domain") or "").strip()
            if domain:
                du = U("domain", domain)
                g.add((du, RDF.type, CLS("Domain")))
                g.add((du, RDFS.label, rdflib.Literal(domain)))
                g.add((u, CLS("inDomain"), du))
            for target_id in m.get("cites") or []:
                g.add((u, CLS("cites"), U("memory", target_id)))
            task_id = str(m.get("evidence_task") or "").strip()
            if task_id:
                g.add((u, CLS("evidencedBy"), U("task", task_id)))
            for path in m.get("evidence_files") or []:
                g.add((u, CLS("evidencedBy"), U("document", path)))

    def _count(self, cls_local: str) -> int:
        q = (f"PREFIX o: <{NS}> SELECT (COUNT(?i) AS ?n) "
             f"WHERE {{ GRAPH ?g {{ ?i a o:{cls_local} }} }}")
        for sol in self._store.query(q):
            return int(sol["n"].value)
        return 0

    def _parent_of(self, cls_local: str) -> str | None:
        q = (f"PREFIX o: <{NS}> PREFIX rdfs: <{_RDFS}> SELECT ?p WHERE "
             f"{{ GRAPH <{NS}model> {{ o:{cls_local} rdfs:subClassOf ?p }} }} LIMIT 1")
        for sol in self._store.query(q):
            p = sol["p"].value
            return p[len(NS):] if p.startswith(NS) else p
        return None

    def _comment_of(self, cls_local: str) -> str:
        q = (f"PREFIX o: <{NS}> PREFIX rdfs: <{_RDFS}> SELECT ?c WHERE "
             f"{{ GRAPH <{NS}model> {{ o:{cls_local} rdfs:comment ?c }} }} LIMIT 1")
        for sol in self._store.query(q):
            return sol["c"].value
        return ""

    def label_of(self, iri_value: str) -> str:
        """rdfs:label of any IRI (instance or class), queried over BOTH
        graphs as a union — falls back to the IRI's own local segment when
        no label triple exists. Used to turn a bare focus-node IRI (the
        SHACL report) or a relation's endpoint into something a human can
        read (task 7dbb242f)."""
        q = (f"PREFIX rdfs: <{_RDFS}> SELECT ?l WHERE "
             f"{{ GRAPH ?g {{ <{iri_value}> rdfs:label ?l }} }} LIMIT 1")
        for sol in self._store.query(q):
            return sol["l"].value
        return iri_value.rsplit("/", 1)[-1]

    def class_of(self, iri_value: str) -> str:
        """rdf:type's local name for one IRI (task f5352fa1) -- the cheap
        per-hit `ontology_class` lookup memory_recall/brain_search attach to
        a result. Empty string when the IRI has no ABox rdf:type triple yet
        (the graph hasn't been rebuilt since this row was written) -- never
        guessed from the row's own fields."""
        q = f"SELECT ?c WHERE {{ GRAPH ?g {{ <{iri_value}> a ?c }} }} LIMIT 1"
        for sol in self._store.query(q):
            c = sol["c"].value
            return c[len(NS):] if c.startswith(NS) else c
        return ""

    def classes(self) -> list[dict]:
        """The prototype's classes() shape (id/name/kind/parent_id/
        description/instance_count/source) — api/okf.py's GET /ontology
        response, answered from the graph (SPARQL COUNT), never sqlite."""
        out = []
        for c in BASE_CATALOG:
            out.append({
                "id": c["id"], "name": c["cls"], "kind": "class",
                "parent_id": self._parent_of(c["cls"]),
                "description": self._comment_of(c["cls"]),
                "instance_count": self._count(c["cls"]), "source": c["source"],
            })
        for cls_local in CODE_CLASSES:
            n = self._count(cls_local)
            if n == 0:
                continue
            out.append({
                "id": f"CodeGraph::{cls_local}", "name": cls_local, "kind": "class",
                "parent_id": self._parent_of(cls_local),
                "description": self._comment_of(cls_local),
                "instance_count": n, "source": "graph",
            })
        out.sort(key=lambda c: (c["source"], c["name"]))
        return out

    def instances(self, class_id: str, limit: int = 200) -> list[dict]:
        """The prototype's instances() shape (id/class_id/label/ref/
        provenance) for one class_id — a base catalog id (Channel, Agent,
        Provider, Task, Document, Folder) or a CodeGraph::<Class> id."""
        entry = _CATALOG_BY_ID.get(class_id)
        if entry is not None:
            cls_local, source, bucket = entry["cls"], entry["source"], entry["bucket"]
        elif class_id.startswith("CodeGraph::"):
            cls_local = class_id.split("::", 1)[1]
            source, bucket = "graph", "code"
        else:
            return []

        q = (f"PREFIX o: <{NS}> PREFIX rdfs: <{_RDFS}> "
             f"SELECT ?i ?label WHERE {{ GRAPH ?g {{ ?i a o:{cls_local} . "
             f"OPTIONAL {{ ?i rdfs:label ?label }} }} }} "
             f"ORDER BY ?label LIMIT {int(limit)}")
        out = []
        for i, sol in enumerate(self._store.query(q)):
            iri = sol["i"].value
            label_term = sol["label"]
            label = label_term.value if label_term is not None else iri
            # ref == label everywhere except Task, whose natural key (the
            # task id) is a real identifier distinct from its title label —
            # the id is the only piece round-tripped through the IRI.
            ref = _ref_of(bucket, iri) if bucket == "task" else label
            out.append({
                "id": f"{class_id}::{i}", "class_id": class_id, "label": label,
                "ref": ref, "provenance": source,
            })
        return out

    def properties(self) -> list[dict]:
        """rdf:Property declarations from the TBox (model.ttl) — includes
        the code-graph edge kinds (calls, imports, ...) statically, not
        discovered per-project from graph.db, since model.ttl already
        declares them as o:relatesTo sub-properties."""
        q = (f"PREFIX o: <{NS}> PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
             f"PREFIX rdfs: <{_RDFS}> SELECT ?p ?domain ?range WHERE "
             f"{{ GRAPH <{NS}model> {{ ?p a rdf:Property . "
             f"OPTIONAL {{ ?p rdfs:domain ?domain }} "
             f"OPTIONAL {{ ?p rdfs:range ?range }} }} }}")
        out = []
        for sol in self._store.query(q):
            p = sol["p"].value
            name = p[len(NS):] if p.startswith(NS) else p
            dom, rng = sol["domain"], sol["range"]
            out.append({
                "id": f"prop::{name}", "name": name,
                "domain_class": (dom.value[len(NS):] if dom is not None else None),
                "range_class": (rng.value[len(NS):] if rng is not None else None),
                "kind": "property",
            })
        out.sort(key=lambda p: p["name"])
        return out

    def axioms(self) -> list[dict]:
        """The rules are SHACL shapes that can fail (task 8eeb3e65) —
        reads the persisted validation report (shapes.ttl over the o:
        model), replacing the arc_governance.PROTOTYPE_AXIOMS bridge this
        docstring used to describe. A fast read: rebuild() already called
        validate() at the end of the last ABox swap, so this never
        re-validates on a request path."""
        from prism_service.services import ontology_rules

        return [{"id": f"axiom::{a['name']}", "name": a["name"],
                 "description": a["description"], "state": a["state"],
                 "looked_at": a["looked_at"], "violations": a["violations"],
                 "detail": a["detail"]}
                for a in ontology_rules.evaluate(self.project)]

    def _class_meta(self) -> dict[str, dict]:
        """label/comment/parent for every rdfs:Class in the TBox — one row
        per class, local name keyed (task 7dbb242f's structure())."""
        q = (f"PREFIX rdfs: <{_RDFS}> SELECT ?c ?label ?comment ?parent WHERE "
             f"{{ GRAPH <{NS}model> {{ ?c a rdfs:Class . "
             f"OPTIONAL {{ ?c rdfs:label ?label }} "
             f"OPTIONAL {{ ?c rdfs:comment ?comment }} "
             f"OPTIONAL {{ ?c rdfs:subClassOf ?parent }} }} }}")
        out: dict[str, dict] = {}
        for sol in self._store.query(q):
            c = sol["c"].value
            local = c[len(NS):] if c.startswith(NS) else c
            meta = out.setdefault(local, {"label": local, "comment": "", "parent": None})
            if sol["label"] is not None:
                meta["label"] = sol["label"].value
            if sol["comment"] is not None:
                meta["comment"] = sol["comment"].value
            if sol["parent"] is not None:
                p = sol["parent"].value
                meta["parent"] = p[len(NS):] if p.startswith(NS) else p
        return out

    def relations(self) -> list[dict]:
        """Every rdf:Property in the TBox with domain/range/comment, the
        count of ABox edges using it, and one real example edge (task
        7dbb242f) — sorted by count desc."""
        q = (f"PREFIX o: <{NS}> "
             f"PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> "
             f"PREFIX rdfs: <{_RDFS}> SELECT ?p ?domain ?range ?label ?comment WHERE "
             f"{{ GRAPH <{NS}model> {{ ?p a rdf:Property . "
             f"OPTIONAL {{ ?p rdfs:domain ?domain }} "
             f"OPTIONAL {{ ?p rdfs:range ?range }} "
             f"OPTIONAL {{ ?p rdfs:label ?label }} "
             f"OPTIONAL {{ ?p rdfs:comment ?comment }} }} }}")
        out = []
        for sol in self._store.query(q):
            p_iri = sol["p"].value
            name = p_iri[len(NS):] if p_iri.startswith(NS) else p_iri
            dom, rng = sol["domain"], sol["range"]
            label, comment = sol["label"], sol["comment"]
            n = 0
            count_q = f"SELECT (COUNT(*) AS ?n) WHERE {{ GRAPH ?g {{ ?s <{p_iri}> ?o }} }}"
            for csol in self._store.query(count_q):
                n = int(csol["n"].value)
            example = None
            if n:
                ex_q = f"SELECT ?s ?o WHERE {{ GRAPH ?g {{ ?s <{p_iri}> ?o }} }} LIMIT 1"
                for esol in self._store.query(ex_q):
                    o_term = esol["o"]
                    to_label = (o_term.value if isinstance(o_term, ox.Literal)
                                else self.label_of(o_term.value))
                    example = {"from_label": self.label_of(esol["s"].value),
                               "to_label": to_label}
            out.append({
                "property": name, "label": label.value if label is not None else name,
                "comment": comment.value if comment is not None else "",
                "domain": (dom.value[len(NS):] if dom is not None and dom.value.startswith(NS)
                           else (dom.value if dom is not None else None)),
                "range": (rng.value[len(NS):] if rng is not None and rng.value.startswith(NS)
                          else (rng.value if rng is not None else None)),
                "count": n, "example": example,
            })
        out.sort(key=lambda r: -r["count"])
        return out

    def structure(self) -> dict:
        """GET /api/okf/ontology/structure (task 7dbb242f) — the taxonomy
        in pre-order from the TBox's rdfs:subClassOf edges (root
        o:Workspace; any class with no declared parent hangs under it
        directly), each with own_count (direct rdf:type instances) and
        count rolled up through its subclasses."""
        from prism_service.services import ontology_rules

        meta = self._class_meta()
        children: dict[str, list[str]] = defaultdict(list)
        for local, m in meta.items():
            if local == "Workspace":
                continue
            children[m["parent"] or "Workspace"].append(local)

        own_counts = {local: self._count(local) for local in meta}
        totals: dict[str, int] = {}

        def total(local: str) -> int:
            if local not in totals:
                totals[local] = own_counts.get(local, 0) + sum(
                    total(child) for child in children.get(local, ()))
            return totals[local]

        for local in meta:
            total(local)

        out: list[dict] = []

        def visit(local: str, depth: int, parent_id: str | None) -> None:
            m = meta[local]
            out.append({
                "id": local, "name": m["label"], "parent": parent_id,
                "comment": m["comment"], "depth": depth,
                "count": totals.get(local, 0), "own_count": own_counts.get(local, 0),
                "abstract": own_counts.get(local, 0) == 0 and bool(children.get(local)),
            })
            for child in sorted(children.get(local, ()), key=lambda c: meta[c]["label"]):
                visit(child, depth + 1, local)

        visit("Workspace", 0, None)

        return {
            "classes": out, "relations": self.relations(),
            "built_from": {"signals": self._count("Signal"), "tasks": self._count("Task")},
            "validated_at": ontology_rules.last_validated_at(self.project),
        }

    def records(self) -> dict:
        """GET /api/okf/ontology/records (task 7dbb242f) — things (distinct
        typed subjects), connections (edges to another typed instance),
        values (literal-object triples on a typed subject), and per-class
        count + up to 6 sample labels — one pass over the ABox graph."""
        RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
        LABEL = f"{_RDFS}label"
        triples = list(self._store.quads_for_pattern(None, None, None, self._abox_iri))

        typed: set[str] = set()
        subject_types: dict[str, list[str]] = defaultdict(list)
        labels: dict[str, str] = {}
        for s, p, o, _ in triples:
            if p.value == RDF_TYPE:
                typed.add(s.value)
                subject_types[s.value].append(o.value)
            elif p.value == LABEL:
                labels[s.value] = o.value

        connections = values = 0
        for s, p, o, _ in triples:
            if p.value == RDF_TYPE or s.value not in typed:
                continue
            if isinstance(o, ox.Literal):
                values += 1
            elif o.value in typed:
                connections += 1

        class_counts: Counter = Counter()
        class_samples: dict[str, list[str]] = defaultdict(list)
        for subj, types in subject_types.items():
            label = labels.get(subj, subj)
            for t in types:
                local = t[len(NS):] if t.startswith(NS) else t
                class_counts[local] += 1
                if len(class_samples[local]) < 6:
                    class_samples[local].append(label)

        classes = [{"id": local, "name": local, "count": n, "sample": class_samples[local]}
                   for local, n in class_counts.items()]
        classes.sort(key=lambda c: -c["count"])

        return {"things": len(typed), "connections": connections, "values": values,
                "classes": classes}

    def concept_info(self, iri_value: str) -> dict:
        """GET /api/okf/ontology/concept (task f5352fa1) -- the Understand
        'In the ontology' strip: this concept's o: class, its o:inDomain
        label, and its o:cites / o:evidencedBy relations, all read straight
        off the graph. Every field is '' / [] when the concept has no ABox
        triples yet (the graph hasn't been rebuilt) -- never fabricated."""
        cls_local = self.class_of(iri_value)

        domain = ""
        q_domain = (f"PREFIX o: <{NS}> PREFIX rdfs: <{_RDFS}> SELECT ?d ?label WHERE "
                    f"{{ GRAPH ?g {{ <{iri_value}> o:inDomain ?d . "
                    f"OPTIONAL {{ ?d rdfs:label ?label }} }} }} LIMIT 1")
        for sol in self._store.query(q_domain):
            label = sol["label"]
            domain = label.value if label is not None else self.label_of(sol["d"].value)

        cites = []
        q_cites = (f"PREFIX o: <{NS}> SELECT ?t WHERE "
                   f"{{ GRAPH ?g {{ <{iri_value}> o:cites ?t }} }}")
        for sol in self._store.query(q_cites):
            t = sol["t"].value
            cites.append({"id": _ref_of("memory", t), "label": self.label_of(t)})

        tasks, documents = [], []
        q_ev = (f"PREFIX o: <{NS}> SELECT ?e WHERE "
                f"{{ GRAPH ?g {{ <{iri_value}> o:evidencedBy ?e }} }}")
        for sol in self._store.query(q_ev):
            e = sol["e"].value
            if e.startswith(f"{NS}instance/task/"):
                tasks.append({"id": _ref_of("task", e), "label": self.label_of(e)})
            elif e.startswith(f"{NS}instance/document/"):
                documents.append({"id": _ref_of("document", e), "label": self.label_of(e)})

        return {
            "class": cls_local, "domain": domain, "cites": cites,
            "evidenced_by_tasks": tasks, "evidenced_by_documents": documents,
        }

    def query(self, sparql: str, limit: int = 500) -> dict:
        """SELECT/ASK only (400 on anything else, at the caller). Named
        graphs are queried as a union by default so a query with no
        explicit GRAPH clause still sees this project's ABox + the shared
        TBox — the oracle's own example uses an explicit GRAPH ?g anyway."""
        qtype = read_only_query_type(sparql)
        if qtype is None:
            raise ValueError("only SELECT and ASK SPARQL queries are supported")
        result = self._store.query(sparql, use_default_graph_as_union=True)
        if qtype == "ASK":
            return {"columns": ["ask"], "bindings": [{"ask": str(bool(result))}],
                    "truncated": False}
        columns = [v.value for v in result.variables]
        bindings, truncated = [], False
        for i, sol in enumerate(result):
            if i >= limit:
                truncated = True
                break
            row = {}
            for col in columns:
                term = sol[col]
                row[col] = term.value if term is not None else None
            bindings.append(row)
        return {"columns": columns, "bindings": bindings, "truncated": truncated}
