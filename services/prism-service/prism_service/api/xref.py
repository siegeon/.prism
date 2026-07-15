"""xref resolver API -- deterministic token -> destination, no LLM/network.

GET /api/xref/resolve?token=<s>&project=<p> resolves a free token through a
fixed ladder against the SAME per-project services the rest of api/ uses
(memory_svc, brain_svc, graph_svc). Pure read-through; never writes.

Resolve ladder, in order (the 8 RESOLVABLE_KINDS):
  1. OKF/memory id-or-name match  -> kind="concept"   -> /understand?concept=<id>
  2. else brain find_symbol hit   -> kind="symbol"    -> /artifact?focus=<file>&symbol=<name>
  3. else indexed-file path match -> kind="file"      -> /artifact?focus=<file>
  4. else task id match           -> kind="task"      -> /tasks/<id>
  5. else session id match        -> kind="session"   -> /sessions/<id>
  6. else <task_id>#gate ref      -> kind="gate"      -> /tasks/<id>#gate
  7. else test node (none yet)    -> kind="test"      -> (reserved)
  8. else retrieval:<id> ref      -> kind="retrieval" -> /retrievals?focus=<id>
  9. else                         -> kind="unresolved"

`summary` is populated from an existing graph_annotations row for the resolved
file WHEN available; it is NEVER fabricated (omitted when absent). `stale` is
not derivable without re-deriving the live input_hash, so it is left absent
for now (both are optional in the frozen contract).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import PurePosixPath
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project

router = APIRouter()

# The published contract of what the ladder can resolve — the SPA chip renderer
# and the gate rubric key off this set. Every kind here has a real rung below
# (a rung may legitimately return None today when its entity isn't indexed yet,
# but the KIND is declared so downstream code can render/route it now).
RESOLVABLE_KINDS = frozenset({
    "concept", "symbol", "file",
    "task", "session", "gate", "test", "retrieval",
})

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    s = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return s or "untitled"


def _resolve_concept(memory_svc, token: str) -> Optional[dict]:
    """Step 1 -- match a memory entry by id, else by (slugged) name."""
    try:
        entry = memory_svc.get_entry(token)
    except Exception:
        entry = None
    if entry is not None:
        return {"id": entry.id, "label": entry.name or entry.id}
    target = token.strip()
    tslug = _slug(target)
    try:
        for domain in memory_svc.list_domains():
            for e in memory_svc.list_entries(domain):
                if e.name and (e.name == target or _slug(e.name) == tslug):
                    return {"id": e.id, "label": e.name}
    except Exception:
        pass
    return None


_FILE_EXTS = {"py", "ts", "tsx", "js", "jsx", "md", "json",
              "txt", "yaml", "yml", "toml", "css", "html"}


def _strip_call(token: str) -> str:
    """Drop a trailing call segment so 'f()' -> 'f', 'a.b(x, y)' -> 'a.b'."""
    return token.split("(", 1)[0].strip()


def _resolve_symbol(brain_svc, token: str) -> Optional[dict]:
    """Step 2 -- match a code symbol, tolerating the way people actually
    write them: call parens ('f()') and a module/class qualifier
    ('conductor_service._median_step_s'). Dotted tokens match on the final
    segment; when a qualifier is present we prefer the candidate whose source
    file stem equals it (disambiguates a name shared across files). Path-like
    or bare file-extension tokens are left for the file rung."""
    bare = _strip_call(token)
    if not bare or "/" in bare or "\\" in bare:
        return None
    leaf = bare.rsplit(".", 1)[-1]
    if not leaf or leaf.lower() in _FILE_EXTS:
        return None
    qualifier = ""
    if "." in bare:
        qualifier = bare.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    try:
        rows = brain_svc.find_symbol(leaf, limit=10) or []
    except Exception:
        rows = []
    if not rows:
        return None
    chosen = rows[0]
    if qualifier:
        for r in rows:
            stem = PurePosixPath((r.get("source_file") or "").replace("\\", "/")).stem
            if stem == qualifier or stem.endswith(qualifier):
                chosen = r
                break
    return {"file": chosen.get("source_file") or "",
            "name": chosen.get("entity_name") or leaf}


def _resolve_file(brain_svc, token: str) -> Optional[str]:
    """Step 3 -- token resolves to an indexed source file (code OR doc),
    exactly or via a healed trailing-suffix match, and we return the REAL
    indexed path so a stale-prefix citation still links to where the file
    lives today. A trailing ``:line`` / ``:line-range`` (the ubiquitous
    ``path:NN`` citation form) is stripped before matching."""
    clean = re.sub(r":\d+(?:-\d+)?$", "", token)
    try:
        return brain_svc.resolve_indexed_file(clean)
    except Exception:
        return None


def _annotation_for(graph_svc, file: str) -> Optional[dict]:
    """Existing hierarchy annotation for a file, or None. Never fabricated."""
    if graph_svc is None or not file:
        return None
    try:
        return graph_svc.get_annotation("hierarchy", file, "name")
    except Exception:
        return None


def _attach_summary(res: dict, graph_svc, file: str) -> None:
    ann = _annotation_for(graph_svc, file)
    if ann and ann.get("purpose"):
        res["summary"] = ann["purpose"]


def _task_lookup(task_svc, task_id: str):
    """Fetch a task through whichever accessor the service exposes: the real
    TaskService has .get() (see conductor_flow.py), test fakes carry
    .get_task(). Returns the raw task (dict or model) or None."""
    fn = getattr(task_svc, "get_task", None) or getattr(task_svc, "get", None)
    if fn is None:
        return None
    try:
        return fn(task_id)
    except Exception:
        return None


def _task_title(task) -> Optional[str]:
    if isinstance(task, dict):
        return task.get("title")
    return getattr(task, "title", None)


def _resolve_task(task_svc, token: str) -> Optional[dict]:
    """Step 4 -- token is a PRISM task id. Resolves via the task service so a
    task uuid dropped in any rendered doc links to its /tasks/{id} detail
    page. Guarded: a project without a task_svc simply skips this rung."""
    if task_svc is None:
        return None
    task = _task_lookup(task_svc, token)
    if not task:
        return None
    return {"id": token, "label": _task_title(task) or token}


def _resolve_session(conductor_svc, token: str) -> Optional[dict]:
    """Step 5 -- token is a Claude session id. Matches against the recent
    session_outcomes ids the conductor already tracks so a session uuid links
    to its /sessions/{id} detail page. Guarded on a missing conductor_svc."""
    if conductor_svc is None:
        return None
    try:
        rows = conductor_svc.get_session_outcomes(limit=500) or []
    except Exception:
        return None
    for r in rows:
        sid = r.get("session_id") if isinstance(r, dict) else None
        if sid and sid == token:
            return {"id": token, "label": token}
    return None


def _resolve_gate(task_svc, token: str) -> Optional[dict]:
    """Step 6 -- a gate reference shaped ``<task_id>#gate`` (optionally
    ``#gate:<name>``). Resolves the owning task via the task service and
    anchors the link at that task's gate card (/tasks/{id}#gate). Returns
    None for non-gate-shaped tokens or an unknown task."""
    if task_svc is None or "#gate" not in token:
        return None
    task_id = token.split("#", 1)[0].strip()
    if not task_id:
        return None
    task = _task_lookup(task_svc, task_id)
    if not task:
        return None
    title = _task_title(task)
    return {"id": task_id, "label": f"{title or task_id} · gate"}


def _resolve_test(brain_svc, token: str) -> Optional[dict]:
    """Step 7 -- reserved for a test entity (a test node keyed by its pytest
    nodeid, e.g. ``tests/unit/foo.py::test_bar``). No test entity is indexed
    yet, so this rung returns None today; the KIND is declared so the SPA
    renderer and gate rubric can key off it now, and this becomes the single
    place to wire the lookup once a test node lands in the graph."""
    return None


def _resolve_retrieval(brain_svc, token: str) -> Optional[dict]:
    """Step 8 -- a search-log reference shaped ``retrieval:<id>`` /
    ``search:<id>`` (the row id from the brain searches table that
    /api/retrievals serves). Resolves to the RetrievalsPage focused on that
    row. Guarded: returns None when the brain exposes no search log or the
    token isn't retrieval-shaped."""
    m = re.match(r"^(?:retrieval|search):(\d+)$", token)
    if not m:
        return None
    rid = m.group(1)
    getter = getattr(brain_svc, "get_recent_searches", None)
    if getter is None:
        return None
    try:
        rows = getter(limit=200) or []
    except Exception:
        return None
    for r in rows:
        if isinstance(r, dict) and str(r.get("id")) == rid:
            return {"id": rid, "label": r.get("query") or f"retrieval {rid}"}
    return None


def resolve_token(
    token: str, memory_svc, brain_svc, graph_svc=None,
    task_svc=None, conductor_svc=None,
) -> dict:
    """Run the deterministic ladder. Pure -- safe to unit-test with fakes.

    The ladder walks the 8 RESOLVABLE_KINDS in order: concept, symbol, file
    (the original three), then task, session, gate, test, retrieval. Each rung
    takes only the service handle it needs and is getattr/None-guarded so a
    project missing a given service just skips that rung instead of crashing."""
    token = (token or "").strip()
    if not token:
        return {"kind": "unresolved", "label": token, "href": None}

    concept = _resolve_concept(memory_svc, token)
    if concept:
        return {"kind": "concept", "label": concept["label"],
                "href": f"/understand?concept={concept['id']}"}

    sym = _resolve_symbol(brain_svc, token)
    if sym:
        # Code tokens land on the unified /artifact surface (title + summary +
        # grouped subgraph), which itself drills onward into /brain?focus=&symbol=
        # (the S4 deep-link). This is the AC-8 flow -- the surface first, the raw
        # Sigma graph one click deeper -- not a dump into the 17k-edge canvas.
        res = {"kind": "symbol", "label": sym["name"],
               "href": f"/artifact?focus={sym['file']}&symbol={sym['name']}"}
        _attach_summary(res, graph_svc, sym["file"])
        return res

    f = _resolve_file(brain_svc, token)
    if f:
        res = {"kind": "file", "label": f, "href": f"/artifact?focus={f}"}
        _attach_summary(res, graph_svc, f)
        return res

    task = _resolve_task(task_svc, token)
    if task:
        return {"kind": "task", "label": task["label"],
                "href": f"/tasks/{task['id']}"}

    session = _resolve_session(conductor_svc, token)
    if session:
        return {"kind": "session", "label": session["label"],
                "href": f"/sessions/{session['id']}"}

    gate = _resolve_gate(task_svc, token)
    if gate:
        return {"kind": "gate", "label": gate["label"],
                "href": f"/tasks/{gate['id']}#gate"}

    test = _resolve_test(brain_svc, token)
    if test:
        return {"kind": "test", "label": test["label"],
                "href": test.get("href")}

    retrieval = _resolve_retrieval(brain_svc, token)
    if retrieval:
        return {"kind": "retrieval", "label": retrieval["label"],
                "href": f"/retrievals?focus={retrieval['id']}"}

    return {"kind": "unresolved", "label": token, "href": None}


@router.get("/resolve")
def resolve(token: str = Query(...), project: str = Query("prism")) -> dict:
    """Resolve one token to {kind, label, href, summary?, stale?}."""
    try:
        p = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return resolve_token(
        token, p.memory_svc, p.brain_svc, p.graph_svc,
        task_svc=getattr(p, "task_svc", None),
        conductor_svc=getattr(p, "conductor_svc", None),
    )


# ---------------------------------------------------------------------------
# Neighbors -- the typed ego network behind the Explore mesh (workstream 4).
#
# Given ANY entity token, return its 1-hop typed neighborhood so the SPA can
# draw a freeform mesh: memories link memories, code calls code, sessions touch
# gates, tasks thread into all of it. The center is a doorway, NEVER a hub --
# every neighbor carries its own re-center `token` so a click re-runs this for
# it. Every edge source is independently guarded: a project missing a service
# or table just omits those edges instead of failing the whole neighborhood.
# ---------------------------------------------------------------------------

# The resolve ladder's 8 kinds collapse onto the 6 mesh node SHAPES the SPA
# draws (square=code, diamond=memory, rounded=task, triangle=test, hexagon=
# gate, circle=session). concept->memory, symbol/file->code; the rest map 1:1.
_UI_KIND = {
    "concept": "memory", "symbol": "code", "file": "code",
    "task": "task", "session": "session", "gate": "gate",
    "test": "test", "retrieval": "retrieval",
}


def _field(obj, name, default=None):
    """Read a field off a task that may be a dataclass OR a dict (test fakes)."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _short(token: str, n: int = 8) -> str:
    """A uuid-ish token shortened for a node label; left intact when short."""
    t = str(token or "")
    return (t[:n] + "…") if len(t) > n + 1 else t


def _nb(kind: str, label: str, href: Optional[str], edge: str,
        token: str, *, domain: Optional[str] = None,
        concept_type: Optional[str] = None) -> dict:
    """One neighbor: its shape (kind), display label, click-through href, the
    relation label drawn on the edge, and the token to re-center on. Memory
    neighbors additionally carry their OKF `domain` + `concept_type` so the
    mesh can sub-caption each diamond and group same-domain diamonds under a
    dashed hull; both are omitted for kinds that don't have them."""
    nb = {"kind": kind, "label": label, "href": href,
          "edge": edge, "token": token}
    if domain:
        nb["domain"] = domain
    if concept_type:
        nb["concept_type"] = concept_type
    return nb


def _task_neighbors(p, task_id: str, task, out: list) -> None:
    """Task threads: linked sessions, parent/children epics, dependencies, and
    the task's own gate. Never a hub -- these are peers reachable in one hop."""
    task_svc = getattr(p, "task_svc", None)
    sids: list = []
    if task_svc is not None:
        try:
            for s in (task_svc.sessions_for_task(task_id) or []):
                sid = s.get("session_id") if isinstance(s, dict) else None
                if sid:
                    sids.append(sid)
                    tok = int(s.get("tokens_used") or 0)
                    lbl = _short(sid) + (f" · {tok // 1000}k" if tok else "")
                    out.append(_nb("session", lbl, f"/sessions/{sid}",
                                   "driven in", sid))
        except Exception:
            pass
        parent = (_field(task, "parent_id", "") or "").strip()
        if parent:
            pt = _task_lookup(task_svc, parent)
            if pt:
                out.append(_nb("task", _task_title(pt) or parent,
                               f"/tasks/{parent}", "child of", parent))
        try:
            for c in (task_svc.list(parent_id=task_id) or []):
                cid = _field(c, "id", "")
                if cid:
                    out.append(_nb("task", _task_title(c) or cid,
                                   f"/tasks/{cid}", "parent of", cid))
        except Exception:
            pass
    for dep in (_field(task, "dependencies", []) or []):
        dep = str(dep).strip()
        if not dep:
            continue
        dt = _task_lookup(task_svc, dep) if task_svc is not None else None
        out.append(_nb("task", (_task_title(dt) if dt else dep) or dep,
                       f"/tasks/{dep}", "depends on", dep))
    step = (_field(task, "workflow_step", "") or "").strip()
    gate_state = (_field(task, "gate_state", "none") or "none").strip()
    if step and gate_state and gate_state != "none":
        out.append(_nb("gate", f"{step} · {gate_state}",
                       f"/tasks/{task_id}#gate", gate_state,
                       f"{task_id}#gate"))
    # The FUSION edge: concepts this task recalled (recall_log attribution),
    # the SAME source the Task detail 'Knowledge · Understand' rail reads. Each
    # becomes a memory diamond in the mesh, so a task focus actually shows the
    # curated knowledge it pulled in — and 2+ in one domain draw a dashed hull.
    _task_concept_neighbors(p, task_id, out)
    # Code the task's sessions actually modified — DIRECT neighbors, so a task
    # focus shows the files it landed in without a hop through the session.
    _task_code_neighbors(p, sids, out)
    # Test files that pin this task's oracle (same content-based discovery as
    # the detail page's Tests tab) — the covers edge from the design artifact.
    for rel in _pinned_test_files(task_id):
        out.append(_nb("test", rel.rsplit("/", 1)[-1],
                       f"/artifact?focus={rel}", "covers", rel))


def _task_code_neighbors(p, session_ids: list, out: list,
                         cap: int = 12) -> None:
    """Code squares for the files this task's sessions MODIFIED (read files
    stay a session-focus detail — a task's direct code ring is what it shipped
    into). Deduped across sessions, capped so a long drive can't flood the
    ring. Guarded: no conductor service or no file log just adds nothing."""
    conductor_svc = getattr(p, "conductor_svc", None)
    getter = getattr(conductor_svc, "session_file_paths", None)
    if getter is None or not session_ids:
        return
    seen: set = set()
    for sid in session_ids:
        try:
            paths = getter(sid) or {}
        except Exception:
            continue
        for f in (paths.get("modified") or []):
            healed = _indexed_code_path(p, f)
            if not healed or healed in seen:
                continue
            seen.add(healed)
            out.append(_nb("code", healed.split("/")[-1],
                           f"/artifact?focus={healed}", "implements in",
                           healed))
            if len(seen) >= cap:
                return


@lru_cache(maxsize=256)
def _pinned_test_files(task_id: str) -> tuple:
    """Test files that PIN this task's oracle — the same content-based
    discovery as api.tasks.get_task_tests (the red-test file names the task id
    in its module docstring). Cached per task id because a 2-hop mesh expands
    MANY task nodes per request and the scan reads every tests/**/*.py; the
    dev daemon bounces on every change, which clears the cache."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task_id):
        return ()
    from pathlib import Path
    tests_root = Path(__file__).resolve().parents[2] / "tests"
    if not tests_root.is_dir():
        return ()
    short = task_id[:8]
    found: list = []
    for f in sorted(tests_root.rglob("*.py")):
        try:
            text = f.read_text(encoding="utf-8")
        except Exception:
            continue
        if task_id in text or (len(short) >= 8 and short in text):
            found.append(f.relative_to(tests_root.parent).as_posix())
    return tuple(found)


def _task_concept_neighbors(p, task_id: str, out: list) -> None:
    """Memory diamonds for the concepts a task recalled. Sourced from the
    OKF host's task_concepts (recall_log entry_id <- task_id, resolved to live
    concepts), so the mesh's task->concept edges match the rail exactly. Each
    carries the concept's OKF `domain` + `type` (hull grouping + sub-caption)
    and deep-links to /understand?concept=<id>. Fully guarded: a project with
    no memory service — or a host that can't build — just adds no concepts."""
    memory_svc = getattr(p, "memory_svc", None)
    if memory_svc is None:
        return
    try:
        from prism_service.services.okf_host import OkfHost
        concepts = OkfHost(
            memory_svc, getattr(p, "brain_svc", None)
        ).task_concepts(task_id) or []
    except Exception:
        return
    for c in concepts:
        cid = c.get("id") if isinstance(c, dict) else None
        if not cid:
            continue
        out.append(_nb("memory", c.get("title") or cid,
                       f"/understand?concept={cid}", "recalled", cid,
                       domain=c.get("domain"),
                       concept_type=c.get("type")))


def _indexed_code_path(p, path: str) -> Optional[str]:
    """Session file logs hold RAW OS paths (absolute, either slash). Heal to
    the brain's real indexed relative path — the ONE token shape the resolve
    ladder, /artifact deep-links, and code<->code mesh edges agree on.
    Returns None for files outside the indexed project (e.g. a user's global
    memory files): a code square that can't open a live neighborhood is a
    dead-end click, so it never enters the mesh at all."""
    brain_svc = getattr(p, "brain_svc", None)
    if brain_svc is None or not path:
        return None
    return _resolve_file(brain_svc, str(path).replace("\\", "/"))


def _session_neighbors(p, session_id: str, out: list) -> None:
    """Session threads: the task it drove, the code it touched, the retrievals
    it ran (each retrieval attributes back to its asking task)."""
    task_svc = getattr(p, "task_svc", None)
    if task_svc is not None:
        try:
            tid = task_svc.task_for_session(session_id)
        except Exception:
            tid = ""
        if tid:
            t = _task_lookup(task_svc, tid)
            out.append(_nb("task", (_task_title(t) if t else tid) or tid,
                           f"/tasks/{tid}", "drove", tid))
    conductor_svc = getattr(p, "conductor_svc", None)
    getter = getattr(conductor_svc, "session_file_paths", None)
    if getter is not None:
        try:
            paths = getter(session_id) or {}
        except Exception:
            paths = {}
        for rel, files in (("modified", paths.get("modified") or []),
                           ("read", paths.get("read") or [])):
            for f in files:
                healed = _indexed_code_path(p, f)
                if not healed:
                    continue
                out.append(_nb("code", healed.split("/")[-1],
                               f"/artifact?focus={healed}", rel, healed))
    _searches_for(p, out, session_id=session_id)


def _searches_for(p, out: list, session_id: str = "",
                  task_id: str = "") -> None:
    """Retrieval edges: rows in the brain searches table attributed to this
    session or task tie the two together (searched-during). The retrieval is
    the RELATION, surfaced as its counterpart session/task node."""
    brain_svc = getattr(p, "brain_svc", None)
    getter = getattr(brain_svc, "get_recent_searches", None)
    if getter is None:
        return
    try:
        rows = getter(limit=200) or []
    except Exception:
        return
    for r in rows:
        if not isinstance(r, dict):
            continue
        rsid = str(r.get("session_id") or "")
        rtid = str(r.get("task_id") or "")
        if session_id and rsid == session_id and rtid:
            out.append(_nb("task", _short(rtid, 12), f"/tasks/{rtid}",
                           "searched under", rtid))
        elif task_id and rtid == task_id and rsid:
            out.append(_nb("session", _short(rsid), f"/sessions/{rsid}",
                           "searched here", rsid))


def _okf_graph(p) -> Optional[dict]:
    """The OKF wiki's concept graph -- nodes carry {id, title, type, domain}
    (see OkfHost.graph / api.okf) + directed wikilink edges. Read-only, guarded:
    a project without a memory service (or a graph that fails to build) yields
    None so callers just skip the concept edges instead of crashing."""
    memory_svc = getattr(p, "memory_svc", None)
    if memory_svc is None:
        return None
    try:
        from prism_service.services.okf_host import OkfHost
        return OkfHost(memory_svc, getattr(p, "brain_svc", None)).graph()
    except Exception:
        return None


def _memory_neighbors(p, concept_id: str, out: list,
                      g: Optional[dict] = None) -> None:
    """Concept<->concept: the OKF wikilink graph. Outbound [[links]] and
    inbound backlinks are peers -- knowledge citing knowledge. Each memory
    neighbor carries the counterpart concept's OKF `type` + `domain` so the
    mesh can caption the diamond and hull same-domain concepts together."""
    if g is None:
        g = _okf_graph(p)
    if not g:
        return
    meta_by_id = {n["id"]: n for n in g.get("nodes", [])}

    def _mem_nb(other_id: str, edge: str) -> dict:
        m = meta_by_id.get(other_id, {})
        return _nb("memory", m.get("title") or other_id,
                   f"/understand?concept={other_id}", edge, other_id,
                   domain=m.get("domain"), concept_type=m.get("type"))

    for e in g.get("edges", []):
        src, tgt = e.get("source"), e.get("target")
        if src == concept_id and tgt:
            out.append(_mem_nb(tgt, "links"))
        elif tgt == concept_id and src:
            out.append(_mem_nb(src, "cited by"))


def _code_neighbors(p, sym: dict, out: list) -> None:
    """Code<->code: callers (find_references) and callees (call_chain, hop 1)
    of the focused symbol. Each is a peer file in the call graph."""
    brain_svc = getattr(p, "brain_svc", None)
    if brain_svc is None or not sym.get("name"):
        return
    name = sym["name"]
    try:
        for r in (brain_svc.find_references(name, limit=12) or []):
            cn = r.get("name") or r.get("caller_name")
            cf = r.get("file") or r.get("source_file") or ""
            if cn:
                href = f"/artifact?focus={cf}&symbol={cn}" if cf else None
                out.append(_nb("code", cn, href, "called by", cn))
    except Exception:
        pass
    try:
        edges = brain_svc.call_chain(name, depth=1, limit=12,
                                     direction="callees") or []
    except Exception:
        edges = []
    for e in edges:
        to = e.get("to")
        if to and to != name:
            out.append(_nb("code", to, f"/artifact?focus={to}", "calls", to))


def _file_neighbors(p, file: str, out: list) -> None:
    """Code file -> the symbols it defines (edge "defines"), each re-centerable
    to its own callers/callees. Cheap (one outline read) and always available,
    so a file dropped in via a deep-link still opens a live neighborhood."""
    brain_svc = getattr(p, "brain_svc", None)
    if brain_svc is None or not file:
        return
    try:
        syms = brain_svc.outline(file) or []
    except Exception:
        syms = []
    for s in syms:
        # BrainService.outline rows key the symbol as `entity_name`;
        # tolerate `name` for older/fake shapes.
        nm = (s.get("entity_name") or s.get("name")) if isinstance(s, dict) else None
        if nm:
            out.append(_nb("code", nm, f"/artifact?focus={file}&symbol={nm}",
                           "defines", nm))


def _gate_neighbors(p, token: str, out: list) -> None:
    """A gate's one thread home: the task it guards."""
    task_id = token.split("#", 1)[0].strip()
    task_svc = getattr(p, "task_svc", None)
    if not task_id or task_svc is None:
        return
    t = _task_lookup(task_svc, task_id)
    if t:
        out.append(_nb("task", _task_title(t) or task_id,
                       f"/tasks/{task_id}", "guards", task_id))


# ---------------------------------------------------------------------------
# Mesh v2 -- neighbor-to-neighbor edges. A true mesh, not a star: beyond the
# implicit center->neighbor spokes, entities in the collected node set can
# edge to EACH OTHER (memories cite memories, code calls code, a session
# recorded the gate it drove, a session edited a file another neighbor names).
# Every source below takes the node-set membership as an already-built lookup
# and does ONE pass per node (never per pair), fully getattr/try-guarded.
# ---------------------------------------------------------------------------

MESH_EDGE_CAP = 320


def _dedupe_edges(edges: list) -> list:
    """Dedupe on the unordered (from, to) pair, first-seen order preserved
    (spokes are appended before inter-neighbor edges, so a spoke always wins
    over a redundant mesh edge covering the same pair). Drops self-loops and
    hard-caps the total so a dense neighborhood can't blow up the canvas."""
    seen: set = set()
    out: list = []
    for e in edges:
        a, b = e.get("from"), e.get("to")
        if not a or not b or a == b:
            continue
        key = frozenset((a, b))
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= MESH_EDGE_CAP:
            break
    return out


def _memory_mesh_edges(p, memory_tokens: set, g: Optional[dict] = None) -> list:
    """memory<->memory: OKF wikilink edges where BOTH endpoints are already in
    the mesh's node set -- this is what turns two sibling concept diamonds
    into a real triangle instead of two spokes off the center."""
    if len(memory_tokens) < 2:
        return []
    if g is None:
        g = _okf_graph(p)
    if not g:
        return []
    out = []
    for e in g.get("edges", []):
        src, tgt = e.get("source"), e.get("target")
        if src in memory_tokens and tgt in memory_tokens:
            out.append({"from": src, "to": tgt, "label": "[[links]]"})
    return out


def _code_mesh_edges(p, code_tokens: set) -> list:
    """code<->code: the call graph between code nodes already in the set.
    ONE call_chain lookup per code token (not per pair); a callee only
    becomes an edge when it's ALSO a node already in the mesh."""
    brain_svc = getattr(p, "brain_svc", None)
    if brain_svc is None or len(code_tokens) < 2:
        return []
    out = []
    for tok in code_tokens:
        name = tok.rsplit("/", 1)[-1] if "/" in tok else tok  # tolerate file-path tokens
        try:
            callees = brain_svc.call_chain(
                name, depth=1, limit=12, direction="callees") or []
        except Exception:
            continue
        for e in callees:
            to = e.get("to")
            if to and to in code_tokens and to != tok:
                out.append({"from": tok, "to": to, "label": "calls"})
    return out


def _session_gate_mesh_edges(p, session_tokens: set, gate_tokens: set) -> list:
    """session<->gate: a session that recorded the gate's task decision (the
    gate token is `<task_id>#gate`). ONE task_for_session lookup per session."""
    task_svc = getattr(p, "task_svc", None)
    if task_svc is None or not session_tokens or not gate_tokens:
        return []
    gate_by_task = {g.split("#", 1)[0]: g for g in gate_tokens}
    out = []
    for sid in session_tokens:
        try:
            tid = task_svc.task_for_session(sid)
        except Exception:
            tid = None
        gate_tok = gate_by_task.get(tid) if tid else None
        if gate_tok:
            out.append({"from": sid, "to": gate_tok, "label": "recorded"})
    return out


def _session_code_mesh_edges(p, session_tokens: set, code_tokens: set) -> list:
    """session<->code: the session's own touched-file paths (session_file_paths)
    that happen to name a code node already in the set. ONE lookup per session."""
    conductor_svc = getattr(p, "conductor_svc", None)
    getter = getattr(conductor_svc, "session_file_paths", None)
    if getter is None or not session_tokens or not code_tokens:
        return []
    out = []
    for sid in session_tokens:
        try:
            paths = getter(sid) or {}
        except Exception:
            continue
        touched = set(paths.get("modified") or []) | set(paths.get("read") or [])
        for f in touched:
            healed = _indexed_code_path(p, f)
            if healed and healed in code_tokens:
                out.append({"from": sid, "to": healed, "label": "edited"})
    return out


def _build_mesh_edges(p, center_out: dict, neighbors: list) -> list:
    """The mesh's full edge list: the implicit spokes (center->hop1,
    via-parent->hop2) PLUS whatever inter-neighbor edges the sources above
    find among the collected node set. ONE list, so the SPA never special-
    cases a spoke vs. a mesh edge. Deduped + capped at MESH_EDGE_CAP."""
    edges: list = []
    by_kind: dict = {center_out["token"]: center_out.get("kind")}
    for n in neighbors:
        by_kind.setdefault(n["token"], n.get("kind"))

    for n in neighbors:
        if n.get("hop", 1) == 2 and n.get("via"):
            edges.append({"from": n["via"], "to": n["token"], "label": n["edge"]})
        else:
            edges.append({"from": center_out["token"], "to": n["token"], "label": n["edge"]})

    memory_tokens = {t for t, k in by_kind.items() if k == "memory"}
    code_tokens = {t for t, k in by_kind.items() if k == "code"}
    session_tokens = {t for t, k in by_kind.items() if k == "session"}
    gate_tokens = {t for t, k in by_kind.items() if k == "gate"}

    edges.extend(_memory_mesh_edges(p, memory_tokens))
    edges.extend(_code_mesh_edges(p, code_tokens))
    edges.extend(_session_gate_mesh_edges(p, session_tokens, gate_tokens))
    edges.extend(_session_code_mesh_edges(p, session_tokens, code_tokens))

    return _dedupe_edges(edges)


# hops=2 growth guards (artifact "2 hops"): each first-hop node contributes at
# most HOP2_PER_NODE of ITS own neighbors, and the whole second ring is capped
# at HOP2_TOTAL so a high-degree hub can't explode the mesh.
HOP2_PER_NODE = 12
HOP2_TOTAL = 140


def _expand(token: str, p) -> tuple:
    """Resolve `token` and gather its typed 1-hop neighbors via the guarded
    per-kind gatherers. Returns (resolved_center, neighbors, center_domain,
    center_type, kind) -- the single reusable hop the 1- and 2-hop builds share.
    Pure; unit-testable with fake services."""
    center = resolve_token(
        token, p.memory_svc, p.brain_svc, getattr(p, "graph_svc", None),
        task_svc=getattr(p, "task_svc", None),
        conductor_svc=getattr(p, "conductor_svc", None),
    )
    kind = center.get("kind", "unresolved")
    out: list = []
    center_domain: Optional[str] = None
    center_type: Optional[str] = None
    if kind == "task":
        task = _task_lookup(getattr(p, "task_svc", None), token)
        if task is not None:
            _task_neighbors(p, token, task, out)
            _searches_for(p, out, task_id=token)
    elif kind == "session":
        _session_neighbors(p, token, out)
    elif kind == "concept":
        g = _okf_graph(p)
        cid = (_resolve_concept(p.memory_svc, token) or {}).get("id", token)
        _memory_neighbors(p, cid, out, g=g)
        # The focused diamond carries its own type+domain too, so the mesh's
        # center caption + hull grouping include it.
        cm = next((n for n in (g or {}).get("nodes", []) if n["id"] == cid),
                  None) if g else None
        if cm:
            center_domain = cm.get("domain")
            center_type = cm.get("type")
    elif kind == "symbol":
        _code_neighbors(p, _resolve_symbol(p.brain_svc, token) or {}, out)
    elif kind == "file":
        _file_neighbors(p, center.get("label") or token, out)
    elif kind == "gate":
        _gate_neighbors(p, token, out)
    return center, out, center_domain, center_type, kind


def _dedupe(neighbors: list) -> list:
    """Dedupe on (kind, href|label), first-seen order preserved."""
    seen: set = set()
    deduped: list = []
    for n in neighbors:
        key = (n["kind"], n.get("href") or n["label"])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(n)
    return deduped


def neighbors_for(token: str, p, limit: int = 24, hops: int = 1) -> dict:
    """Build the typed neighborhood for `token`. hops=1 is the 1-hop ego net;
    hops=2 additionally expands each first-hop node's OWN neighborhood onto an
    outer ring (degree-capped, total-capped, deduped, no center/self repeats).
    Every neighbor carries its `hop` (1|2); second-hop nodes also carry `via`,
    the first-hop token they hang off, so the mesh draws the edge to the right
    parent instead of the center. The response's `edges` list is the full
    mesh -- implicit spokes PLUS any inter-neighbor edges found among the
    collected node set (see _build_mesh_edges) -- so the client renders one
    edge list instead of assuming every edge touches the center."""
    center, out, center_domain, center_type, kind = _expand(token, p)
    hop1 = _dedupe(out)[: max(1, int(limit))]
    for n in hop1:
        n["hop"] = 1

    center_out = {
        "kind": _UI_KIND.get(kind, kind),
        "label": center.get("label") or token,
        "href": center.get("href"),
        "token": token,
    }
    if center_domain:
        center_out["domain"] = center_domain
    if center_type:
        center_out["concept_type"] = center_type
    # Last motion for a task center (Selected card). Best-effort off the task
    # row's updated_at; omitted honestly when the row has no timestamp.
    if kind == "task":
        t = _task_lookup(getattr(p, "task_svc", None), token)
        motion = (_field(t, "updated_at", "") if t is not None else "") or ""
        if motion:
            center_out["last_motion"] = motion

    neighbors = list(hop1)
    if int(hops) >= 2 and hop1:
        # Seed the seen-set with the center + every first-hop node so the
        # second ring never re-draws either.
        seen = {(center_out["kind"],
                 center_out.get("href") or center_out["label"] or token)}
        for n in hop1:
            seen.add((n["kind"], n.get("href") or n["label"]))
        hop2: list = []
        for parent in hop1:
            if len(hop2) >= HOP2_TOTAL:
                break
            _, pout, _pd, _pt, _pk = _expand(parent["token"], p)
            added = 0
            for nb in pout:
                if added >= HOP2_PER_NODE or len(hop2) >= HOP2_TOTAL:
                    break
                key = (nb["kind"], nb.get("href") or nb["label"])
                if key in seen:
                    continue
                seen.add(key)
                nb["hop"] = 2
                nb["via"] = parent["token"]
                hop2.append(nb)
                added += 1
        neighbors.extend(hop2)

    edges = _build_mesh_edges(p, center_out, neighbors)
    return {"center": center_out, "neighbors": neighbors, "edges": edges}


@router.get("/neighbors")
def neighbors(
    token: str = Query(...), project: str = Query("prism"),
    limit: int = Query(24, ge=1, le=200),
    hops: int = Query(1, ge=1, le=2),
) -> dict:
    """The Explore mesh's ego network: {center, neighbors[], edges[]} for one
    entity. hops=2 grows a degree-capped second ring (artifact's "2 hops"
    toggle); edges is the TRUE mesh -- spokes plus neighbor-to-neighbor
    links -- not just center-touching lines."""
    try:
        p = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return neighbors_for(token, p, limit=limit, hops=hops)


@router.post("/resolve_batch")
def resolve_batch(payload: dict = Body(default={})) -> dict:
    """Resolve many tokens in ONE call so a rendered doc fires a single request
    instead of one-per-chip. This is what lets the renderer resolve eagerly and
    only link the tokens that actually resolve -- the rest stay plain code, no
    broken-link noise. Same per-token semantics as /resolve."""
    project = payload.get("project") or "prism"
    tokens = payload.get("tokens") or []
    try:
        p = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    task_svc = getattr(p, "task_svc", None)
    conductor_svc = getattr(p, "conductor_svc", None)
    results: dict = {}
    for t in tokens[:300]:
        if isinstance(t, str) and t and t not in results:
            results[t] = resolve_token(
                t, p.memory_svc, p.brain_svc, p.graph_svc,
                task_svc=task_svc, conductor_svc=conductor_svc,
            )
    return {"results": results}
