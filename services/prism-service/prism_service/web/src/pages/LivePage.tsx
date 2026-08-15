import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, ErrorBanner } from "@/components/ui";
import { GraphState } from "@/live/graphState";
import { draw } from "@/live/draw";
import type { GraphSnapshot, WorkEvent } from "@/live/types";

/** /live — "PRISM shows its work": live agent activity flowing onto a
 * node graph. Boots from GET /api/work/graph (one snapshot: managed
 * tasks + subtasks + recently-active sessions), then EventSource(
 * '/sse/work?project=') keeps it live: agent.run adds/updates session
 * nodes, tokens.turn spawns a packet dot travelling session -> task
 * (speed proportional to tok_s), drive.heartbeat pulses the task node,
 * task.changed updates status/step/gate labels. Skeleton visuals only
 * (see live/draw.ts) — a later pass restyles it; the motion is real. */
export default function LivePage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const stateRef = useRef<GraphState>(new GraphState());
  const [error, setError] = useState<string | null>(null);
  const [booted, setBooted] = useState(false);

  // Boot snapshot.
  useEffect(() => {
    let cancel = false;
    setBooted(false);
    setError(null);
    api
      .get<GraphSnapshot>(`/api/work/graph?project=${encodeURIComponent(project)}`)
      .then((snap) => {
        if (cancel) return;
        const canvas = canvasRef.current;
        const w = canvas?.clientWidth || 800;
        const h = canvas?.clientHeight || 600;
        stateRef.current.bootstrap(snap, w, h);
        setBooted(true);
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

  // Canvas sizing.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver(() => {
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = w; canvas.height = h;
      stateRef.current.resize(w, h);
    });
    ro.observe(canvas);
    return () => ro.disconnect();
  }, []);

  // rAF draw loop — independent of the d3-force tick so packet motion
  // stays smooth even between simulation ticks.
  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    let raf = 0;
    let last = performance.now();
    const frame = (now: number) => {
      const dt = now - last;
      last = now;
      stateRef.current.step(dt);
      draw(ctx, stateRef.current, now);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [booted]);

  useEffect(() => () => stateRef.current.destroy(), []);

  const onClick = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const x = ev.clientX - rect.left, y = ev.clientY - rect.top;
    const node = stateRef.current.nodeAt(x, y);
    if (node) navigate(node.href);
  }, [navigate]);

  return (
    <Page>
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold text-[color:var(--text-primary)]">Live</h1>
        <div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)]">
          PRISM shows its work — live agent activity
        </div>
      </div>
      {error && <ErrorBanner>{error}</ErrorBanner>}
      <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-1)] h-[calc(100vh-220px)] min-h-[420px]">
        <canvas
          ref={canvasRef}
          onClick={onClick}
          className="w-full h-full cursor-pointer"
        />
      </div>
    </Page>
  );
}
