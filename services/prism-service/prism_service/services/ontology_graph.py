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
from pathlib import Path
from urllib.parse import quote, unquote

import pyoxigraph as ox

from prism_service.config import project_data_dir

NS = "urn:prism:onto:"
_RDFS = "http://www.w3.org/2000/01/rdf-schema#"
_MODEL_TTL = Path(__file__).resolve().parent.parent / "ontology" / "model.ttl"

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
        """Parse model.ttl and replace the shared TBox named graph. Returns
        the triple count loaded — always called at the start of rebuild()
        so the TBox never drifts from the file on disk."""
        self._store.remove_graph(self._model_iri)
        ttl = _MODEL_TTL.read_text(encoding="utf-8")
        self._store.load(input=ttl, format=ox.RdfFormat.TURTLE,
                          base_iri=NS, to_graph=self._model_iri)
        return sum(1 for _ in self._store.quads_for_pattern(
            None, None, None, self._model_iri))

    def is_empty(self) -> bool:
        q = f"ASK {{ GRAPH <{self._abox_iri.value}> {{ ?s ?p ?o }} }}"
        return not bool(self._store.query(q))

    def rebuild(self, rows: dict | None = None) -> dict:
        """Build the ABox from the SAME real rows ontology_prototype_
        projection reads (gather(project), reused rather than re-queried
        when the caller already has it) and replace the named ABox graph
        atomically: remove_graph + bulk_load, never an incremental add —
        a second rebuild must not double the triple count. Returns
        {"total_triples", "per_class"} — triple counts per catalog class,
        read straight back off the store after the swap."""
        if rows is None:
            from prism_service.services import ontology_prototype_projection as proj
            rows = proj.gather(self.project)

        import rdflib

        g = rdflib.Graph()
        RDF, RDFS = rdflib.RDF, rdflib.RDFS

        def U(bucket: str, key: object) -> "rdflib.URIRef":
            return rdflib.URIRef(_iri(bucket, key))

        def CLS(local: str) -> "rdflib.URIRef":
            return rdflib.URIRef(NS + local)

        self._emit_channels_agents_providers(g, rows, U, CLS, RDF, RDFS)
        self._emit_tasks(g, rows, U, CLS, RDF, RDFS)
        self._emit_signals(g, rows, U, CLS, RDF, RDFS)
        self._emit_documents_folders(g, rows, U, CLS, RDF, RDFS)
        self._emit_code_graph(g, rows, U, CLS, RDF, RDFS)

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
        return {"total_triples": len(g), "per_class": per_class}

    @staticmethod
    def _emit_channels_agents_providers(g, rows, U, CLS, RDF, RDFS) -> None:
        import rdflib
        for name in rows["channels"]:
            u = U("channel", name)
            g.add((u, RDF.type, CLS("Channel")))
            g.add((u, RDFS.label, rdflib.Literal(name)))
        for aid in rows["agents"]:
            u = U("agent", aid)
            g.add((u, RDF.type, CLS("Agent")))
            g.add((u, RDFS.label, rdflib.Literal(aid)))
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
    def _emit_signals(g, rows, U, CLS, RDF, RDFS) -> None:
        """Signal rows -> o:Signal (rdfs:subClassOf o:QueueItem in model.ttl,
        task 785bb4ce: the Queue holds SIGNALS, not tasks), with
        o:arrivedVia -> its o:Channel, o:state as a literal, and
        o:becameTask -> the o:Task it turned into once the owner acted."""
        import rdflib

        for s in rows.get("signals", []):
            u = U("signal", s["id"])
            g.add((u, RDF.type, CLS("Signal")))
            g.add((u, RDFS.label, rdflib.Literal(s["label"])))
            g.add((u, CLS("state"), rdflib.Literal(s.get("state") or "open")))
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
        import rdflib

        seen_folders: set[str] = set()
        for path in rows["documents"]:
            u = U("document", path)
            g.add((u, RDF.type, CLS("Document")))
            g.add((u, RDFS.label, rdflib.Literal(path)))
            parent = str(Path(path).parent)
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
        """Placeholder pending the SHACL slice (8eeb3e65 fills this from
        the graph itself + member-shapes.ttl) — for now bridged to the same
        arc_governance.evaluate_axioms(context) real-row evaluation
        ontology_prototype_projection's sqlite cache already uses, so the
        Understand axioms rail is never empty in this slice. Called fresh
        (not cached), so it always reflects the project's CURRENT rows."""
        from prism_service.services import ontology_prototype_projection as proj
        from prism_service.services.arc_governance import evaluate_axioms

        out = []
        for name in proj.axiom_names(self.project):
            out.append({"id": f"axiom::{name}", "name": name,
                         "description": "", "state": "quiet", "detail": ""})
        for a in evaluate_axioms(proj.axiom_context(self.project)):
            out.append({"id": f"axiom::{a['name']}", "name": a["name"],
                         "description": a["description"], "state": a["state"],
                         "detail": a["detail"]})
        return out

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
