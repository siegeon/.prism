"""Ultimate Graph narrative layer — background cluster enrichment
(siegeon/.prism#50, slices 1/2/6).

The deterministic structure layer (entities, edges, communities, the
l0/l1/l2 path hierarchy) is truth but reads badly: cluster names are
string-stitched into junk like "prism service prism service". This worker
cleans them with *inference* — it walks the code's own hierarchy (domain →
service → module, the same levels the Sigma viewer drills), and for each
node asks `claude -p` for a short human name + one-line purpose, written to
graph_annotations with provenance.

Two hard constraints from the design:
  * ESCAPE WHEN UNCHANGED. Each scope's inputs (its member files) hash to an
    input_hash; if the stored annotation's hash still matches, the scope is
    skipped. A run where nothing changed does no inference at all, so the
    job idles instead of burning tokens.
  * STRUCTURE STAYS SEPARATE. Annotations never mutate the entities/edges;
    they live in graph_annotations with provenance, so the LLM narrative
    can't corrupt structural truth.

Pattern mirrors memory_summary_worker (env-gated daemon shelling claude).
Env:
  PRISM_GRAPH_ENRICH_WORKER=off            # default on
  PRISM_GRAPH_ENRICH_WORKER_INTERVAL=120   # seconds between sweeps
  PRISM_GRAPH_ENRICH_WORKER_MAX_PER_CYCLE=4
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys as _sys
import threading
import time

WORKER_ID = "graph_enrich_worker"
WORKER_LABEL = "Cluster name writer"

LEVEL_NAMES = {0: "domain", 1: "service", 2: "module"}

ENRICH_PROMPT_TEMPLATE = (
    "You are naming a cluster in a code knowledge graph. The cluster is a "
    "{level} — a group of related files in one project.\n\n"
    "Path key: {key}\n"
    "Member files ({n_files}):\n{files}\n\n"
    "Representative symbols:\n{symbols}\n\n"
    "Give it a SHORT human name (2-4 words, Title Case, no path syntax, no "
    "repetition) and a ONE-sentence purpose (under 20 words) describing what "
    "this {level} does. Reply with ONLY compact JSON, no prose, no fences:\n"
    '{{"name": "...", "purpose": "..."}}'
)


def _env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").lower()
    if raw in ("1", "on", "true", "yes"):
        return True
    if raw in ("0", "off", "false", "no"):
        return False
    return default


def is_enabled() -> bool:
    return _env_truthy("PRISM_GRAPH_ENRICH_WORKER", default=True)


def _interval_s() -> int:
    try:
        return max(30, int(os.environ.get("PRISM_GRAPH_ENRICH_WORKER_INTERVAL", "120")))
    except ValueError:
        return 120


def _max_per_cycle() -> int:
    try:
        return max(1, int(os.environ.get("PRISM_GRAPH_ENRICH_WORKER_MAX_PER_CYCLE", "4")))
    except ValueError:
        return 4


def _log(msg: str) -> None:
    print(f"[{WORKER_ID}] {msg}", file=_sys.stderr, flush=True)


def _input_hash(files: list[str]) -> str:
    """Stable hash of a scope's membership. Changes only when files are
    added/removed from the cluster — that's the 'data changed' signal."""
    return hashlib.sha256("\n".join(sorted(files)).encode("utf-8")).hexdigest()[:16]


def hierarchy_scopes(project: str, min_files: int = 2) -> list[dict]:
    """Enumerate domain/service/module scopes from the code hierarchy.

    Reads the SAME graph.json + hierarchy keys the Sigma viewer renders
    (incl. the fallback_community -> 'comm:N' bucketing for path-less
    files), so every super-node the user can see gets a scope_id that
    matches — no straggler keeps its string-stitched junk label. Groups
    member files + sample symbols per l0/l1/l2 key; larger clusters first;
    `min_files` drops one-file singletons.
    """
    import json as _json
    from prism_service.project_context import get_project
    from prism_service.services.graph_service import compute_node_hierarchy
    try:
        ctx = get_project(project)
        json_path = ctx._data_dir / "graphify-src" / "graphify-out" / "graph.json"
    except Exception:
        return []
    if not json_path.exists():
        return []
    try:
        data = _json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_nodes = [n for n in data.get("nodes", []) if n.get("file_type") != "rationale"]

    # key -> {level, files:set, symbols:list}
    groups: dict[str, dict] = {}
    for n in raw_nodes:
        sf = n.get("source_file") or ""
        h = compute_node_hierarchy(sf, fallback_community=n.get("community"))
        name = n.get("label") or n.get("id")
        member = sf or (f"node:{n.get('id')}")
        for lvl in (0, 1, 2):
            key = h.get(f"l{lvl}")
            if not key:
                continue
            g = groups.setdefault(key, {"level": lvl, "files": set(), "symbols": []})
            g["files"].add(member)
            if name and len(g["symbols"]) < 24:
                g["symbols"].append(name)

    scopes = []
    for key, g in groups.items():
        files = sorted(g["files"])
        if len(files) < min_files:
            continue
        scopes.append({
            "scope_id": key,
            "level": g["level"],
            "files": files,
            "symbols": g["symbols"],
            "input_hash": _input_hash(files),
        })
    scopes.sort(key=lambda s: len(s["files"]), reverse=True)
    return scopes


def community_scopes(project: str, min_size: int = 3) -> list[dict]:
    """Enumerate Leiden communities (the clusters shown at L3 / symbol
    level) with member files + sample symbols. These are what read as
    'prism service · …' junk today because the deterministic labeler keys
    off the dominant path — every cluster in a single-service repo collapses
    to the same prefix. Inference names them by what they actually DO.
    scope_id = the community id (string). level=None -> prompt says 'cluster'.
    """
    from prism_service.project_context import get_project
    try:
        ctx = get_project(project)
        db = str(ctx._data_dir / "graph.db")
    except Exception:
        return []
    try:
        conn = sqlite3.connect(db, timeout=5.0)
        rows = conn.execute(
            "SELECT community, file, name FROM entities "
            "WHERE community IS NOT NULL"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return []
    groups: dict = {}
    for cid, file, name in rows:
        if cid is None:
            continue
        g = groups.setdefault(int(cid), {"files": set(), "symbols": []})
        if file:
            g["files"].add(file)
        if name and len(g["symbols"]) < 24:
            g["symbols"].append(name)
    scopes = []
    for cid, g in groups.items():
        files = sorted(g["files"])
        size = len(g["symbols"]) + len(files)
        if size < min_size:
            continue
        scopes.append({
            "scope_id": str(cid),
            "level": None,            # render_prompt -> "cluster"
            "files": files or [f"community:{cid}"],
            "symbols": g["symbols"],
            "input_hash": _input_hash(files + g["symbols"]),
        })
    scopes.sort(key=lambda s: len(s["symbols"]) + len(s["files"]), reverse=True)
    return scopes


def _basename(p: str) -> str:
    return os.path.basename((p or "").replace("\\", "/"))


def render_prompt(scope: dict) -> str:
    files = "\n".join(f"  - {_basename(f)}" for f in scope["files"][:30])
    syms = ", ".join(sorted(set(scope["symbols"]))[:24]) or "(none)"
    return ENRICH_PROMPT_TEMPLATE.format(
        level=LEVEL_NAMES.get(scope["level"], "cluster"),
        key=scope["scope_id"],
        n_files=len(scope["files"]),
        files=files,
        symbols=syms,
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> tuple[str, str]:
    m = _JSON_RE.search(text or "")
    if not m:
        return "", ""
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return "", ""
    name = " ".join(str(obj.get("name", "")).split())[:60]
    purpose = " ".join(str(obj.get("purpose", "")).split())[:160]
    return name, purpose


def enrich_one(scope: dict, project: str) -> tuple[str, str]:
    """Infer (name, purpose) for one scope via claude -p. Returns ("","")
    on any failure so the caller moves on and retries next cycle."""
    from prism_service.inference import claude_cli
    from prism_service.project_context import get_project
    try:
        work_dir = str(get_project(project)._data_dir)
    except Exception:
        work_dir = os.getcwd()
    try:
        res = claude_cli.invoke(
            prompt=render_prompt(scope),
            work_dir=work_dir, plugin_dir=work_dir, max_turns=1,
            project=project, purpose="graph_enrich", allowed_tools=(),
        )
    except claude_cli.ClaudeNotLoggedInError:
        _log("claude not logged in; pausing this cycle")
        raise
    except Exception as exc:
        _log(f"enrich_one raised: {exc}")
        return "", ""
    text = res.final_text() if res.exit_code == 0 else ""
    return _parse(text)


def _enrich_kind(graph, project, scope_kind, scopes, budget, out):
    """Enrich up to `budget` CHANGED scopes of one kind. Escapes scopes
    whose input_hash still matches. Returns how many of the budget remain."""
    changed = []
    for s in scopes:
        existing = graph.get_annotation(scope_kind, s["scope_id"], "name")
        if existing and existing.get("input_hash") == s["input_hash"]:
            out["skipped"] += 1   # escape — unchanged
        else:
            changed.append(s)
    out["pending"] += len(changed)
    for s in changed[:budget]:
        try:
            name, purpose = enrich_one(s, project)
        except Exception:
            return 0  # auth missing — stop this cycle entirely
        budget -= 1
        if not name:
            out["errors"] += 1
            continue
        prov = f"claude @ {time.strftime('%Y-%m-%d')}"
        if graph.upsert_annotation(scope_kind, s["scope_id"], "name",
                                   name, purpose, s["input_hash"], prov):
            out["enriched"] += 1
        else:
            out["errors"] += 1
    return budget


def enrich_project_once(project: str, max_per_cycle: int) -> dict:
    """Enrich up to `max_per_cycle` CHANGED scopes for a project — both the
    path hierarchy (domain/service/module) AND the Leiden communities (the
    L3 clusters). Escapes (does zero inference) when every scope's
    input_hash already matches its stored annotation."""
    from prism_service.project_context import get_project
    out = {"project": project, "enriched": 0, "skipped": 0, "errors": 0, "pending": 0}
    try:
        graph = get_project(project).graph_svc
    except Exception:
        return out
    # Hierarchy first (cheaper, fewer), then spend any remaining budget on
    # communities (the bigger, noisier set the user sees at L3).
    budget = _enrich_kind(graph, project, "hierarchy",
                          hierarchy_scopes(project), max_per_cycle, out)
    if budget > 0:
        _enrich_kind(graph, project, "community",
                     community_scopes(project), budget, out)
    return out


def run_once() -> dict:
    from prism_service.config import list_projects
    n = _max_per_cycle()
    summary = {"projects": [], "enriched": 0}
    try:
        projects = list_projects()
    except Exception as exc:
        _log(f"list_projects failed: {exc}")
        return summary
    for project in projects:
        r = enrich_project_once(project, n)
        summary["enriched"] += r["enriched"]
        if r["enriched"] or r["pending"]:
            summary["projects"].append(r)
    return summary


def _loop() -> None:
    interval = _interval_s()
    _log(f"started; interval={interval}s")
    while True:
        try:
            run_once()
        except Exception as exc:
            _log(f"run_once raised: {exc}")
        time.sleep(interval)


def start_graph_enrich_worker() -> "threading.Thread | None":
    if not is_enabled():
        return None
    t = threading.Thread(target=_loop, name="prism-graph-enrich-worker", daemon=True)
    t.start()
    return t
