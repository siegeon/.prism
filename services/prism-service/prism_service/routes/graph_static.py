"""Graph static routes — Sigma.js viewer HTML and graphify-visual JSON.

Extracted from the deleted app/ui/graph_page.py during the v5.0.0 cutover.
The interactive viewer is server-rendered HTML that delegates rendering to
the user's browser GPU; community labels and graph.json are served as JSON
files. None of these routes depend on NiceGUI.
"""

from __future__ import annotations

import json
import re
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from prism_service.project_context import get_project
from prism_service.services.graph_service import compute_node_hierarchy
from prism_service.services import graph_hier_index

router = APIRouter()


_SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
# Only graph.json is served now — the WebGL viewer is the only frontend.
# The legacy graphify graph.html / GRAPH_REPORT.md paths were dropped
# because they capped at ~11K nodes and the Sigma viewer covers every
# size graphify can produce.
_ALLOWED_VISUAL_FILES = {"graph.json"}


_SIGMA_VIEWER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>PRISM Graph Viewer</title>
<style>
  html, body { height: 100%; }
  body { margin: 0; font-family: system-ui, sans-serif;
         background: #0f0f1a; color: #e5e7eb;
         display: flex; height: 100vh; overflow: hidden; }
  #graph-wrap { flex: 1; position: relative; }
  #graph { position: absolute; inset: 0; }
  #status { position: absolute; top: 8px; left: 8px; padding: 6px 10px;
            background: rgba(15,15,26,0.8); border: 1px solid #2a2a4e;
            border-radius: 6px; font-size: 12px; z-index: 10; max-width: 60ch; }
  #hint { position: absolute; bottom: 8px; left: 8px; padding: 6px 10px;
          background: rgba(15,15,26,0.8); border: 1px solid #2a2a4e;
          border-radius: 6px; font-size: 11px; z-index: 10; color: #9ca3af; }
  /* Right-side legend panel — matches graphify's graph.html styling
     so users can see cluster labels + toggle clusters on/off. */
  #sidebar { width: 280px; background: #1a1a2e; border-left: 1px solid #2a2a4e;
             display: flex; flex-direction: column; overflow: hidden; }
  #sidebar h3 { font-size: 12px; color: #aaa; margin: 0 0 10px 0;
                text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
  #legend-wrap { flex: 1; overflow-y: auto; padding: 14px; }
  .legend-item { display: flex; align-items: center; gap: 8px;
                 padding: 5px 4px; cursor: pointer; border-radius: 4px;
                 font-size: 12px; user-select: none; }
  .legend-item:hover { background: #2a2a4e; }
  .legend-item.dimmed { opacity: 0.35; }
  .legend-dot { width: 12px; height: 12px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1; overflow: hidden; text-overflow: ellipsis;
                  white-space: nowrap; color: #e0e0e0; }
  .legend-count { color: #666; font-size: 11px; }
  .legend-hide { width: 16px; height: 16px; line-height: 14px; border-radius: 3px;
                 background: transparent; border: 0; color: #888; cursor: pointer;
                 font-size: 14px; padding: 0; opacity: 0; flex-shrink: 0; }
  .legend-item:hover .legend-hide { opacity: 0.6; }
  .legend-hide:hover { opacity: 1 !important; background: rgba(255,230,203,0.15);
                       color: var(--midground-base, #ffe6cb); }
  .legend-item.dimmed .legend-hide { opacity: 0.9; color: #ffe6cb; }
  #sidebar-stats { padding: 10px 14px; border-top: 1px solid #2a2a4e;
                   font-size: 11px; color: #666; }
  * { scrollbar-color: #2a2a4e transparent; scrollbar-width: thin; }
  /* GitNexus-style chrome (round 1) — MIT, see header. */
  #toolbar { position: absolute; top: 8px; right: 8px; display: flex;
             gap: 4px; padding: 4px;
             background: rgba(15,15,26,0.85); border: 1px solid #2a2a4e;
             border-radius: 6px; z-index: 12; }
  #toolbar button { background: transparent; border: 0;
             color: #cfd2dc; cursor: pointer; padding: 6px 8px;
             border-radius: 4px; font-size: 11px;
             display: inline-flex; align-items: center; gap: 4px;
             min-width: 30px; justify-content: center; }
  #toolbar button:hover { background: #2a2a4e; }
  #toolbar button.active { color: #fff; background: #2a2a4e; }
  #toolbar button svg { width: 14px; height: 14px;
             stroke: currentColor; stroke-width: 2; fill: none;
             stroke-linecap: round; stroke-linejoin: round; }
  .tb-sep { width: 1px; background: #2a2a4e; margin: 4px 2px; }
  /* Filter section in sidebar */
  #filters { padding: 14px 14px 8px 14px;
             border-bottom: 1px solid #2a2a4e; }
  #filters h3 { font-size: 12px; color: #aaa; margin: 0 0 8px 0;
                text-transform: uppercase; letter-spacing: 0.05em;
                font-weight: 600; }
  .chip-row { display: flex; flex-wrap: wrap; gap: 4px;
              margin-bottom: 10px; }
  .chip-row:last-child { margin-bottom: 0; }
  .chip-label { font-size: 9px; text-transform: uppercase;
                letter-spacing: 0.06em; color: #777;
                width: 100%; margin-bottom: 2px; }
  .chip { font-size: 11px; padding: 3px 8px; border-radius: 999px;
          border: 1px solid #2a2a4e; background: transparent;
          color: #cfd2dc; cursor: pointer; user-select: none; }
  .chip:hover { background: #2a2a4e; }
  .chip.off { color: #555; border-style: dashed; }
  .chip-count { color: #666; font-size: 10px; margin-left: 4px; }

</style>
</head>
<body>
<div id="graph-wrap">
  <div id="status">Loading graph...</div>
  <div id="graph"></div>
  <div id="hint">Scroll to zoom Context -> Code (C4 depth) · drag to pan · click a super-node to drill in</div>
<div id="toolbar">
  <button id="tb-zoom-in" title="Zoom in"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.3-4.3M8 11h6M11 8v6"/></svg></button>
  <button id="tb-zoom-out" title="Zoom out"><svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.3-4.3M8 11h6"/></svg></button>
  <button id="tb-fit" title="Fit to view"><svg viewBox="0 0 24 24"><path d="M3 9V5a2 2 0 012-2h4M21 9V5a2 2 0 00-2-2h-4M3 15v4a2 2 0 002 2h4M21 15v4a2 2 0 01-2 2h-4"/></svg></button>
  <button id="tb-focus" title="Focus selected node"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3"/></svg></button>
  <span class="tb-sep"></span>
  <button id="tb-pause" title="Pause / resume layout"><svg viewBox="0 0 24 24"><path d="M6 4v16M10 4v16M14 4v16M18 4v16"/></svg></button>
  <button id="tb-reset" title="Reset view"><svg viewBox="0 0 24 24"><path d="M3 12a9 9 0 109-9M3 12V5M3 12h7"/></svg></button>
  <button id="tb-edges" title="Toggle curved edges" class="active"><svg viewBox="0 0 24 24"><path d="M3 21c0-9 6-9 9-9s9 0 9-9"/></svg></button>
  <button id="tb-labels" title="Toggle labels" class="active"><svg viewBox="0 0 24 24"><path d="M4 4h12M4 9h8M4 14h16M4 19h6"/></svg></button>
</div>
</div>
<aside id="sidebar">
  <div id="filters">
    <h3>Filters</h3>
    <div class="chip-label">Language</div>
    <div class="chip-row" id="filter-langs"></div>
    <div class="chip-label">Edge type</div>
    <div class="chip-row" id="filter-edges"></div>
  </div>
  <div id="legend-wrap">
    <h3>Clusters</h3>
    <div id="legend-list"></div>
  </div>
  <div id="sidebar-stats">Loading...</div>
</aside>
<script type="module">
  // PRISM Graph Viewer — server-side drill-down (siegeon/.prism graph LOD).
  //
  // The viewer no longer loads the whole graph. It fetches exactly the
  // slice being browsed from /graphify-visual/<p>/hierarchy.json:
  //   root            -> L0 domains (super-nodes)
  //   click a super   -> its children (services/modules) or leaves
  // Each view is a few hundred nodes, so FA2 layout runs synchronously and
  // instantly. Drilling swaps the view; a breadcrumb walks back up.
  import Graph from "https://esm.sh/graphology@0.25.4";
  import forceAtlas2 from "https://esm.sh/graphology-layout-forceatlas2@0.10.1";
  import Sigma from "https://esm.sh/sigma@3.0.3";
  import EdgeCurveProgram from "https://esm.sh/@sigma/edge-curve@3.1.0?deps=sigma@3.0.3";

  const PROJECT_ID = "__PROJECT_ID__";
  const statusEl = document.getElementById("status");
  const hintEl = document.getElementById("hint");
  const legendList = document.getElementById("legend-list");
  const statsEl = document.getElementById("sidebar-stats");
  const legendTitle = document.querySelector("#legend-wrap h3");
  document.getElementById("filters").style.display = "none";

  // Community palette — mirrors web/src/lib/palette.ts (keep in lock-step).
  const COMMUNITY_COLORS = [
    "#5eead4", "#a3d9a5", "#fcd34d", "#f9a8d4", "#c4b5fd", "#6ee7b7", "#cbd5e1",
    "#8ff5e6", "#bee5c0", "#fde58a", "#fbc7e5", "#d6cdfd", "#9ef0c9", "#dde4eb",
  ];
  function hashStr(s) {
    let h = 0; s = s || "";
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return Math.abs(h);
  }
  function colorForCommunity(c) {
    if (c === undefined || c === null) return "#6b7280";
    return COMMUNITY_COLORS[Math.abs(Number(c) || 0) % COMMUNITY_COLORS.length];
  }
  function colorForKey(k) {
    return COMMUNITY_COLORS[hashStr(k) % COMMUNITY_COLORS.length];
  }
  function withAlpha(hex, a) {
    const h = (hex || "#6b7280").replace("#", "");
    const r = parseInt(h.substring(0, 2), 16);
    const g = parseInt(h.substring(2, 4), 16);
    const b = parseInt(h.substring(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  // C4-flavored level names: L0 domains -> L1 services -> L2 modules -> symbols
  const C4 = ["Context · domains", "Containers · services",
              "Components · modules", "Code · symbols"];

  let renderer = null;
  let graph = null;
  let showLabels = true;
  let hoverNode = null;
  let neighbors = null;
  let loading = false;

  // Breadcrumb / drill stack. Each entry: {focus, level, label}. Root has
  // focus=null; every click pushes the clicked super-node.
  const nav = [{ focus: null, level: null, label: "all domains" }];

  // Breadcrumb bar (injected above the canvas).
  const crumbs = document.createElement("div");
  crumbs.id = "crumbs";
  crumbs.style.cssText =
    "position:absolute;top:8px;left:50%;transform:translateX(-50%);" +
    "z-index:11;display:flex;gap:4px;align-items:center;flex-wrap:wrap;" +
    "max-width:70%;padding:5px 8px;background:rgba(15,15,26,0.85);" +
    "border:1px solid #2a2a4e;border-radius:6px;font-size:12px;";
  document.getElementById("graph-wrap").appendChild(crumbs);

  function apiUrl(focus, level) {
    const p = new URLSearchParams();
    if (focus !== null && focus !== undefined) p.set("focus", focus);
    if (level !== null && level !== undefined) p.set("level", String(level));
    p.set("_", String(Date.now()));
    return `/graphify-visual/${PROJECT_ID}/hierarchy.json?` + p.toString();
  }

  const sizeForSuper = (n) => 6 + Math.min(28, Math.sqrt(n || 1) * 1.5);
  const sizeForLeaf = (d) => 2.5 + Math.min(13, Math.sqrt(d || 0) * 1.3);

  async function loadView(focus, level) {
    if (loading) return;
    loading = true;
    statusEl.textContent = "Loading…";
    let data;
    try {
      const r = await fetch(apiUrl(focus, level), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      data = await r.json();
    } catch (e) {
      statusEl.textContent = "Error loading graph: " + e.message;
      loading = false;
      return;
    }

    const nodes = data.nodes || [];
    const edges = data.edges || [];
    const g = new Graph({ multi: false, type: "undirected" });

    for (const n of nodes) {
      if (g.hasNode(n.id)) continue;
      const isSuper = !!n.is_super;
      const isCtx = !!n.is_context;
      // Context nodes read as external references: a muted slate hue and a
      // capped size so they frame the group without dominating it.
      const color = isCtx ? "#7c86a8"
        : (isSuper ? colorForKey(n.id) : colorForCommunity(n.community));
      const size = isCtx ? Math.min(sizeForSuper(n.size), 15)
        : (isSuper ? sizeForSuper(n.size) : sizeForLeaf(n.degree));
      g.addNode(n.id, {
        x: Math.random(), y: Math.random(),
        size, color,
        label: isCtx ? "↗ " + (n.label || n.id) : (n.label || n.id),
        _super: isSuper,
        _context: isCtx,
        _navKey: n.nav_key || null,
        _navLevel: n.nav_level != null ? n.nav_level : null,
        _level: n.level,
        _child: n.child_count || 0,
        _count: n.size || 0,
        _src: n.source_file || "",
      });
    }
    for (const e of edges) {
      if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue;
      if (e.source === e.target || g.hasEdge(e.source, e.target)) continue;
      const isCtx = !!e.context;
      const c = g.getNodeAttribute(e.source, "color");
      g.addEdge(e.source, e.target, {
        // External links: thin + dimmer, tinted toward the context hue so
        // they read as "leaves this group, goes over there."
        size: isCtx ? 0.5
          : Math.max(0.4, Math.min(4, Math.log2((e.weight || 1) + 1))),
        color: isCtx ? "rgba(124,134,168,0.28)" : withAlpha(c, 0.22),
        type: "curve",
        _context: isCtx,
      });
    }

    if (g.order > 0) {
      const iters = g.order > 400 ? 120 : (g.order > 80 ? 250 : 400);
      try {
        const settings = forceAtlas2.inferSettings(g);
        forceAtlas2.assign(g, { iterations: iters, settings });
      } catch (_) { /* degenerate graph — leave random positions */ }
    }

    graph = g;
    hoverNode = null; neighbors = null;
    mountRenderer(g);
    renderCrumbs();
    renderLegend(data);
    renderStats(data);
    statusEl.textContent = statusLine(data);
    loading = false;
  }

  function mountRenderer(g) {
    if (renderer) { renderer.kill(); renderer = null; }
    renderer = new Sigma(g, document.getElementById("graph"), {
      renderLabels: showLabels,
      labelColor: { color: "#e5e7eb" },
      labelSize: 12,
      labelWeight: "600",
      labelRenderedSizeThreshold: 6,
      labelDensity: 0.9,
      labelGridCellSize: 70,
      defaultEdgeType: "curve",
      edgeProgramClasses: { curve: EdgeCurveProgram },
      minCameraRatio: 0.04,
      maxCameraRatio: 25,
      zIndex: true,
      nodeReducer: (node, attrs) => {
        const res = Object.assign({}, attrs);
        if (hoverNode) {
          if (node === hoverNode) {
            res.zIndex = 2; res.highlighted = true;
          } else if (neighbors && neighbors.has(node)) {
            res.zIndex = 1;
          } else {
            res.color = withAlpha(attrs.color, 0.15);
            res.label = null;
            res.zIndex = 0;
          }
        }
        if (!showLabels && !attrs._super) res.label = null;
        return res;
      },
      edgeReducer: (edge, attrs) => {
        const res = Object.assign({}, attrs);
        if (hoverNode) {
          const ext = graph.extremities(edge);
          if (ext[0] !== hoverNode && ext[1] !== hoverNode) res.hidden = true;
        }
        return res;
      },
    });

    renderer.on("enterNode", ({ node }) => {
      hoverNode = node;
      neighbors = new Set(graph.neighbors(node));
      neighbors.add(node);
      document.getElementById("graph").style.cursor =
        graph.getNodeAttribute(node, "_super") ? "pointer" : "default";
      showNodeInfo(node);
      renderer.refresh();
    });
    renderer.on("leaveNode", () => {
      hoverNode = null; neighbors = null;
      document.getElementById("graph").style.cursor = "default";
      renderer.refresh();
    });
    renderer.on("clickNode", ({ node }) => {
      const a = graph.getNodeAttributes(node);
      if (a._super) drillInto(node, a);
    });
  }

  function drillInto(nodeId, attrs) {
    // Context nodes carry the real cluster key/level to navigate to; their
    // display id is namespaced (ctx::…) and label prefixed with ↗.
    const key = attrs._context && attrs._navKey ? attrs._navKey : nodeId;
    const level = attrs._context && attrs._navLevel != null
      ? attrs._navLevel : attrs._level;
    const label = (attrs.label || key).replace(/^↗ /, "");
    nav.push({ focus: key, level, label });
    loadView(key, level);
  }
  function goTo(index) {
    if (index >= nav.length - 1) return;
    nav.length = index + 1;
    const top = nav[nav.length - 1];
    loadView(top.focus, top.level);
  }
  function goBack() { if (nav.length > 1) goTo(nav.length - 2); }

  function renderCrumbs() {
    crumbs.innerHTML = "";
    nav.forEach((entry, i) => {
      if (i > 0) {
        const sep = document.createElement("span");
        sep.textContent = "›"; sep.style.color = "#555";
        crumbs.appendChild(sep);
      }
      const b = document.createElement("button");
      b.textContent = entry.label;
      const last = i === nav.length - 1;
      b.style.cssText =
        "background:transparent;border:0;cursor:pointer;font-size:12px;" +
        "padding:2px 4px;border-radius:4px;max-width:26ch;overflow:hidden;" +
        "text-overflow:ellipsis;white-space:nowrap;" +
        (last ? "color:#ffe6cb;font-weight:600;" : "color:#9ca3af;");
      if (!last) b.onmouseenter = () => (b.style.color = "#e5e7eb");
      if (!last) b.onmouseleave = () => (b.style.color = "#9ca3af");
      b.onclick = () => goTo(i);
      crumbs.appendChild(b);
    });
  }

  function legendRow(id, a) {
    const row = document.createElement("div");
    row.className = "legend-item";
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = a.color;
    const label = document.createElement("span");
    label.className = "legend-label";
    label.textContent = a.label;
    label.title = a._src || a.label;
    const count = document.createElement("span");
    count.className = "legend-count";
    count.textContent = a._super ? (a._count || "") : "";
    row.appendChild(dot); row.appendChild(label); row.appendChild(count);
    row.onclick = () => { if (a._super) drillInto(id, a); else centerOn(id); };
    row.onmouseenter = () => {
      hoverNode = id; neighbors = new Set(graph.neighbors(id));
      neighbors.add(id); renderer && renderer.refresh();
    };
    row.onmouseleave = () => {
      hoverNode = null; neighbors = null; renderer && renderer.refresh();
    };
    return row;
  }

  function renderLegend(data) {
    const isLeaf = !!data.leaf;
    legendTitle.textContent = isLeaf ? "Symbols" : "Clusters";
    legendList.innerHTML = "";
    const all = graph.mapNodes((id, a) => ({ id, a }));
    // The group's own nodes (context/external refs listed separately below).
    const own = all.filter((r) => !r.a._context)
      .sort((x, y) => (y.a._count || y.a.size) - (x.a._count || x.a.size))
      .slice(0, 60);
    for (const { id, a } of own) legendList.appendChild(legendRow(id, a));

    // Option (b): "connects to" — the external clusters these leaves link
    // into, so an all-isolated group still shows where its edges go.
    const ctx = all.filter((r) => r.a._context)
      .sort((x, y) => (y.a.size || 0) - (x.a.size || 0));
    if (ctx.length) {
      const hdr = document.createElement("h3");
      hdr.textContent = "Connects to";
      hdr.style.cssText = "margin:16px 0 8px 0;";
      legendList.appendChild(hdr);
      for (const { id, a } of ctx) legendList.appendChild(legendRow(id, a));
    }
  }

  function centerOn(id) {
    if (!renderer) return;
    const disp = renderer.getNodeDisplayData(id);
    if (!disp) return;
    const cam = renderer.getCamera();
    cam.animate({ x: disp.x, y: disp.y, ratio: 0.35 }, { duration: 400 });
  }

  function renderStats(data) {
    const lvl = data.level != null ? data.level : 0;
    const nName = C4[Math.min(lvl, 3)];
    let s;
    if (data.leaf) {
      const lc = data.leaf_count != null ? data.leaf_count : graph.order;
      s = `${nName} · ${lc} symbols`;
      if (data.truncated) s += ` (top ${lc} of ${data.total_leaves} by connectivity)`;
      if (data.context_count) s += ` · linked to ${data.context_count} external clusters`;
    } else {
      s = `${nName} · ${graph.order} nodes · ${graph.size} edges`;
    }
    statsEl.textContent = s;
  }

  function statusLine(data) {
    const lvl = data.level != null ? data.level : 0;
    const where = data.focus ? (nav[nav.length - 1].label) : "root";
    return `${C4[Math.min(lvl, 3)]} · ${where}`;
  }

  function showNodeInfo(node) {
    const a = graph.getNodeAttributes(node);
    const name = (a.label || "").replace(/^↗ /, "");
    if (a._context) {
      statsEl.textContent =
        `${name} — external cluster (${a._count} symbols) · click to jump there`;
    } else if (a._super) {
      const kids = a._child > 0 ? `${a._child} sub-clusters` : "leaf symbols";
      statsEl.textContent =
        `${name} — ${a._count} symbols · click to open ${kids}`;
    } else {
      statsEl.textContent = `${name}${a._src ? " — " + a._src : ""}`;
    }
  }

  // -- toolbar ---------------------------------------------------------
  function cam() { return renderer && renderer.getCamera(); }
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  on("tb-zoom-in", () => cam() && cam().animatedZoom({ duration: 200 }));
  on("tb-zoom-out", () => cam() && cam().animatedUnzoom({ duration: 200 }));
  on("tb-fit", () => cam() && cam().animatedReset({ duration: 300 }));
  on("tb-reset", () => cam() && cam().animatedReset({ duration: 300 }));
  on("tb-focus", goBack);
  on("tb-pause", goBack);
  on("tb-labels", () => {
    showLabels = !showLabels;
    document.getElementById("tb-labels").classList.toggle("active", showLabels);
    if (renderer) { renderer.setSetting("renderLabels", showLabels); renderer.refresh(); }
  });
  on("tb-edges", () => {
    const b = document.getElementById("tb-edges");
    const hide = b.classList.toggle("off");
    b.classList.toggle("active", !hide);
    if (renderer) {
      renderer.setSetting("hideEdgesOnMove", false);
      renderer.setSetting("renderEdges", !hide);
      renderer.refresh();
    }
  });
  // Retitle the two repurposed buttons so hover text matches behavior.
  (function retitle() {
    const f = document.getElementById("tb-focus");
    const p = document.getElementById("tb-pause");
    if (f) f.title = "Back (up one level)";
    if (p) p.title = "Back (up one level)";
  })();

  hintEl.textContent =
    "Click a cluster to drill in · breadcrumb or ↑ button to go back · " +
    "scroll to zoom · drag to pan";

  // Optional deep-link: #focus=<key>&level=<n> opens straight at a cluster
  // (shareable, and lets a leaf/comm bucket load without clicking down).
  function initialFromHash() {
    const h = (location.hash || "").replace(/^#/, "");
    if (!h) return null;
    const p = new URLSearchParams(h);
    if (!p.has("focus")) return null;
    const focus = p.get("focus");
    const level = p.has("level") ? Number(p.get("level")) : 0;
    nav.push({ focus, level, label: focus });
    return { focus, level };
  }

  const start = initialFromHash();
  loadView(start ? start.focus : null, start ? start.level : null).catch(err => {
    statusEl.textContent = "Error loading graph: " + err.message;
  });
</script>
</body>
</html>"""


@router.get("/graphify-visual/{project_id}/hierarchy.json")
def _graphify_hierarchy(
    project_id: str,
    focus: str | None = Query(None),
    level: int | None = Query(None),
    cap: int = Query(graph_hier_index.DEFAULT_LEAF_CAP, ge=1, le=5000),
    ctx_level: int = Query(1, alias="ctx", ge=0, le=2),
    full: int = Query(0),
):
    """Hierarchical view of the project graph — server-side drill-down.

    The project hierarchy is L0 domains → L1 services → L2 modules →
    leaf symbols, keyed by path prefix (see compute_node_hierarchy).

    Drill-down (default) — the viewer fetches only the slice it renders:

        (no params)          -> L0 super-nodes (domains)
        focus=<key>&level=0  -> that domain's L1 children (or its leaves)
        focus=<key>&level=1  -> that service's L2 children (or its leaves)
        focus=<key>&level=2  -> that module's leaf symbols (capped by `cap`)

    Super-edges are pre-aggregated from leaf edges. `full=1` returns the
    legacy whole-graph payload (every leaf + edge) for offline/export use;
    on a large monorepo that is the 100MB+ response this drill-down exists
    to avoid, so it is opt-in only.
    """
    if not _SAFE_PROJECT_RE.match(project_id or ""):
        raise HTTPException(status_code=400, detail="invalid project id")
    ctx = get_project(project_id)
    json_path = ctx._data_dir / "graphify-src" / "graphify-out" / "graph.json"
    if not json_path.exists():
        raise HTTPException(
            status_code=404, detail="graph.json not generated yet"
        )

    # Pull DB-derived community labels so super-nodes that fall back to
    # comm:<id> at L0 (flat repos) get a human label instead of the raw id.
    db_path = ctx._data_dir / "graph.db"
    comm_labels: dict[int, str] = {}
    if db_path.exists():
        try:
            conn = sqlite3.connect(str(db_path))
            try:
                for r in conn.execute("SELECT id, label FROM communities"):
                    comm_labels[int(r[0])] = r[1] or ""
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        except sqlite3.Error:
            pass

    # Ultimate Graph narrative layer: enriched per-hierarchy-node names
    # ({l-path key: "Human Name"}) written by the background enrich worker.
    # The viewer prefers these over the path-derived super-node labels, so
    # domains/services/modules read as real names instead of path mash-ups.
    hierarchy_labels: dict = {}
    hierarchy_purposes: dict = {}
    try:
        for key, ann in ctx.graph_svc.annotations_for("hierarchy", "name").items():
            if ann.get("name"):
                hierarchy_labels[key] = ann["name"]
            if ann.get("purpose"):
                hierarchy_purposes[key] = ann["purpose"]
    except Exception:
        pass

    # Override the deterministic community labels (which collapse to
    # "prism service · …" in a single-service repo) with the inference-
    # derived names, so the L3 / symbol-level clusters read meaningfully.
    try:
        for cid_str, ann in ctx.graph_svc.annotations_for("community", "name").items():
            if ann.get("name"):
                try:
                    comm_labels[int(cid_str)] = ann["name"]
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass

    # Legacy whole-graph payload (opt-in). Kept for export / offline tools;
    # this is the very response the drill-down exists to avoid shipping by
    # default on large graphs.
    if full:
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            raise HTTPException(status_code=500, detail="graph.json parse error")
        raw_nodes = [
            n for n in data.get("nodes", []) if n.get("file_type") != "rationale"
        ]
        out_nodes = []
        for n in raw_nodes:
            h = compute_node_hierarchy(
                n.get("source_file"), fallback_community=n.get("community"))
            out_nodes.append({**n, "level": 3, **h})
        return JSONResponse({
            "nodes": out_nodes,
            "edges": data.get("links") or data.get("edges") or [],
            "community_labels": comm_labels,
            "hierarchy_labels": hierarchy_labels,
            "hierarchy_purposes": hierarchy_purposes,
        }, headers={"Cache-Control": "no-store"})

    # Merge label sources into one map keyed by super-node key. Enriched
    # hierarchy names win; comm:<id> keys resolve through the community table.
    labels = dict(hierarchy_labels)
    for cid, lbl in comm_labels.items():
        labels.setdefault(f"comm:{cid}", lbl)

    try:
        idx = graph_hier_index.get_index(project_id, json_path)
    except Exception:
        raise HTTPException(status_code=500, detail="graph.json parse error")

    payload = idx.build(focus, level, labels, cap=cap, context_level=ctx_level)
    payload.update({
        "community_labels": comm_labels,
        "hierarchy_labels": hierarchy_labels,
        "hierarchy_purposes": hierarchy_purposes,
    })
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})



@router.get("/graphify-visual/{project_id}/communities.json")
def _graphify_communities(project_id: str):
    """Serve DB-derived community labels for the viewer sidebar.

    Joins the `communities` label table with per-community node counts
    from `entities`, filtering out rationale entries so the counts
    match what the viewer actually renders client-side.
    """
    from fastapi.responses import JSONResponse
    if not _SAFE_PROJECT_RE.match(project_id or ""):
        raise HTTPException(status_code=400, detail="invalid project id")
    ctx = get_project(project_id)
    db_path = ctx._data_dir / "graph.db"
    if not db_path.exists():
        return JSONResponse({"communities": []})
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT e.community AS id, COUNT(*) AS n, "
                "       c.label AS label "
                "FROM entities e "
                "LEFT JOIN communities c ON c.id = e.community "
                "WHERE e.community IS NOT NULL "
                "  AND COALESCE(e.file_type,'') != 'rationale' "
                "GROUP BY e.community "
                "ORDER BY n DESC"
            ).fetchall()
        except sqlite3.OperationalError:
            rows = conn.execute(
                "SELECT community AS id, COUNT(*) AS n, NULL AS label "
                "FROM entities WHERE community IS NOT NULL "
                "GROUP BY community ORDER BY n DESC"
            ).fetchall()
        out = [
            {
                "id": int(r["id"]),
                "label": (r["label"] if "label" in r.keys() else None)
                         or f"community {r['id']}",
                "count": int(r["n"]),
            }
            for r in rows
        ]
    finally:
        conn.close()
    return JSONResponse({"communities": out})


@router.get("/graphify-visual/{project_id}/{filename}")
def _graphify_visual(project_id: str, filename: str):
    """Serve graph.json for the WebGL viewer. Project slug strictly
    validated to prevent path traversal. Declared after the specific
    communities.json route so literal filenames take precedence over
    this path-parameter fallback."""
    if not _SAFE_PROJECT_RE.match(project_id or ""):
        raise HTTPException(status_code=400, detail="invalid project id")
    if filename not in _ALLOWED_VISUAL_FILES:
        raise HTTPException(status_code=404, detail="not found")
    ctx = get_project(project_id)
    path = ctx._data_dir / "graphify-src" / "graphify-out" / filename
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="graph.json not generated yet")
    return FileResponse(str(path), media_type="application/json")


@router.get("/graph/viewer/{project_id}")
def _graph_viewer(project_id: str):
    """Sigma.js WebGL viewer for a project's graph.json.

    Phase 2 of #16 — handles 100K+ nodes by delegating rendering to
    the user's browser GPU instead of asking graphify to emit a
    possibly-rejected HTML blob. Container ships no graphics libs;
    all rendering happens client-side.
    """
    from fastapi.responses import HTMLResponse
    if not _SAFE_PROJECT_RE.match(project_id or ""):
        raise HTTPException(status_code=400, detail="invalid project id")
    html = _SIGMA_VIEWER_HTML.replace("__PROJECT_ID__", project_id)
    return HTMLResponse(content=html)
