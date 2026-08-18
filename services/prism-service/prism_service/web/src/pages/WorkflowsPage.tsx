import { useCallback, useEffect, useRef, useState } from "react";
import { useProject } from "@/lib/project";
import { fetchWorkflowDef } from "@/lib/useWorkflowDef";
import { Page, ErrorBanner } from "@/components/ui";
import { WorkflowGraph, drawWorkflows } from "@/live/workflowGraph";
import type { WireEnd } from "@/live/workflowWires";
import type { Point, WirePort } from "@/live/wires";

/** /workflows — the conductor's FSM and the bots that drive it, per project.
 *
 * In PRISM a workflow IS a bot: an FSM that agentically interacts with the
 * conductor's FSM. Both already exist server-side, so this section stores
 * NOTHING of its own — it is a view assembled from GET /api/workflows (the
 * step list off models/workflow.py, the bots off ROLE_CARDS, and occupancy
 * counted from the task rows the board already keeps).
 *
 * Structurally this is LivePage's shape minus the live wire: it owns the DOM
 * canvas, the rAF loop, and pan/zoom/drag; all geometry and drawing live in
 * live/workflowGraph.ts. The FSM changes only on deploy and occupancy moves
 * on the scale of a task transition, so a 10s poll is the honest refresh
 * here — an SSE subscription would be a stream with nothing to say.
 */

function positionsKey(project: string): string {
  return `prism.workflows.positions.${project}`;
}

/** Re-docked wire ends, keyed `<wire>:from` / `<wire>:to` — the same shape
 * /live persists under prism.live.ports.<project>. */
function portsKey(project: string): string {
  return `prism.workflows.ports.${project}`;
}

/** Mid-path bends, per wire. Client state like positions: the canvas is a
 * VIEW, so how the owner arranged it is theirs, not the service's. */
function waypointsKey(project: string): string {
  return `prism.workflows.waypoints.${project}`;
}

/** Reads one saved blob, tolerating absent/corrupt/blocked storage — a bad
 * entry means "boot from the deterministic layout", never a broken page. */
function readJson<T>(key: string, apply: (raw: T) => void): void {
  try {
    const raw = localStorage.getItem(key);
    if (raw) apply(JSON.parse(raw) as T);
  } catch {
    // corrupt or unavailable storage — fall through to defaults
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage full/unavailable — the edit holds for this session only
  }
}

/** Screen-space px a pointer must travel before a press becomes a pan or a
 * node drag — keeps a plain click from being swallowed by a pixel of jitter. */
const DRAG_THRESHOLD_PX = 5;
const POLL_MS = 10_000;

type DragState = {
  mode: "none" | "pan" | "node" | "port" | "waypoint";
  moved: boolean;
  lastX: number;
  lastY: number;
  nodeId: string | null;
  offsetX: number;
  offsetY: number;
  /** mode "port"/"waypoint": which wire is being edited, and which end or
   * which bend of it. */
  wireKey: string | null;
  wireEnd: WireEnd | null;
  waypointIndex: number;
};

const IDLE_DRAG: DragState = {
  mode: "none", moved: false, lastX: 0, lastY: 0, nodeId: null, offsetX: 0, offsetY: 0,
  wireKey: null, wireEnd: null, waypointIndex: -1,
};

export default function WorkflowsPage() {
  const [project] = useProject();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const graphRef = useRef<WorkflowGraph>(new WorkflowGraph());
  const dragRef = useRef<DragState>({ ...IDLE_DRAG });
  const [error, setError] = useState<string | null>(null);
  const [grabbing, setGrabbing] = useState(false);

  // Definition + live occupancy, polled. Re-applying a payload only refreshes
  // counts and which wires read live; node geometry is derived, so a poll
  // never disturbs a layout the owner has dragged.
  useEffect(() => {
    let cancel = false;
    let timer = 0;
    const load = () => {
      fetchWorkflowDef(project)
        .then((def) => {
          if (cancel) return;
          setError(null);
          graphRef.current.setDef(def);
          // Wire edits rehydrate AFTER setDef: both maps are keyed by wire,
          // and the wire list only exists once a definition has landed.
          // Unknown keys are dropped inside the editor.
          const known = graphRef.current.wireKeys();
          readJson<Record<string, WirePort>>(portsKey(project), (raw) =>
            graphRef.current.editor.hydratePorts(raw, known));
          readJson<Record<string, Point[]>>(waypointsKey(project), (raw) =>
            graphRef.current.editor.hydrateWaypoints(raw, known));
          const canvas = canvasRef.current;
          graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600);
        })
        .catch((e) => { if (!cancel) setError(e instanceof Error ? e.message : String(e)); });
    };
    // Rehydrate before the first paint so dragged nodes never visibly snap
    // from their default slot to the saved one.
    readJson<Record<string, Point>>(positionsKey(project),
      (raw) => graphRef.current.hydrateOverrides(raw));
    load();
    timer = window.setInterval(load, POLL_MS);
    return () => { cancel = true; window.clearInterval(timer); };
  }, [project]);

  // Crisp at devicePixelRatio: backing store scaled, CSS size unscaled.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver(() => {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.getContext("2d")?.setTransform(dpr, 0, 0, dpr, 0, 0);
      graphRef.current.fit(w, h);
    });
    ro.observe(canvas);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    let raf = 0;
    let last = performance.now();
    const frame = (now: number) => {
      const dt = now - last;
      last = now;
      graphRef.current.step(dt, now);
      drawWorkflows(ctx, graphRef.current, canvas.clientWidth, canvas.clientHeight, now);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  const persist = useCallback(() => {
    writeJson(positionsKey(project), graphRef.current.serializeOverrides());
  }, [project]);

  const persistWires = useCallback(() => {
    const { editor } = graphRef.current;
    writeJson(portsKey(project), editor.serializePorts());
    writeJson(waypointsKey(project), editor.serializeWaypoints());
  }, [project]);

  const handleReset = useCallback(() => {
    graphRef.current.clearOverrides();
    const canvas = canvasRef.current;
    graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600, true);
    try {
      // Positions AND wire edits — a reset that left wires bent would only
      // half-work, which is worse than no escape hatch at all.
      localStorage.removeItem(positionsKey(project));
      localStorage.removeItem(portsKey(project));
      localStorage.removeItem(waypointsKey(project));
    } catch {
      // nothing to clean up if storage isn't available
    }
  }, [project]);

  // Escape lets go of the selected wire — the keyboard half of "click
  // empty space to deselect".
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") graphRef.current.editor.selected = null;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const onPointerDown = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) { dragRef.current = { ...IDLE_DRAG }; return; }
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    const base = { moved: false, lastX: ev.clientX, lastY: ev.clientY };

    // Order matters and is the whole disambiguation. The selected wire's
    // own handles win first (a waypoint and a port dot both sit ON top of
    // things that would otherwise claim the press), then nodes, then any
    // wire body, then empty space.
    const waypoint = g.waypointAtWorld(world.x, world.y);
    if (waypoint) {
      dragRef.current = {
        ...IDLE_DRAG, ...base, mode: "waypoint",
        wireKey: waypoint.key, waypointIndex: waypoint.index,
      };
      return;
    }
    const port = g.portAtWorld(world.x, world.y);
    if (port) {
      dragRef.current = {
        ...IDLE_DRAG, ...base, mode: "port",
        wireKey: port.key, wireEnd: port.end, nodeId: port.nodeId,
      };
      return;
    }
    const node = g.nodeAtWorld(world.x, world.y);
    if (node) {
      dragRef.current = {
        ...IDLE_DRAG, ...base, mode: "node", nodeId: node.id,
        offsetX: world.x - node.slot.x, offsetY: world.y - node.slot.y,
      };
      return;
    }
    // A press on a wire selects it outright — no drag threshold, because
    // selecting is what makes its handles appear to aim at next.
    const wire = g.wireAtWorld(world.x, world.y);
    g.editor.selected = wire ? wire.key : null;
    dragRef.current = { ...IDLE_DRAG, ...base, mode: wire ? "none" : "pan" };
  }, []);

  const onPointerMove = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    if (d.mode === "none") return;
    const dx = ev.clientX - d.lastX, dy = ev.clientY - d.lastY;
    if (!d.moved && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
      d.moved = true;
      setGrabbing(true);
    }
    if (!d.moved) return;

    const g = graphRef.current;
    if (d.mode === "pan") {
      g.pan.x -= dx / g.zoom;
      g.pan.y -= dy / g.zoom;
      d.lastX = ev.clientX;
      d.lastY = ev.clientY;
      return;
    }

    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);

    if (d.mode === "node" && d.nodeId) {
      g.setOverride(d.nodeId, world.x - d.offsetX, world.y - d.offsetY);
    } else if (d.mode === "port" && d.wireKey && d.wireEnd) {
      // The wire re-routes live under the cursor: setPortFromWorld writes
      // the override every move and legs() reads it straight back.
      const wire = g.wire(d.wireKey);
      const slot = wire && g.slotForEnd(wire, d.wireEnd);
      if (slot) g.editor.setPortFromWorld(d.wireKey, d.wireEnd, slot, world.x, world.y);
    } else if (d.mode === "waypoint" && d.wireKey) {
      g.editor.moveWaypoint(d.wireKey, d.waypointIndex, world.x, world.y);
    }
    d.lastX = ev.clientX;
    d.lastY = ev.clientY;
  }, []);

  const onPointerUp = useCallback(() => {
    const d = dragRef.current;
    dragRef.current = { ...IDLE_DRAG };
    if (grabbing) setGrabbing(false);
    if (!d.moved) return;
    if (d.mode === "node") { persist(); return; }
    if (d.mode === "waypoint" && d.wireKey) {
      // Dropped back onto the straight run between its neighbours, the
      // bend isn't bending anything — it removes itself rather than
      // lingering as an invisible handle. That IS the remove gesture.
      const g = graphRef.current;
      const wire = g.wire(d.wireKey);
      const pts = wire ? g.route(wire) : [];
      if (pts.length >= 2) {
        g.editor.pruneIfStraightened(d.wireKey, d.waypointIndex, pts[0], pts[pts.length - 1]);
      }
    }
    if (d.mode === "port" || d.mode === "waypoint") persistWires();
  }, [grabbing, persist, persistWires]);

  /** Double-click is the bend gesture: on a placed waypoint it removes it,
   * anywhere else along the selected wire it inserts one at the hop that
   * was clicked. */
  const onDoubleClick = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);

    const waypoint = g.waypointAtWorld(world.x, world.y);
    if (waypoint) {
      g.editor.removeWaypoint(waypoint.key, waypoint.index);
      persistWires();
      return;
    }
    const wire = g.wireAtWorld(world.x, world.y);
    if (!wire) return;
    g.editor.selected = wire.key;
    const leg = g.legAtWorld(wire, world.x, world.y);
    if (!leg) return;
    g.editor.insertWaypoint(wire.key, leg.leg, leg.point);
    persistWires();
  }, [persistWires]);

  const onWheel = useCallback((ev: React.WheelEvent<HTMLCanvasElement>) => {
    ev.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const before = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    g.zoom = Math.max(0.35, Math.min(2.2, g.zoom * Math.exp(-ev.deltaY * 0.001)));
    const after = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    g.pan.x += before.x - after.x;
    g.pan.y += before.y - after.y;
  }, []);

  return (
    <Page>
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-[color:var(--text-primary)]">Workflows</h1>
        <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)]">
          The conductor pipeline and the bots that drive it
        </div>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <div className="relative rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] h-[calc(100vh-220px)] min-h-[420px] overflow-hidden">
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onDoubleClick={onDoubleClick}
          onWheel={onWheel}
          className={`w-full h-full touch-none ${grabbing ? "cursor-grabbing" : "cursor-pointer"}`}
        />
        <button
          type="button"
          onClick={handleReset}
          className="absolute left-[22px] bottom-[22px] text-2xs uppercase tracking-wider font-mono px-2 py-0.5 rounded border border-[color:var(--border-default)] text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)] bg-[color:var(--surface-1)]"
        >
          reset layout
        </button>
      </div>
    </Page>
  );
}
