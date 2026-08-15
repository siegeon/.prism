import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { useVersion } from "@/lib/version";
import { Page, ErrorBanner } from "@/components/ui";
import { GraphState } from "@/live/graphState";
import { draw } from "@/live/draw";
import type { GraphSnapshot, WorkEvent } from "@/live/types";

/** /live — "PRISM shows its work": every running task is a live card-node
 * on a deterministic circuit-board canvas (design directive in
 * E:\gamify-lab\DESIGN_DIRECTIVE.md). Boots from GET /api/work/graph, then
 * EventSource('/sse/work?project=') keeps it live: agent.run adds/updates
 * session cards with a wire connect, tokens.turn ghosts the Tokens row and
 * rides a packet along the wire, drive.heartbeat pulses the task card and
 * refills its Step capacity bar, task.changed ghosts the Step value and
 * updates status/gate. All rendering lives in src/live/* (layout, cards,
 * wires, packets, hud, idle) — this page owns only the DOM canvas, the two
 * data subscriptions, pan/zoom input, and the rAF loop. */
export default function LivePage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const version = useVersion();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef<GraphState>(new GraphState());
  const [error, setError] = useState<string | null>(null);

  // Pan/zoom drag bookkeeping (mutable ref — no need to re-render React
  // on every mousemove, the canvas repaints itself via rAF).
  const dragRef = useRef<{ dragging: boolean; moved: boolean; lastX: number; lastY: number }>(
    { dragging: false, moved: false, lastX: 0, lastY: 0 },
  );

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

  // Pan: drag empty space. Click on a card: first click selects, a
  // second click on an already-selected card navigates to its href.
  const onPointerDown = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    dragRef.current = { dragging: true, moved: false, lastX: ev.clientX, lastY: ev.clientY };
  }, []);

  const onPointerMove = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    if (!d.dragging) return;
    const dx = ev.clientX - d.lastX, dy = ev.clientY - d.lastY;
    if (Math.abs(dx) > 2 || Math.abs(dy) > 2) d.moved = true;
    if (d.moved) {
      const state = stateRef.current;
      state.pan.x -= dx / state.zoom;
      state.pan.y -= dy / state.zoom;
      d.lastX = ev.clientX;
      d.lastY = ev.clientY;
    }
  }, []);

  const onPointerUp = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    const wasDrag = d.moved;
    dragRef.current = { dragging: false, moved: false, lastX: 0, lastY: 0 };
    if (wasDrag) return;

    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const state = stateRef.current;
    const world = state.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
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
  }, [navigate]);

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
      <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] h-[calc(100vh-220px)] min-h-[420px] overflow-hidden">
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onWheel={onWheel}
          className="w-full h-full cursor-pointer touch-none"
        />
      </div>
    </Page>
  );
}
