"""xref resolver API -- deterministic token -> destination, no LLM/network.

GET /api/xref/resolve?token=<s>&project=<p> resolves a free token through a
fixed ladder against the SAME per-project services the rest of api/ uses
(memory_svc, brain_svc, graph_svc). Pure read-through; never writes.

Resolve ladder, in order:
  1. OKF/memory id-or-name match  -> kind="concept" -> /understand?concept=<id>
  2. else brain find_symbol hit   -> kind="symbol"  -> /brain?focus=<file>&symbol=<name>
  3. else indexed-file path match -> kind="file"    -> /brain?focus=<file>
  4. else                         -> kind="unresolved"

`summary` is populated from an existing graph_annotations row for the resolved
file WHEN available; it is NEVER fabricated (omitted when absent). `stale` is
not derivable without re-deriving the live input_hash, so it is left absent
for now (both are optional in the frozen contract).
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Query

from prism_service.project_context import get_project

router = APIRouter()

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


def resolve_token(token: str, memory_svc, brain_svc, graph_svc=None) -> dict:
    """Run the deterministic ladder. Pure -- safe to unit-test with fakes."""
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

    return {"kind": "unresolved", "label": token, "href": None}


@router.get("/resolve")
def resolve(token: str = Query(...), project: str = Query("prism")) -> dict:
    """Resolve one token to {kind, label, href, summary?, stale?}."""
    try:
        p = get_project(project)
    except Exception as exc:
        raise HTTPException(404, f"unknown project: {project}: {exc}")
    return resolve_token(token, p.memory_svc, p.brain_svc, p.graph_svc)


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
    results: dict = {}
    for t in tokens[:300]:
        if isinstance(t, str) and t and t not in results:
            results[t] = resolve_token(t, p.memory_svc, p.brain_svc, p.graph_svc)
    return {"results": results}
