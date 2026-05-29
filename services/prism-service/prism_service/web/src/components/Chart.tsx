/**
 * Dashboard charting primitives. The dashboard is the change-over-time
 * "pulse" surface, so it leans on @observablehq/plot (already a dep) for
 * the timelines and a dependency-free inline-SVG Sparkline for the small
 * trend cards. Everything is themed against the Hermes accent tokens so
 * the charts speak the same color language as the rest of the SPA.
 */
import { useEffect, useRef } from "react";
import * as Plot from "@observablehq/plot";

/** Accent foreground hexes mirrored from index.css --accent-*-fg, so
 *  Plot marks (which can't read CSS vars) match the pill/legend palette. */
export const TONE = {
  teal: "#5eead4",
  sage: "#a3d9a5",
  amber: "#fcd34d",
  violet: "#c4b5fd",
  emerald: "#6ee7b7",
  rose: "#f9a8d4",
} as const;

/** Shared dark-theme Plot defaults: transparent canvas, muted axes. */
export const plotBase = {
  style: { background: "transparent", color: "#8b97ad", fontSize: "10px", overflow: "visible" },
};

/** Renders an Observable Plot spec into a div. Parents should useMemo the
 *  `options` so the figure only re-renders when its data actually changes. */
export function PlotFigure({ options, className }: { options: Plot.PlotOptions; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const fig = Plot.plot(options);
    el.append(fig);
    return () => { fig.remove(); };
  }, [options]);
  return <div ref={ref} className={className} />;
}

/** Tiny axis-less trend line for KPI cards. Pure SVG — no Plot overhead. */
export function Sparkline({ data, color, width = 116, height = 32 }: {
  data: number[]; color: string; width?: number; height?: number;
}) {
  if (!data.length) return null;
  const max = Math.max(1, ...data);
  const dx = data.length > 1 ? width / (data.length - 1) : 0;
  const pts = data.map((v, i) => [i * dx, height - (v / max) * (height - 4) - 2] as const);
  const line = pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  return (
    <svg width={width} height={height} className="overflow-visible">
      <path d={`${line} L${width} ${height} L0 ${height} Z`} fill={color} opacity={0.12} />
      <path d={line} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" />
      <circle cx={last[0]} cy={last[1]} r={2.2} fill={color} />
    </svg>
  );
}
