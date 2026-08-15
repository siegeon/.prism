/** Pure canvas rendering for the /live graph. Reads GraphState, writes
 * pixels — no physics, no event handling. Kept separate from
 * graphState.ts so restyling the skeleton later never has to touch the
 * simulation/event-application logic. */

import type { GraphState, SimNode } from "./graphState";
import { radiusFor } from "./graphState";

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

/** Resolved once per draw() call (cheap: ~8 getPropertyValue reads at
 * 60fps) so a live theme toggle is picked up without a page reload. */
function resolveTheme() {
  return {
    edge: cssVar("--border-default", "#3a3f4b"),
    label: cssVar("--text-secondary", "#9aa1ad"),
    task: cssVar("--accent-slate-fg", "#7dd3fc"),
    subtask: cssVar("--accent-teal-fg", "#2dd4bf"),
    session: cssVar("--accent-violet-fg", "#a78bfa"),
    working: cssVar("--accent-emerald-fg", "#34d399"),
    gate: cssVar("--accent-amber-fg", "#fbbf24"),
    stalled: cssVar("--accent-rose-fg", "#fb7185"),
    packet: cssVar("--accent-emerald-fg", "#34d399"),
  };
}

function ringColorFor(n: SimNode, theme: ReturnType<typeof resolveTheme>): string | null {
  if (n.activity_state === "working" || n.activity_state === "driving") return theme.working;
  if (n.activity_state === "awaiting_gate" || n.gate_state === "pending") return theme.gate;
  if (n.activity_state === "stalled") return theme.stalled;
  return null;
}

function fillColorFor(n: SimNode, theme: ReturnType<typeof resolveTheme>): string {
  if (n.kind === "task") return theme.task;
  if (n.kind === "subtask") return theme.subtask;
  return theme.session;
}

export function draw(ctx: CanvasRenderingContext2D, state: GraphState, now: number): void {
  const { width, height } = state;
  ctx.clearRect(0, 0, width, height);
  const theme = resolveTheme();

  // Edges first, under everything.
  ctx.strokeStyle = theme.edge;
  ctx.lineWidth = 1;
  ctx.globalAlpha = 0.5;
  for (const l of state.links) {
    ctx.beginPath();
    ctx.moveTo(l.source.x ?? 0, l.source.y ?? 0);
    ctx.lineTo(l.target.x ?? 0, l.target.y ?? 0);
    ctx.stroke();
  }
  ctx.globalAlpha = 1;

  // Packets: dots travelling source(session/subtask) -> target(task).
  ctx.fillStyle = theme.packet;
  for (const p of state.packets) {
    const sx = p.link.source.x ?? 0, sy = p.link.source.y ?? 0;
    const tx = p.link.target.x ?? 0, ty = p.link.target.y ?? 0;
    const x = sx + (tx - sx) * p.t;
    const y = sy + (ty - sy) * p.t;
    ctx.beginPath();
    ctx.arc(x, y, 3, 0, Math.PI * 2);
    ctx.fill();
  }

  // Nodes.
  for (const n of state.nodes) {
    const x = n.x ?? 0, y = n.y ?? 0;
    const r = radiusFor(n);

    // Heartbeat pulse: an expanding, fading ring while pulseUntil is
    // still ahead of now (drive.heartbeat / agent.run set this).
    if (n.pulseUntil > now) {
      const remaining = n.pulseUntil - now; // 700ms -> 0
      const frac = 1 - remaining / 700;
      ctx.beginPath();
      ctx.arc(x, y, r + 4 + frac * 10, 0, Math.PI * 2);
      ctx.strokeStyle = theme.working;
      ctx.globalAlpha = Math.max(0, 1 - frac);
      ctx.lineWidth = 2;
      ctx.stroke();
      ctx.globalAlpha = 1;
    }

    const ring = ringColorFor(n, theme);
    if (ring) {
      ctx.beginPath();
      ctx.arc(x, y, r + 3, 0, Math.PI * 2);
      ctx.strokeStyle = ring;
      ctx.lineWidth = 2;
      ctx.stroke();
    }

    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = fillColorFor(n, theme);
    ctx.globalAlpha = n.kind === "session" ? 0.85 : 1;
    ctx.fill();
    ctx.globalAlpha = 1;

    if (n.kind !== "session") {
      ctx.fillStyle = theme.label;
      ctx.font = "11px system-ui, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(truncate(n.label, 24), x, y + r + 14);
    }
  }
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}
