// Way 1 — task ACTIVITY Gantt (bar-based, graphical, click-to-drill).
// Replaces the flat "sessions" list as the way to see WHAT HAPPENED on a task:
// real work sessions render as wall-time bars on a shared time axis; gate
// decisions render as honesty markers (real-verifier vs override) on their own
// lane. Synthetic gate-actor labels are NEVER drawn as bare session rows — they
// ARE the gate markers. Same bar visual language as the TokenTurns burn graph.
import { useState } from "react";
import { motion } from "motion/react";

export type GanttLane = {
  session_id: string;
  start: number; // epoch seconds
  end: number;
  duration_s: number;
  skills: number;
  live: boolean;
};
export type GanttGate = {
  gate: string; // "red" | "green" | "gate"
  ts: number;
  actor: string;
  override: boolean;
  verified: boolean;
  proof: string;
  reason: string;
};
export type Timeline = {
  window: { start: number; end: number };
  lanes: GanttLane[];
  gates: GanttGate[];
};

function clockHM(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}
function fmtDur(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}
function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

export default function TaskActivityGantt({
  timeline, reduced,
}: { timeline: Timeline; reduced?: boolean | null }) {
  const [open, setOpen] = useState<string | null>(null);
  const { start, end } = timeline.window;
  const span = Math.max(1, end - start);
  const pct = (ts: number) =>
    Math.max(0, Math.min(100, ((ts - start) / span) * 100));

  // axis ticks at 0/25/50/75/100% of the window
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => start + f * span);

  if (timeline.lanes.length === 0 && timeline.gates.length === 0) {
    return (
      <p className="text-[12px] opacity-40 italic py-3">
        No activity recorded yet.
      </p>
    );
  }

  return (
    <div className="mt-2 select-none">
      {/* time axis */}
      <div className="relative h-4 ml-[120px] border-b border-[color:var(--border-default)]/40">
        {ticks.map((t, i) => (
          <span
            key={i}
            className="absolute -translate-x-1/2 text-[9px] font-mono text-[color:var(--text-muted)] opacity-60"
            style={{ left: `${pct(t)}%` }}
          >
            {i === ticks.length - 1 ? "now" : clockHM(t)}
          </span>
        ))}
      </div>

      {/* session lanes */}
      {timeline.lanes.map((l) => {
        const left = pct(l.start);
        const width = Math.max(1.5, pct(l.end) - left);
        return (
          <div key={l.session_id} className="flex items-center h-11 gap-2">
            <button
              onClick={() => setOpen(open === l.session_id ? null : l.session_id)}
              className="w-[112px] shrink-0 text-right font-mono text-[10px] text-[color:var(--text-secondary)] truncate hover:opacity-100 opacity-80"
              title={l.session_id}
            >
              {shortId(l.session_id)}
            </button>
            <div className="relative flex-1 h-7">
              <motion.div
                className="absolute top-1/2 -translate-y-1/2 h-5 rounded-sm cursor-pointer"
                style={{
                  left: `${left}%`, width: `${width}%`,
                  background: l.live
                    ? "var(--accent-amber-fg)"
                    : "var(--accent-amber-bg)",
                  boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)",
                }}
                title={`${l.session_id} · ${fmtDur(l.duration_s)} · ${l.skills} skills${l.live ? " · live" : ""}`}
                onClick={() => setOpen(open === l.session_id ? null : l.session_id)}
                initial={false}
                animate={l.live && !reduced
                  ? { opacity: [1, 0.55, 1] } : { opacity: 1 }}
                transition={l.live && !reduced
                  ? { duration: 1.4, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
              />
            </div>
            <span className="w-[64px] shrink-0 text-[10px] font-mono text-[color:var(--text-muted)] tabular-nums">
              {fmtDur(l.duration_s)}{l.skills ? ` · ${l.skills}sk` : ""}
            </span>
          </div>
        );
      })}

      {/* gate marker lane */}
      <div className="flex items-center h-11 gap-2 mt-1">
        <span className="w-[112px] shrink-0 text-right font-mono text-[10px] text-[color:var(--text-muted)] uppercase tracking-wider">
          gates
        </span>
        <div className="relative flex-1 h-7">
          {timeline.gates.map((g, i) => {
            const color = g.override
              ? "var(--accent-amber-fg)" : "var(--accent-emerald-fg, #34d399)";
            return (
              <button
                key={i}
                onClick={() => setOpen(open === `g${i}` ? null : `g${i}`)}
                className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 grid place-items-center"
                style={{ left: `${pct(g.ts)}%` }}
                title={`${g.gate}_gate · ${g.override ? "OVERRIDE" : "verified"} · ${g.actor}${g.proof ? " · " + g.proof : ""}\n${g.reason}`}
              >
                <span
                  className="block h-3.5 w-3.5 rotate-45"
                  style={{ background: color, boxShadow: `0 0 5px ${color}` }}
                />
                <span
                  className="absolute -bottom-3 text-[8px] font-mono uppercase"
                  style={{ color }}
                >
                  {g.gate}{g.override ? "!" : "✓"}
                </span>
              </button>
            );
          })}
        </div>
        <span className="w-[64px] shrink-0" />
      </div>

      {/* drill-in detail for a clicked gate */}
      {open?.startsWith("g") && (() => {
        const g = timeline.gates[Number(open.slice(1))];
        if (!g) return null;
        return (
          <div className="ml-[120px] mt-4 text-[11px] leading-snug rounded bg-[color:var(--surface-2)]/50 p-2 border border-[color:var(--border-default)]/40">
            <span className="font-mono uppercase tracking-wider text-[color:var(--text-secondary)]">
              {g.gate}_gate · {g.override ? "⚠ override" : "✓ verified"}
            </span>
            <div className="mt-1 opacity-70">actor: <span className="font-mono">{g.actor || "—"}</span>{g.proof ? ` · proof: ${g.proof}` : ""}</div>
            <div className="mt-1 opacity-50 break-words">{g.reason}</div>
          </div>
        );
      })()}

      {/* legend */}
      <div className="ml-[120px] mt-5 flex flex-wrap gap-x-4 gap-y-1 text-[9px] font-mono text-[color:var(--text-muted)] opacity-70">
        <span><span className="inline-block h-2 w-3 align-middle rounded-sm" style={{ background: "var(--accent-amber-bg)" }} /> work session</span>
        <span><span className="inline-block h-2 w-2 align-middle rotate-45" style={{ background: "var(--accent-amber-fg)" }} /> gate ! override</span>
        <span><span className="inline-block h-2 w-2 align-middle rotate-45" style={{ background: "var(--accent-emerald-fg, #34d399)" }} /> gate ✓ verified</span>
      </div>
    </div>
  );
}
