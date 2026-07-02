"""In-memory hierarchical index over a project's graphify graph.json.

Powers the Sigma viewer's server-side drill-down (siegeon/.prism graph LOD).
The viewer used to fetch the ENTIRE graph (all leaves + edges — 100k+ nodes
on a large monorepo, a 150MB+ payload) and aggregate/lay-out client-side,
which made big projects unbrowsable. This module instead lets the endpoint
return only the slice being browsed:

    root                -> the L0 super-nodes (domains)         ~hundreds
    focus=<l0>, level=0 -> that domain's L1 children (services)
    focus=<l1>, level=1 -> that service's L2 children (modules)
    focus=<l2>, level=2 -> that module's real leaf symbols       (capped)

Each level is a group-by over the precomputed per-leaf {l0,l1,l2} keys plus
an aggregation of leaf edges up to the display level. graph.json is parsed
once and cached by (project, mtime); every drill request is then a cheap
in-memory lookup.
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from pathlib import Path
from typing import Optional

from prism_service.services.graph_service import compute_node_hierarchy

# Leaf level is 3; aggregated super-node levels are 0/1/2.
LEAF_LEVEL = 3
# Cap on leaf nodes returned for a single L2 module drill. A handful of
# modules (e.g. a giant `actions` tree) hold tens of thousands of leaves;
# past this the client can't render usefully anyway, so we return the
# highest-degree ones and flag the truncation.
DEFAULT_LEAF_CAP = 800


class HierIndex:
    """Precomputed hierarchy + edge aggregation for one graph.json."""

    def __init__(self, data: dict) -> None:
        raw_nodes = [
            n for n in data.get("nodes", [])
            if n.get("file_type") != "rationale"
        ]
        raw_edges = data.get("links") or data.get("edges") or []

        # id -> compact metadata (incl. l0/l1/l2 keys)
        self.meta: dict = {}
        # level (0/1/2) -> {leaf_id -> key at that level}
        self.level_key: dict = {0: {}, 1: {}, 2: {}}
        # level (0/1/2) -> {key -> [leaf_id, ...]}
        self.members: dict = {0: defaultdict(list), 1: defaultdict(list),
                              2: defaultdict(list)}
        self.degree: dict = defaultdict(float)

        for n in raw_nodes:
            nid = n.get("id")
            if nid is None:
                continue
            h = compute_node_hierarchy(
                n.get("source_file"), fallback_community=n.get("community"))
            self.meta[nid] = {
                "id": nid,
                "label": n.get("label") or nid,
                "source_file": n.get("source_file"),
                "community": n.get("community"),
                "l0": h["l0"], "l1": h["l1"], "l2": h["l2"],
            }
            for lvl, k in ((0, h["l0"]), (1, h["l1"]), (2, h["l2"])):
                if k is not None:
                    self.level_key[lvl][nid] = k
                    self.members[lvl][k].append(nid)

        # Compact leaf edge list (only edges whose endpoints both survived
        # the rationale filter, no self-loops).
        self.edges: list = []
        for e in raw_edges:
            s = e.get("source")
            t = e.get("target")
            if s in self.meta and t in self.meta and s != t:
                w = float(e.get("weight") or 1.0)
                self.edges.append((s, t, w))
                self.degree[s] += w
                self.degree[t] += w

        # Precompute aggregated super-edge weights per level:
        #   super_edges[lvl][(keyA, keyB)] = summed weight   (keyA < keyB)
        self.super_edges: dict = {0: defaultdict(float), 1: defaultdict(float),
                                 2: defaultdict(float)}
        for lvl in (0, 1, 2):
            lk = self.level_key[lvl]
            se = self.super_edges[lvl]
            for s, t, w in self.edges:
                ks = lk.get(s)
                kt = lk.get(t)
                if ks is None or kt is None or ks == kt:
                    continue
                a, b = (ks, kt) if ks < kt else (kt, ks)
                se[(a, b)] += w

        # Parent -> child-key sets, for deciding whether a super-node has
        # real sub-structure (drill to children) or should jump to leaves.
        #   children[0][l0] = {l1, ...}   children[1][l1] = {l2, ...}
        self.children: dict = {0: defaultdict(set), 1: defaultdict(set)}
        for m in self.meta.values():
            if m["l0"] is not None and m["l1"] is not None:
                self.children[0][m["l0"]].add(m["l1"])
            if m["l1"] is not None and m["l2"] is not None:
                self.children[1][m["l1"]].add(m["l2"])

    # -- payload builders ------------------------------------------------

    def _super_label(self, key: str, labels: dict) -> str:
        """Human label for a super-node key (path-prefix or comm:<id>)."""
        if key in labels and labels[key]:
            return labels[key]
        if key.startswith("comm:"):
            return key
        return key.rsplit("/", 1)[-1] or key

    def _supers(self, lvl: int, keys: list, labels: dict) -> dict:
        keyset = set(keys)
        nodes = []
        for k in keys:
            members = self.members[lvl].get(k, [])
            deg = sum(self.degree.get(mid, 0.0) for mid in members)
            # child_count > 0 and level < 2 means "expands to sub-clusters";
            # a level-2 super always expands to its leaves.
            if lvl < 2:
                child_count = len({c for c in self.children[lvl].get(k, set())
                                   if c != k})
            else:
                child_count = 0
            nodes.append({
                "id": k,
                "label": self._super_label(k, labels),
                "level": lvl,
                "is_super": True,
                "size": len(members),
                "degree": deg,
                "child_count": child_count,
            })
        edges = []
        for (a, b), w in self.super_edges[lvl].items():
            if a in keyset and b in keyset:
                edges.append({"source": a, "target": b, "weight": w})
        return {"nodes": nodes, "edges": edges}

    def _leaves(self, leaf_ids: list, cap: int, labels: dict,
                context_level: int = 1, max_context: int = 14) -> dict:
        total = len(leaf_ids)
        truncated = False
        if total > cap:
            leaf_ids = sorted(
                leaf_ids, key=lambda i: self.degree.get(i, 0.0), reverse=True
            )[:cap]
            truncated = True
        keyset = set(leaf_ids)
        nodes = []
        for lid in leaf_ids:
            m = self.meta[lid]
            nodes.append({
                "id": lid,
                "label": m["label"],
                "level": LEAF_LEVEL,
                "is_super": False,
                "source_file": m["source_file"],
                "community": m["community"],
                "degree": self.degree.get(lid, 0.0),
            })
        edges = [
            {"source": s, "target": t, "weight": w}
            for (s, t, w) in self.edges
            if s in keyset and t in keyset
        ]

        # Context clusters (option b): leaves in a group — especially the
        # comm:<id> fallback buckets — often connect to symbols OUTSIDE the
        # group, so an internal-edges-only view is a disconnected cloud.
        # Collapse each out-of-group endpoint to its cluster key at
        # `context_level` and add leaf -> context super-node edges, so every
        # leaf that links anywhere shows where it goes (and FA2 pulls the
        # cloud into satellites around those hubs). Marked is_context so the
        # client styles them apart from the group's own nodes.
        ctx_nodes, ctx_edges = self._context_links(
            keyset, labels, context_level, max_context)

        return {"nodes": nodes + ctx_nodes, "edges": edges + ctx_edges,
                "truncated": truncated, "total_leaves": total,
                "leaf_count": len(leaf_ids), "context_count": len(ctx_nodes)}

    def _context_links(self, keyset: set, labels: dict, context_level: int,
                       max_context: int) -> tuple:
        """Aggregate out-of-group edges into `is_context` super-nodes.

        Returns (context_nodes, context_edges). An edge with exactly one
        endpoint inside `keyset` contributes weight from the inside leaf to
        the context cluster the outside endpoint rolls up to. Only the
        top `max_context` clusters by summed weight are kept.
        """
        lk = self.level_key[context_level]
        ctx_weight: dict = defaultdict(float)
        pair_weight: dict = defaultdict(float)
        for s, t, w in self.edges:
            s_in = s in keyset
            t_in = t in keyset
            if s_in == t_in:  # both inside (already drawn) or both outside
                continue
            inside, outside = (s, t) if s_in else (t, s)
            ck = lk.get(outside)
            if ck is None:
                continue
            ctx_weight[ck] += w
            pair_weight[(inside, ck)] += w

        if not ctx_weight:
            return [], []
        top = sorted(ctx_weight, key=ctx_weight.get, reverse=True)[:max_context]
        top_set = set(top)
        nodes = []
        for ck in top:
            members = self.members[context_level].get(ck, [])
            child_count = (
                len({c for c in self.children[context_level].get(ck, set())
                     if c != ck}) if context_level < 2 else 0)
            nodes.append({
                "id": "ctx::" + ck,
                "label": self._super_label(ck, labels),
                "level": context_level,
                "is_super": True,
                "is_context": True,
                "size": len(members),
                "degree": ctx_weight[ck],
                "child_count": child_count,
                "nav_key": ck,
                "nav_level": context_level,
            })
        edges = [
            {"source": leaf, "target": "ctx::" + ck, "weight": w,
             "context": True}
            for (leaf, ck), w in pair_weight.items() if ck in top_set
        ]
        return nodes, edges

    def build(self, focus: Optional[str], level: Optional[int],
              labels: dict, cap: int = DEFAULT_LEAF_CAP,
              context_level: int = 1, max_context: int = 14) -> dict:
        """Return the subgraph for a drill position.

        focus=None            -> L0 super-nodes (domains).
        focus set, level 0/1  -> children super-nodes, or leaves if the
                                 super-node has no real sub-structure.
        focus set, level 2    -> the module's leaf symbols (capped), plus
                                 context super-nodes for out-of-group links.
        """
        if focus is None:
            out = self._supers(0, list(self.members[0].keys()), labels)
            out.update({"level": 0, "focus": None, "leaf": False})
            return out

        lvl = int(level or 0)
        # Try to descend to real child super-nodes first.
        if lvl in (0, 1):
            children = {c for c in self.children[lvl].get(focus, set())
                        if c != focus}
            if children:
                out = self._supers(lvl + 1, sorted(children), labels)
                out.update({"level": lvl + 1, "focus": focus, "leaf": False})
                return out
        # No sub-structure (or already at L2): show leaves under `focus`.
        # Context collapses to the parent level of the leaves (so a module's
        # symbols show sibling *services*, not each other), clamped to >=0.
        clevel = max(0, min(2, context_level))
        leaf_ids = self.members[lvl].get(focus, [])
        out = self._leaves(leaf_ids, cap, labels,
                           context_level=clevel, max_context=max_context)
        out.update({"level": LEAF_LEVEL, "focus": focus, "leaf": True})
        return out


_CACHE: dict = {}
_LOCK = threading.Lock()


def get_index(project_id: str, json_path: Path) -> HierIndex:
    """Return a cached HierIndex for graph.json, rebuilding on mtime change."""
    mtime = json_path.stat().st_mtime
    key = project_id
    with _LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] == mtime:
            return cached[1]
    # Parse + build outside the lock (slow); last writer wins — fine.
    data = json.loads(json_path.read_text(encoding="utf-8"))
    idx = HierIndex(data)
    with _LOCK:
        _CACHE[key] = (mtime, idx)
    return idx
