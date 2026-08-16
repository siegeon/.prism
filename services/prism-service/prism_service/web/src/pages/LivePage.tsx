import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { useVersion } from "@/lib/version";
import { Page, ErrorBanner } from "@/components/ui";
import { GraphState } from "@/live/graphState";
import { draw } from "@/live/draw";
import { actionStripHitTest, exploreHrefFor } from "@/live/cards";
import type { GraphSnapshot, WorkEvent } from "@/live/types";

/** localStorage key for a project's manual card-position overrides (owner
 * ask: "the individual panels should be able to be moved") — read once
 * after bootstrap (GraphState.hydrateOverrides), written on every drag
 * commit (pointerUp) and by the "reset layout" affordance. */
function positionsKey(project: string): string {
  return `prism.live.positions.${project}`;
}

/** Screen-space px a pointer must travel past pointerdown before a press
 * converts into an actual pan or card drag — keeps a plain click/select
 * from being swallowed by 1px of jitter. */
const DRAG_THRESHOLD_PX = 5;

type DragMode = "none" | "pan" | "node";
type DragState = {
  mode: DragMode;
  moved: boolean;
  lastX: number;
  lastY: number;
  /** mode==="node" only: the id being dragged, and the world-space offset
   * from the pointer to the card's own x/y (so the card doesn't jump to
   * re-center under the cursor the instant the drag starts). */
  nodeId: string | null;
  offsetX: number;
  offsetY: number;
};

const IDLE_DRAG: DragState = { mode: "none", moved: false, lastX: 0, lastY: 0, nodeId: null, offsetX: 0, offsetY: 0 };

/** /live — "PRISM shows its work": every running task is a live card-node
 * on a deterministic circuit-board canvas (design directive in
 * E:\gamify-lab\DESIGN_DIRECTIVE.md). Boots from GET /api/work/graph, then
 * EventSource('/sse/work?project=') keeps it live: agent.run adds/updates
 * session cards with a wire connect, tokens.turn ghosts the Tokens row and
 * rides a packet along the wire, drive.heartbeat pulses the task card and
 * refills its Step capacity bar, task.changed ghosts the Step value and
 * updates status/gate. All rendering lives in src/live/* (layout, cards,
 * wires, packets, hud, idle) — this page owns only the DOM canvas, the two
 * data subscriptions, pan/zoom + card-drag input, and the rAF loop.
 *
 * Pointer handling on the canvas: pointerdown either (a) hits a selected
 * card's docked action strip (checked first, never arms a drag), (b) hits
 * a card body (arms a potential card DRAG), or (c) hits empty space (arms
 * a potential PAN). Movement past DRAG_THRESHOLD_PX converts the arm into
 * the real thing; a card drag writes a live world-space override into
 * GraphState every move (wires/packets/the action strip already key off
 * the node's animated x/y, so they follow for free) and persists to
 * localStorage on release. An un-moved pointerup falls through to the
 * original click/select/navigate logic, unchanged. */
export default function LivePage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const version = useVersion();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef<GraphState>(new GraphState());
  const [error, setError] = useState<string | null>(null);
  // Only used to switch the CSS cursor (grabbing while a card drag is
  // live) — the canvas itself repaints via rAF, not React re-render.
  const [grabbing, setGrabbing] = useState(false);

  // Pan/card-drag bookkeeping (mutable ref — no need to re-render React on
  // every pointermove, the canvas repaints itself via rAF).
  const dragRef = useRef<DragState>({ ...IDLE_DRAG });

  // Boot snapshot. Also wires GraphState's self-heal fetcher (round 2,
  // piece 4 build item 4) to this same endpoint -- a debounced refetch
  // fires whenever GraphState notices its local state may have drifted
  // (a fresh placeholder, an unrecognized task.changed field), reusing
  // this one query rather than the page owning a second fetch path.
  useEffect(() => {
    let cancel = false;
    setError(null);
    const fetchSnapshot = () =>
      api.get<GraphSnapshot>(`/api/work/graph?project=${encodeURIComponent(project)}`);
    stateRef.current.setReconcileFetcher(fetchSnapshot);
    fetchSnapshot()
      .then((snap) => {
        if (cancel) return;
        const canvas = canvasRef.current;
        const w = canvas?.clientWidth || 800;
        const h = canvas?.clientHeight || 600;
        stateRef.current.bootstrap(snap, w, h);
        // Rehydrate any manual drag positions saved for this project —
        // stale ids (a card that no longer exists) are pruned inside
        // hydrateOverrides itself.
        try {
          const raw = localStorage.getItem(positionsKey(project));
          if (raw) stateRef.current.hydrateOverrides(JSON.parse(raw));
        } catch {
          // corrupt/unavailable storage — boot with the deterministic
          // layout, same as a first-ever visit
        }
      })
      .catch((e) => { if (!cancel) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancel = true; };
  }, [project]);

  // Incremental push.
  useEffect(() => {
    const es = new EventSource(`/sse/work?project=${encodeURIComponent(project)}`);
    es.onmessage = (m) => {
      try {
        const event = JSON.parse(m.data) as WorkEvent;
        stateRef.current.applyEvent(event);
      } catch {
        // malformed frame — drop it, keep the stream alive
      }
    };
    return () => es.close();
  }, [project]);

  // Canvas sizing — crisp at devicePixelRatio, backing store scaled, CSS
  // size unscaled, so lines/text stay sharp on hi-DPI displays.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver(() => {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      const ctx = canvas.getContext("2d");
      ctx?.setTransform(dpr, 0, 0, dpr, 0, 0);
      stateRef.current.resize(w, h);
    });
    ro.observe(canvas);
    return () => ro.disconnect();
  }, []);

  // rAF draw loop.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    let raf = 0;
    let last = performance.now();
    const versionLabel = version?.version || "";
    const frame = (now: number) => {
      const dt = now - last;
      last = now;
      stateRef.current.step(dt, now);
      draw(ctx, stateRef.current, now, versionLabel);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [version?.version]);

  useEffect(() => () => stateRef.current.destroy(), []);

  // Persists the current override map for `project` to localStorage —
  // called on every card-drag release (the live position is already in
  // GraphState.overrides by then; this just survives a reload).
  const persistOverrides = useCallback(() => {
    try {
      localStorage.setItem(positionsKey(project), JSON.stringify(stateRef.current.serializeOverrides()));
    } catch {
      // storage full/unavailable — the drag still holds for this session,
      // it just won't survive a reload
    }
  }, [project]);

  // The legend-area "reset layout" affordance: drop every manual override
  // (cards ease back to their deterministic slot) and forget the saved
  // copy so a reload doesn't resurrect it.
  const handleResetLayout = useCallback(() => {
    stateRef.current.clearAllOverrides();
    try {
      localStorage.removeItem(positionsKey(project));
    } catch {
      // ignore — nothing left to clean up if storage isn't available
    }
  }, [project]);

  // pointerdown arms exactly one of: action-strip click (mode "none" — a
  // strip hit is a click target, never a drag), a card DRAG (mode
  // "node"), or empty-space PAN (mode "pan"). Which one actually fires is
  // decided by whether/how far the pointer moves before release.
  const onPointerDown = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) { dragRef.current = { ...IDLE_DRAG }; return; }
    const rect = canvas.getBoundingClientRect();
    const state = stateRef.current;
    const world = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);

    // Action-strip hits are checked BEFORE node hits — a press that lands
    // on the strip must never be mistaken for the start of a card drag.
    const selectedNode = state.nodes.find((n) => n.selected);
    if (selectedNode && actionStripHitTest(selectedNode, world.x, world.y)) {
      dragRef.current = { mode: "none", moved: false, lastX: ev.clientX, lastY: ev.clientY, nodeId: null, offsetX: 0, offsetY: 0 };
      return;
    }

    const node = state.nodeAtWorld(world.x, world.y);
    if (node) {
      dragRef.current = {
        mode: "node", moved: false, lastX: ev.clientX, lastY: ev.clientY,
        nodeId: node.id, offsetX: world.x - node.x, offsetY: world.y - node.y,
      };
      return;
    }
    dragRef.current = { mode: "pan", moved: false, lastX: ev.clientX, lastY: ev.clientY, nodeId: null, offsetX: 0, offsetY: 0 };
  }, []);

  const onPointerMove = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    if (d.mode === "none") return;
    const dx = ev.clientX - d.lastX, dy = ev.clientY - d.lastY;
    if (!d.moved && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
      d.moved = true;
      if (d.mode === "node") setGrabbing(true);
    }
    if (!d.moved) return;

    const state = stateRef.current;
    const now = performance.now();
    if (d.mode === "pan") {
      state.pan.x -= dx / state.zoom;
      state.pan.y -= dy / state.zoom;
      state.noteUserCameraInput(now);
      d.lastX = ev.clientX;
      d.lastY = ev.clientY;
      return;
    }
    // mode === "node": the card follows the pointer in world space every
    // move — wires/packets/the action strip re-derive from n.x/n.y each
    // frame, so they carry along for free (see graphState.ts's step()).
    const canvas = canvasRef.current;
    if (!canvas || !d.nodeId) return;
    const rect = canvas.getBoundingClientRect();
    const world = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    state.draggingNodeId = d.nodeId;
    state.setOverride(d.nodeId, world.x - d.offsetX, world.y - d.offsetY);
    state.noteUserCameraInput(now);
    d.lastX = ev.clientX;
    d.lastY = ev.clientY;
  }, []);

  const onPointerUp = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    const wasDrag = d.moved;
    const mode = d.mode;
    const nodeId = d.nodeId;
    dragRef.current = { ...IDLE_DRAG };
    stateRef.current.draggingNodeId = null;
    if (grabbing) setGrabbing(false);

    if (mode === "node" && wasDrag && nodeId) {
      // The live position is already in GraphState.overrides (every
      // pointermove wrote it) — release just commits it to localStorage
      // and suppresses the click/select/navigate path below.
      persistOverrides();
      return;
    }
    if (wasDrag) return;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const state = stateRef.current;
    const world = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    // The docked action strip floats just below a SELECTED card's own
    // slot bounds, so it must be checked BEFORE nodeAtWorld (which only
    // hit-tests the card rectangle itself) -- "open" is the same href a
    // second click on the body already gives; "explore" is the new hop.
    const selectedNode = state.nodes.find((n) => n.selected);
    if (selectedNode) {
      const hit = actionStripHitTest(selectedNode, world.x, world.y);
      if (hit === "open") { navigate(selectedNode.href); return; }
      if (hit === "explore") { navigate(exploreHrefFor(selectedNode)); return; }
    }
    const node = state.nodeAtWorld(world.x, world.y);
    if (!node) {
      state.select(null);
      return;
    }
    if (node.selected) {
      navigate(node.href);
    } else {
      state.select(node.id);
    }
  }, [navigate, grabbing, persistOverrides]);

  const onWheel = useCallback((ev: React.WheelEvent<HTMLCanvasElement>) => {
    ev.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const state = stateRef.current;
    const before = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    const factor = Math.exp(-ev.deltaY * 0.001);
    state.zoom = Math.max(0.35, Math.min(2.2, state.zoom * factor));
    const after = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    state.pan.x += before.x - after.x;
    state.pan.y += before.y - after.y;
    state.noteUserCameraInput(performance.now());
  }, []);

  return (
    <Page>
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-[color:var(--text-primary)]">Live</h1>
        <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)]">
          PRISM shows its work — live agent activity
        </div>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <div className="relative rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] h-[calc(100vh-220px)] min-h-[420px] overflow-hidden">
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onWheel={onWheel}
          className={`w-full h-full touch-none ${grabbing ? "cursor-grabbing" : "cursor-pointer"}`}
        />
        {/* Sits just above the canvas-drawn legend chip (hud.ts's
            drawLegend(ctx, 22, height - 14), bottom-left) -- same slate/
            muted grammar as StepRail's own reset-style button, no new
            colors. The only escape hatch once a card's been dragged: it
            clears every manual override so the layout eases back to its
            deterministic slots. */}
        <button
          type="button"
          onClick={handleResetLayout}
          className="absolute left-[22px] bottom-[66px] text-2xs uppercase tracking-wider font-mono px-2 py-0.5 rounded border border-[color:var(--border-default)] text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)] bg-[color:var(--surface-1)]"
        >
          reset layout
        </button>
      </div>
    </Page>
  );
}
