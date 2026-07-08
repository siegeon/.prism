import { useEffect, useMemo, useRef, useState } from "react";

// Visual REPLAY of a task's journey through the conductor SDLC (task
// e825e00a). Reads the real /api/tasks/:id history (advance_task +
// gate_decide rows) and plays it back on the 10-step ladder so you can
// SEE what happened: which step was worked when, by whom, and how each
// gate resolved. No new data — pure replay of the recorded audit log.

const STEPS = [
  { id: "review_previous_notes", label: "Review notes", role: "Steward", gate: false },
  { id: "draft_story", label: "Draft story", role: "Steward", gate: false },
  { id: "story_gate", label: "Story gate", role: "", gate: true },
  { id: "verify_plan", label: "Verify plan", role: "Steward", gate: false },
  { id: "plan_gate", label: "Plan gate", role: "", gate: true },
  { id: "write_failing_tests", label: "Write tests", role: "Verifier", gate: false },
  { id: "red_gate", label: "Red gate", role: "", gate: true },
  { id: "implement_tasks", label: "Implement", role: "Builder", gate: false },
  { id: "verify_green_state", label: "Verify green", role: "Verifier", gate: false },
  { id: "green_gate", label: "Green gate", role: "", gate: true },
] as const;

const IDX: Record<string, number> = Object.fromEntries(STEPS.map((s, i) => [s.id, i]));

type Row = { actor?: string; action?: string; details?: string; timestamp?: string };

// One replay frame = a recorded transition, resolved to which step is
// "current" after it and what happened (advance vs gate approve/reject).
export type Frame = {
  t?: string;
  stepIndex: number;
  actor: string;
  kind: "advance" | "gate";
  decision?: string; // approve | reject (gate frames)
  label: string;
};

function grab(re: RegExp, d: string): string | undefined {
  return re.exec(d)?.[1]?.trim();
}

// Build the ordered frame list from the raw history rows.
export function framesFromHistory(rows: Row[]): Frame[] {
  const out: Frame[] = [];
  let cur = -1;
  for (const r of rows) {
    const d = r.details ?? "";
    if (r.action === "advance_task") {
      const to = grab(/(?:^|;\s*)to=([^;]*)/, d);
      if (to && to in IDX) cur = IDX[to];
      out.push({ t: r.timestamp, stepIndex: cur, actor: r.actor || "conductor",
        kind: "advance", label: `advanced to ${to ?? "?"}` });
    } else if (r.action === "gate_decide") {
      const gate = grab(/(?:^|;\s*)gate=([^;]*)/, d);
      const act = grab(/(?:^|;\s*)action=([^;]*)/, d) ?? "";
      if (gate && gate in IDX) cur = IDX[gate];
      out.push({ t: r.timestamp, stepIndex: cur, actor: r.actor || "?",
        kind: "gate", decision: act, label: `gate ${gate ?? "?"} ${act}` });
    }
  }
  return out;
}

function clockOf(ts?: string): string {
  return ts ? String(ts).slice(11, 19) : "";
}

export default function StateReplay({ history }: { history: Row[] }) {
  const frames = useMemo(() => framesFromHistory(history), [history]);
  const [i, setI] = useState(0);
  const [playing, setPlaying] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Clamp to the latest frame whenever new history streams in.
  useEffect(() => { setI((n) => Math.min(n, Math.max(0, frames.length - 1))); }, [frames.length]);

  useEffect(() => {
    if (!playing) return;
    if (i >= frames.length - 1) { setPlaying(false); return; }
    timer.current = setTimeout(() => setI((n) => n + 1), 1100);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [playing, i, frames.length]);

  if (frames.length === 0) {
    return <div className="text-[12px] opacity-50">No conductor transitions recorded yet — nothing to replay.</div>;
  }

  const f = frames[Math.min(i, frames.length - 1)];
  const rejected = f.kind === "gate" && f.decision === "reject";

  return (
    <div>
      {/* current-frame caption */}
      <div className="flex items-center gap-2 flex-wrap text-[12px] mb-3">
        <span className="font-mono opacity-70">{clockOf(f.t)}</span>
        <span
          className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded"
          style={{
            background: `var(--accent-${f.kind === "gate" ? (rejected ? "rose" : "amber") : "teal"}-bg)`,
            color: `var(--accent-${f.kind === "gate" ? (rejected ? "rose" : "amber") : "teal"}-fg)`,
          }}
        >
          {f.kind}
        </span>
        <span className="opacity-85">{f.label}</span>
        <span className="opacity-50 font-mono text-[11px]">· {f.actor}</span>
      </div>

      {/* step ladder */}
      <div className="flex items-start gap-1 overflow-x-auto pb-2">
        {STEPS.map((s, idx) => {
          const done = idx < f.stepIndex;
          const active = idx === f.stepIndex;
          const bg = done ? "var(--accent-teal-bg)" : active
            ? (s.gate ? "var(--accent-amber-bg)" : "var(--accent-teal-bg)") : "var(--surface-2)";
          const fg = done ? "var(--accent-teal-fg)" : active
            ? (s.gate ? "var(--accent-amber-fg)" : "var(--accent-teal-fg)") : "var(--text-secondary)";
          return (
            <div key={s.id} className="flex flex-col items-center min-w-[64px] text-center">
              <div
                className="h-7 w-7 rounded-full flex items-center justify-center text-[10px] font-mono"
                style={{
                  background: bg, color: fg,
                  boxShadow: active ? `0 0 0 2px ${fg}` : "inset 0 0 0 1px var(--surface-3)",
                  transform: s.gate ? "rotate(45deg)" : "none",
                }}
              >
                <span style={{ transform: s.gate ? "rotate(-45deg)" : "none" }}>
                  {done ? "✓" : s.gate ? "G" : idx + 1}
                </span>
              </div>
              <span className="text-[9px] leading-tight mt-1 opacity-70">{s.label}</span>
              {s.role && <span className="text-[8px] uppercase tracking-wide opacity-40">{s.role}</span>}
            </div>
          );
        })}
      </div>

      {/* transport */}
      <div className="flex items-center gap-2 mt-3">
        <button
          type="button"
          onClick={() => { if (i >= frames.length - 1) setI(0); setPlaying((p) => !p); }}
          className="text-[11px] uppercase tracking-wider px-3 py-1.5 rounded"
          style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
        >
          {playing ? "Pause" : i >= frames.length - 1 ? "Replay" : "Play"}
        </button>
        <button type="button" onClick={() => { setPlaying(false); setI((n) => Math.max(0, n - 1)); }}
          className="text-[11px] px-2.5 py-1.5 rounded bg-[color:var(--midground-base)]/15">◀</button>
        <button type="button" onClick={() => { setPlaying(false); setI((n) => Math.min(frames.length - 1, n + 1)); }}
          className="text-[11px] px-2.5 py-1.5 rounded bg-[color:var(--midground-base)]/15">▶</button>
        <input
          type="range" min={0} max={frames.length - 1} value={i}
          onChange={(e) => { setPlaying(false); setI(Number(e.target.value)); }}
          className="flex-1 accent-[color:var(--accent-teal-fg)]"
        />
        <span className="text-[11px] font-mono opacity-50 shrink-0">{i + 1}/{frames.length}</span>
      </div>
    </div>
  );
}
