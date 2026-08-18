import { useCallback, useEffect, useRef, useState } from "react";
import { useProject } from "@/lib/project";
import { fetchWorkflowDef } from "@/lib/useWorkflowDef";
import { Page, ErrorBanner } from "@/components/ui";
import { WorkflowGraph, drawWorkflows } from "@/live/workflowGraph";

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

/** Screen-space px a pointer must travel before a press becomes a pan or a
 * node drag — keeps a plain click from being swallowed by a pixel of jitter. */
const DRAG_THRESHOLD_PX = 5;
const POLL_MS = 10_000;

type DragState = {
  mode: "none" | "pan" | "node";
  moved: boolean;
  lastX: number;
  lastY: number;
  nodeId: string | null;
  offsetX: number;
  offsetY: number;
};

const IDLE_DRAG: DragState = {
  mode: "none", moved: false, lastX: 0, lastY: 0, nodeId: null, offsetX: 0, offsetY: 0,
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
          const canvas = canvasRef.current;
          graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600);
        })
        .catch((e) => { if (!cancel) setError(e instanceof Error ? e.message : String(e)); });
    };
    // Rehydrate before the first paint so dragged nodes never visibly snap
    // from their default slot to the saved one.
    try {
      const raw = localStorage.getItem(positionsKey(project));
      if (raw) graphRef.current.hydrateOverrides(JSON.parse(raw));
    } catch {
      // corrupt/unavailable storage — boot with the deterministic layout
    }
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
    try {
      localStorage.setItem(
        positionsKey(project),
        JSON.stringify(graphRef.current.serializeOverrides()),
      );
    } catch {
      // storage full/unavailable — the drag holds for this session only
    }
  }, [project]);

  const handleReset = useCallback(() => {
    graphRef.current.clearOverrides();
    const canvas = canvasRef.current;
    graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600, true);
    try {
      localStorage.removeItem(positionsKey(project));
    } catch {
      // nothing to clean up if storage isn't available
    }
  }, [project]);

  const onPointerDown = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) { dragRef.current = { ...IDLE_DRAG }; return; }
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    const node = g.nodeAtWorld(world.x, world.y);
    dragRef.current = node
      ? {
          mode: "node", moved: false, lastX: ev.clientX, lastY: ev.clientY,
          nodeId: node.id, offsetX: world.x - node.slot.x, offsetY: world.y - node.slot.y,
        }
      : { ...IDLE_DRAG, mode: "pan", lastX: ev.clientX, lastY: ev.clientY };
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
    } else if (d.nodeId) {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
      g.setOverride(d.nodeId, world.x - d.offsetX, world.y - d.offsetY);
    }
    d.lastX = ev.clientX;
    d.lastY = ev.clientY;
  }, []);

  const onPointerUp = useCallback(() => {
    const d = dragRef.current;
    dragRef.current = { ...IDLE_DRAG };
    if (grabbing) setGrabbing(false);
    if (d.mode === "node" && d.moved) persist();
  }, [grabbing, persist]);

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
