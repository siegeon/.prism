import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { subscribeStream } from "@/lib/sharedStream";
import { useVersion } from "@/lib/version";
import { Page, ErrorBanner } from "@/components/ui";
import { GraphState } from "@/live/graphState";
import { draw } from "@/live/draw";
import { actionStripHitTest, exploreHrefFor } from "@/live/cards";
import type { GraphSnapshot, WorkEvent } from "@/live/types";
import LiveGatePanel from "@/components/live/LiveGatePanel";

/** localStorage key for a project's manual card-position overrides (owner
 * ask: "the individual panels should be able to be moved") — read once
 * after bootstrap (GraphState.hydrateOverrides), written on every drag
 * commit (pointerUp) and by the "reset layout" affordance. */
function positionsKey(project: string): string {
  return `prism.live.positions.${project}`;
}

/** localStorage key for a project's manual PORT placements (task
 * b9ce0450, owner correction 2026-08-16: "the owner can drag that port
 * dot anywhere they want on the pane... the placement survives a page
 * reload") -- same convention as positionsKey above, read once after
 * bootstrap (GraphState.hydratePortOverrides), written on port-drag
 * release and cleared by "reset layout". */
function portsKey(project: string): string {
  return `prism.live.ports.${project}`;
}

/** Screen-space px a pointer must travel past pointerdown before a press
 * converts into an actual pan or card drag — keeps a plain click/select
 * from being swallowed by 1px of jitter. */
const DRAG_THRESHOLD_PX = 5;

type DragMode = "none" | "pan" | "node" | "port";
type DragState = {
  mode: DragMode;
  moved: boolean;
  lastX: number;
  lastY: number;
  /** mode==="node" only: the id being dragged, and the world-space offset
   * from the pointer to the card's own x/y (so the card doesn't jump to
   * re-center under the cursor the instant the drag starts). mode==="port"
   * reuses nodeId for the CARD the dragged port is anchored to. */
  nodeId: string | null;
  offsetX: number;
  offsetY: number;
  /** mode==="port" only: which wire (wireKey) and which end ("from"/"to")
   * is being re-docked. */
  portKey: string | null;
  portEnd: "from" | "to" | null;
};

const IDLE_DRAG: DragState = {
  mode: "none", moved: false, lastX: 0, lastY: 0, nodeId: null, offsetX: 0, offsetY: 0,
  portKey: null, portEnd: null,
};

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
  // Task d56f3b25 (S3): which task's gate decision panel is open, mounted
  // in place (URL stays /live) -- never a navigate() to /tasks/<id>.
  const [gatePanelTaskId, setGatePanelTaskId] = useState<string | null>(null);
  // Only used to switch the CSS cursor (grabbing while a card drag is
  // live) — the canvas itself repaints via rAF, not React re-render.
  const [grabbing, setGrabbing] = useState(false);
  // task 763168f8: true while the pointer rests on a port handle (no drag
  // armed) — drives the cursor-grab affordance so the movable wires are
  // discoverable. React bails on same-value sets, so per-move cost is
  // one hit-test.
  const [hoverPort, setHoverPort] = useState(false);

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
        try {
          const rawPorts = localStorage.getItem(portsKey(project));
          if (rawPorts) stateRef.current.hydratePortOverrides(JSON.parse(rawPorts));
        } catch {
          // corrupt/unavailable storage — boot with auto-placed ports
        }
      })
      .catch((e) => { if (!cancel) setError(e instanceof Error ? e.message : String(e)); });
    return () => { cancel = true; };
  }, [project]);

  // Incremental push.
  useEffect(() => subscribeStream(
    `/sse/work?project=${encodeURIComponent(project)}`,
    (data) => {
      try {
        const event = JSON.parse(data) as WorkEvent;
        stateRef.current.applyEvent(event);
      } catch {
        // malformed frame — drop it, keep the stream alive
      }
    },
  ), [project]);

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

  // Mirrors persistOverrides — called on every port-drag release (AC-9);
  // the live placement is already in GraphState.portOverrides by then.
  const persistPortOverrides = useCallback(() => {
    try {
      localStorage.setItem(portsKey(project), JSON.stringify(stateRef.current.serializePortOverrides()));
    } catch {
      // storage full/unavailable — the drag still holds for this session
    }
  }, [project]);

  // The legend-area "reset layout" affordance: drop every manual override
  // (cards ease back to their deterministic slot) and forget the saved
  // copy so a reload doesn't resurrect it.
  const handleResetLayout = useCallback(() => {
    stateRef.current.clearAllOverrides();
    try {
      localStorage.removeItem(positionsKey(project));
      // AC-9: "reset layout" clears BOTH position overrides and any
      // manually-docked ports (portsKey), same as clearAllOverrides()
      // clears both maps in GraphState.
      localStorage.removeItem(portsKey(project));
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
      dragRef.current = { ...IDLE_DRAG, lastX: ev.clientX, lastY: ev.clientY };
      return;
    }

    // AC-7 / stop_if: a port dot sits ON the card edge that nodeAtWorld's
    // AABB already claims, so this check must run BEFORE nodeAtWorld (and
    // after the action-strip check, which floats outside the slot bounds
    // and already owns that region) -- this exact ordering is the whole
    // disambiguation between the new port gesture and card-drag/pan.
    const portHit = state.portAtWorld(world.x, world.y);
    if (portHit) {
      dragRef.current = {
        mode: "port", moved: false, lastX: ev.clientX, lastY: ev.clientY,
        nodeId: portHit.nodeId, offsetX: 0, offsetY: 0,
        portKey: portHit.key, portEnd: portHit.end,
      };
      return;
    }

    const node = state.nodeAtWorld(world.x, world.y);
    if (node) {
      dragRef.current = {
        mode: "node", moved: false, lastX: ev.clientX, lastY: ev.clientY,
        nodeId: node.id, offsetX: world.x - node.x, offsetY: world.y - node.y,
        portKey: null, portEnd: null,
      };
      return;
    }
    dragRef.current = { ...IDLE_DRAG, mode: "pan", lastX: ev.clientX, lastY: ev.clientY };
  }, []);

  const onPointerMove = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    if (d.mode === "none") {
      // task 763168f8: with no drag armed, hover the REAL port hit-test
      // (portAtWorld — the same slop the drag uses, mx-0a0bf4) so the
      // cursor says "grab" exactly where dragging actually works.
      const canvas = canvasRef.current;
      if (canvas) {
        const rect = canvas.getBoundingClientRect();
        const w = stateRef.current.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
        setHoverPort(!!stateRef.current.portAtWorld(w.x, w.y));
      }
      return;
    }
    const dx = ev.clientX - d.lastX, dy = ev.clientY - d.lastY;
    if (!d.moved && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
      d.moved = true;
      if (d.mode === "node" || d.mode === "port") setGrabbing(true);
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
    const canvas = canvasRef.current;
    if (!canvas || !d.nodeId) return;
    const rect = canvas.getBoundingClientRect();
    const world = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);

    if (d.mode === "port" && d.portKey && d.portEnd) {
      // The wire re-routes live during the drag (AC-6): setPortFromWorld
      // writes the override every move, and wireEndpointsFor (called from
      // draw.ts every frame) reads it straight back.
      state.draggingPortId = d.portKey;
      state.setPortFromWorld(d.portKey, d.portEnd, d.nodeId, world.x, world.y);
      state.noteUserCameraInput(now);
      d.lastX = ev.clientX;
      d.lastY = ev.clientY;
      return;
    }

    // mode === "node": the card follows the pointer in world space every
    // move — wires/packets/the action strip re-derive from n.x/n.y each
    // frame, so they carry along for free (see graphState.ts's step()).
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
    stateRef.current.draggingPortId = null;
    if (grabbing) setGrabbing(false);

    if (mode === "node" && wasDrag && nodeId) {
      // The live position is already in GraphState.overrides (every
      // pointermove wrote it) — release just commits it to localStorage
      // and suppresses the click/select/navigate path below.
      persistOverrides();
      return;
    }
    if (mode === "port") {
      // AC-6/AC-7: a drag commits the already-live override to
      // localStorage; an un-moved press on a dot (a plain click) is
      // swallowed here too, rather than falling through to select or
      // navigate the card underneath it.
      if (wasDrag) persistPortOverrides();
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
      if (hit === "gate") {
        // FR-2: mounts the panel in place, no route change -- the URL
        // stays /live.
        setGatePanelTaskId(selectedNode.id);
        return;
      }
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
  }, [navigate, grabbing, persistOverrides, persistPortOverrides]);

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
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <div className="relative rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] h-[calc(100vh-220px)] min-h-[420px] overflow-hidden">
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onWheel={onWheel}
          className={`w-full h-full touch-none ${grabbing ? "cursor-grabbing" : hoverPort ? "cursor-grab" : "cursor-pointer"}`}
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
        {gatePanelTaskId && (
          <LiveGatePanel
            taskId={gatePanelTaskId}
            project={project}
            onClose={() => setGatePanelTaskId(null)}
          />
        )}
      </div>
    </Page>
  );
}
