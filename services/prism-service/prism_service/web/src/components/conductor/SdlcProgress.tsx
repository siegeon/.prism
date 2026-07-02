// Segmented 8-step SDLC progress bar (task a5e0d9f5). Renders a LIVE, frame-by
// -frame estimate from the server `phase_progress` field: between the 5s polls
// the current-step fill is EXTRAPOLATED from wall-clock time so the bar creeps,
// the % readout ticks in decimals, and a mm:ss in-step timer counts up every
// second. The state pill reflects the REAL task status (in progress / done /
// blocked) — NOT a guessed activity ping — with a shimmer + pulse while the
// task is actively in progress. Completed steps solid (emerald); future muted.
import { useEffect, useRef, useState } from "react";
import { motion, useMotionValue, useTransform, useAnimationFrame } from "motion/react";
// useSpring tweens the current-step fill between 5s polls; its config keys off
// `reduced` so prefers-reduced-motion collapses the tween (see segSpring below).
import { useSpring } from "motion/react";
import { WORKFLOW_STEPS_ORDERED, stepLabel } from "@/lib/workflowChips";

export type TokenTurn = { out: number; dt_s: number; tok_s: number };

export type PhaseProgress = {
  pct?: number;
  basis?: string;
  in_step_s?: number;
  typical_s?: number;
  children_done?: number;
  children_total?: number;
  tokens_since_step?: number;
  // Per-turn burn series (oldest..newest) + honest total turn count — drives
  // the live TokenTurns graph beside each conductor tile.
  token_turns?: TokenTurn[];
  turns?: number;
  // 'linked' = authoritative per-task series; 'wallclock' = project-wide
  // approximate fallback (TokenTurns renders it dimmed + labelled).
  tokens_source?: "linked" | "wallclock";
  // Forward-projected seconds to the terminal gate (learned per-step medians;
  // sharpens over time). eta_sample_n = current step's sample count.
  eta_s?: number | null;
  eta_sample_n?: number;
  // Full-SDLC time budget — the countdown bar drains eta_s against this.
  eta_total_s?: number | null;
};

function fmtTokens(t: number): string {
  if (t >= 1000) return `${(t / 1000).toFixed(t >= 10000 ? 0 : 1)}k`;
  return `${t}`;
}

function fmtClock(s: number): string {
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

// Coarse remaining-time label for the ETA readout.
function fmtEta(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}

// Real task status → label + accent tone (honest; no guessed "idle").
const STATUS_META: Record<string, { label: string; tone: string }> = {
  in_progress: { label: "in progress", tone: "teal" },
  done: { label: "done", tone: "emerald" },
  blocked: { label: "blocked", tone: "rose" },
  pending: { label: "pending", tone: "slate" },
};

function liveFraction(p: PhaseProgress, elapsedS: number): number {
  if ((p.basis ?? "time") === "children" && (p.children_total ?? 0) > 0) {
    return Math.min(1, (p.children_done ?? 0) / (p.children_total ?? 1));
  }
  const typical = Math.max(1, p.typical_s ?? 30);
  const liveSeconds = (p.in_step_s ?? 0) + Math.max(0, elapsedS);
  return Math.min(0.97, 1 - Math.exp(-liveSeconds / typical));
}

export default function SdlcProgress({
  step,
  phase,
  status,
  reduced,
  showCaption = true,
  hideTokens = false,
}: {
  step?: string;
  phase?: PhaseProgress | null;
  status?: string;
  reduced?: boolean | null;
  showCaption?: boolean;
  // When the tile renders a dedicated TokenTurns graph, suppress the caption's
  // token readout + the amber token bar so token info isn't duplicated.
  hideTokens?: boolean;
}) {
  const steps = WORKFLOW_STEPS_ORDERED;
  const curIdx = steps.findIndex((s) => s.id === step);
  const tokens = phase?.tokens_since_step ?? 0;
  const basis = phase?.basis ?? "time";
  const tokFrac = Math.max(0, Math.min(1, tokens / 500_000));
  const seed = Math.max(0, Math.min(1, phase?.pct ?? 0));

  const meta = STATUS_META[(status ?? "").toLowerCase()] ?? { label: status || "", tone: "slate" };
  // "live" = the task is genuinely being worked (status in_progress) — the
  // honest basis for the shimmer + pulse, instead of a laggy token delta.
  const live = (status ?? "").toLowerCase() === "in_progress";
  // A finished task's ticker must FREEZE: the server already froze in_step_s at
  // completed_at (basis="done", pct=1). Without this the animation frame kept
  // adding real wall-time to the frozen clock, so a done task's step counter
  // ticked up forever and its terminal segment rendered as still-filling.
  const taskDone =
    (status ?? "").toLowerCase() === "done" ||
    (phase?.pct ?? 0) >= 1 ||
    (phase?.basis === "done");

  // Overall task progress toward DONE: completed steps + current phase fraction.
  const overallSeed = curIdx >= 0 ? (curIdx + seed) / steps.length : seed;
  const [liveLabel, setLiveLabel] = useState(overallSeed * 100);
  const [liveInStep, setLiveInStep] = useState(phase?.in_step_s ?? 0);
  // current-SEGMENT fill (within the active step) drives just that segment.
  // The animation frame writes the raw live fraction to `segFill`; a spring
  // tweens the DISPLAYED value smoothly between the 5s poll snapshots instead
  // of hard-jumping. Under prefers-reduced-motion the spring collapses to a
  // stiff, damping-pinned pass-through so the bar tracks without easing.
  const segFill = useMotionValue(seed);
  const segSpring = useSpring(segFill, reduced
    ? { stiffness: 1000, damping: 100, restDelta: 0.001 }
    : { stiffness: 90, damping: 22, restDelta: 0.0005 });
  const segWidth = useTransform(segSpring, (v) => `${(Math.max(0, Math.min(1, v)) * 100).toFixed(3)}%`);

  const anchor = useRef({ t0: 0 });
  useEffect(() => {
    anchor.current.t0 = performance.now();
  }, [phase]);

  const lastPctAt = useRef(0);
  const lastClockAt = useRef(0);
  useAnimationFrame((t) => {
    if (!phase || curIdx < 0) return;
    // Only a genuinely in-progress task accrues elapsed wall-time; a done or
    // paused task stays pinned to the server's frozen in_step_s.
    const elapsedS = live ? (performance.now() - anchor.current.t0) / 1000 : 0;
    const wf = taskDone ? 1 : liveFraction(phase, elapsedS);
    segFill.set(wf);
    if (t - lastPctAt.current > 120) {
      lastPctAt.current = t;
      setLiveLabel(((curIdx + wf) / steps.length) * 100);
    }
    if (t - lastClockAt.current > 500) {
      lastClockAt.current = t;
      setLiveInStep((phase.in_step_s ?? 0) + elapsedS);
    }
  });

  return (
    <div className="w-full">
      <div className="flex items-stretch gap-[3px] h-2.5">
        {steps.map((s, i) => {
          const isGate = s.type === "gate";
          // A done task shows its terminal segment as complete (emerald), not
          // as a live-filling current step.
          const done = curIdx >= 0 && (i < curIdx || (i === curIdx && taskDone));
          const current = i === curIdx && !taskDone;
          const base = "relative flex-1 rounded-full overflow-hidden";
          if (done) {
            return (
              <div
                key={s.id}
                className={base}
                title={stepLabel(s.id)}
                style={{ background: "var(--accent-emerald-bg)", boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)" }}
              />
            );
          }
          if (current) {
            return (
              <div key={s.id} className={base} title={stepLabel(s.id)} style={{ background: "var(--surface-3)" }}>
                <motion.div
                  className="absolute inset-y-0 left-0 rounded-full"
                  style={{ width: segWidth, background: "var(--accent-teal-bg)", boxShadow: "inset 0 0 0 1px var(--accent-teal-ring)" }}
                />
                {!reduced && live && (
                  <motion.div
                    className="absolute inset-y-0 left-0 w-1/2 pointer-events-none"
                    style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.35), transparent)" }}
                    initial={{ x: "-100%" }}
                    animate={{ x: "260%" }}
                    transition={{ duration: 1.4, repeat: Infinity, ease: "easeInOut" }}
                  />
                )}
              </div>
            );
          }
          return (
            <div
              key={s.id}
              className={base}
              title={stepLabel(s.id)}
              style={{ background: "var(--surface-2)", opacity: isGate ? 0.45 : 0.7 }}
            />
          );
        })}
      </div>

      {showCaption && curIdx >= 0 && (
        <>
          <div className="mt-1 flex items-center justify-between text-[10px] font-mono text-[color:var(--text-muted)]">
            <span className="uppercase tracking-wider truncate">
              {stepLabel(steps[curIdx].id)} · {liveLabel.toFixed(2)}%
              {basis === "children" && phase?.children_total ? ` · ${phase.children_done}/${phase.children_total}` : ""}
            </span>
            <span className="flex items-center gap-1.5 shrink-0 tabular-nums">
              <motion.span
                className="inline-block h-1.5 w-1.5 rounded-full"
                style={{ background: `var(--accent-${meta.tone}-fg)` }}
                animate={!reduced && live ? { opacity: [1, 0.3, 1] } : { opacity: 1 }}
                transition={!reduced && live ? { duration: 1.2, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
              />
              <span>{fmtClock(liveInStep)}</span>
              {live && (phase?.eta_s ?? 0) > 5 && (
                <span
                  className="opacity-70"
                  style={{ color: "var(--accent-teal-fg)" }}
                  title={`ETA — forward-projected from learned per-step medians${phase?.eta_sample_n != null ? ` (current step n=${phase.eta_sample_n})` : ""}`}
                >
                  · ~{fmtEta(phase!.eta_s!)} left{(phase?.eta_sample_n ?? 0) < 2 ? " rough" : ""}
                </span>
              )}
              {!hideTokens && <span className="opacity-70">· {fmtTokens(tokens)} tok</span>}
              {meta.label && <span style={{ color: `var(--accent-${meta.tone}-fg)` }}>· {meta.label}</span>}
            </span>
          </div>
          {!hideTokens && tokens > 0 && (
            <div className="mt-0.5 h-[2px] w-full rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
              <motion.div
                className="h-full rounded-full"
                style={{ background: "var(--accent-amber-bg)", opacity: 0.7 }}
                initial={false}
                animate={{ width: `${tokFrac * 100}%` }}
                transition={{ duration: reduced ? 0 : 0.24 }}
              />
            </div>
          )}
        </>
      )}
    </div>
  );
}
