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
import { stepLabel, personaLabel } from "@/lib/workflowChips";
import { useWorkflowSteps } from "@/lib/useWorkflowDef";
import { fmtTokens } from "@/lib/format";
import { activityLabel } from "@/lib/activityLabel";

// Per-segment tooltip: step name + the role that owns it ("story gate ·
// Steward"), so hovering the minimap says WHO drives each phase.
function segTitle(s: { id: string; persona?: string }): string {
  const who = personaLabel(s.persona);
  return who ? `${stepLabel(s.id)} · ${who}` : stepLabel(s.id);
}

export type TokenTurn = { out: number; dt_s: number; tok_s: number };

export type PhaseProgress = {
  pct?: number;
  basis?: string;
  in_step_s?: number;
  typical_s?: number;
  children_done?: number;
  children_total?: number;
  // Ephemeral per-step sub-agent fanout for the CURRENT step: how many
  // disposable units (e.g. test-writers) were dispatched vs returned. 0/0
  // when the step handed out none. basis==="fanout" ⇒ pct = returned/dispatched.
  fanout_dispatched?: number;
  fanout_returned?: number;
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

// Honest per-task work state (server: conductor_service.activity_for). The
// tile pill + live pulse read THIS, not the raw status — "working" is the ONLY
// state that means the task is actively being driven right now.
export type Activity = {
  state?: string;             // working|driving|adrift|stalled|awaiting_gate|done|blocked|pending
  task_motion_s?: number | null;   // s since the last conductor transition on THIS task
  session_quiet_s?: number | null; // s since the linked session's transcript moved
  // The driver's own mid-step progress evidence (drive_heartbeat), present only
  // while fresh. Lets the pill say WHAT a driving step is doing, not just that
  // it is doing something.
  heartbeat?: { step?: string; last_tool?: string; elapsed_s?: number | null; age_s?: number | null } | null;
  // Task e9625a4d + 0f090a6c additive fields (api/conductor.py
  // _with_report_signal), read only via lib/activityLabel.ts's activityLabel().
  report_signal_lost?: boolean;
  report_signal_age_s?: number | null;
};

function fmtClock(s: number): string {
  const total = Math.max(0, Math.floor(s));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

// Coarse remaining-time label for the ETA readout.
function fmtEta(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}

// Grace window: the step clock keeps counting for this many seconds after
// entering a step even before the first token lands for it (setup/tool
// calls precede the first assistant turn), so early execution isn't
// invisible. Matches the 90s session-quiet window session activity itself
// uses (ACTIVITY_META below).
const COUNT_GRACE_S = 90;

// Honest activity STATE → label + accent tone. Keyed by activity.state (not the
// raw status) so a task the conductor isn't driving reads as slate/rose, not a
// teal "in progress" lie. THE ONE map every surface renders through — LiveBar and
// ConductorPage import it rather than printing the raw enum (owner 2026-07-21 was
// shown a bare "adrift" on the LiveBar and asked what he was supposed to do).
//
// "idle"/"stalled" are ALARM WORDS: the owner reads them as "I must intervene
// somewhere", so they may appear ONLY when there IS an owner action. `adrift`
// is NOT one of those moments — it means a linked session emitted tokens in the
// last 90s while no conductor step BOUNDARY was crossed in 120s, which is the
// normal condition of a healthy 5-10min step. It now says so plainly.
export const ACTIVITY_META: Record<string, { label: string; tone: string }> = {
  working: { label: "in progress", tone: "teal" },
  // Heartbeat-attributed liveness (task e3b7ebf6): a step running past both
  // the 120s transition window and the 90s session-quiet window, with a
  // fresh task-scoped progress ping. Same non-alarm treatment as `working`
  // — there is nothing for the owner to do.
  driving: { label: "driving", tone: "teal" },
  paused: { label: "paused", tone: "teal" },   // epic between slices — progress, NOT stalled
  awaiting_gate: { label: "awaiting review", tone: "amber" },
  // A gate step whose `machine_only_gate` is true (red_gate — the ONE gate
  // a human owner is never routed to, see api/workflows.py
  // MACHINE_ONLY_GATES) resolves here instead of awaiting_gate above, so
  // the pill never claims a human reviewer is owed a decision that does
  // not exist. Non-alarm tone: there is nothing for the owner to do, same
  // as `working`/`driving` (owner 2026-08-26, caught live on their own
  // screen via remote assist: "dont show the user things that are not
  // real").
  awaiting_gate_machine: { label: "machine deciding", tone: "teal" },
  adrift: { label: "driver active · between step reports", tone: "teal" },
  // "needs you" was a lie here: when nothing is driving a mid-flow step the
  // OWNER has no affordance to click, the fix is a driver relaunch (the
  // resume watcher's job). The alarm words stay reserved for gates, where an
  // owner action actually exists (awaiting_gate above).
  stalled: { label: "no active driver", tone: "rose" },
  in_progress: { label: "in progress", tone: "teal" },  // pre-activity fallback
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
  activity,
  reduced,
  showCaption = true,
  hideTokens = false,
  workflow,
}: {
  step?: string;
  phase?: PhaseProgress | null;
  status?: string;
  // Honest work state — drives the pill + pulse. When omitted we fall back to
  // the raw status (legacy callers) so nothing regresses to a blank pill.
  activity?: Activity | null;
  reduced?: boolean | null;
  showCaption?: boolean;
  // When the tile renders a dedicated TokenTurns graph, suppress the caption's
  // token readout + the amber token bar so token info isn't duplicated.
  hideTokens?: boolean;
  // The task's own workflow (task.workflow) — resolves the minimap to THAT
  // workflow's own FSM steps via useWorkflowSteps, same as StepRail. See
  // StepRail.tsx's `workflow` prop doc for the full rationale.
  workflow?: string;
}) {
  const steps = useWorkflowSteps(workflow);
  const curIdx = steps.findIndex((s) => s.id === step);
  const tokens = phase?.tokens_since_step ?? 0;
  const basis = phase?.basis ?? "time";
  const tokFrac = Math.max(0, Math.min(1, tokens / 500_000));
  const seed = Math.max(0, Math.min(1, phase?.pct ?? 0));

  // The honest state: prefer activity.state, fall back to raw status.
  const state = (activity?.state ?? status ?? "").toLowerCase();
  // Defensive cross-check (task 122ff356): "awaiting review" may only reach
  // the screen while the CURRENT step's own resolved type is genuinely a
  // gate — never let a stale/relayed activity.state render that claim on a
  // mid-execution non-gate step. Keys off the step's `type` field, never a
  // step id, so a new workflow step can't reintroduce the same phantom claim.
  const curStepType = curIdx >= 0 ? steps[curIdx].type : undefined;
  // Same defensive pattern, one gate further: a gate step whose OWN
  // machine_only_gate is true (fetched from GET /api/workflows, never a
  // hardcoded id here) never renders the human-reviewer claim either.
  const curStepMachineOnly = curIdx >= 0 && steps[curIdx].machine_only_gate === true;
  const effectiveState = state === "awaiting_gate" && curStepType !== "gate" ? "working"
    : state === "awaiting_gate" && curStepMachineOnly ? "awaiting_gate_machine"
    : state;
  const meta = ACTIVITY_META[effectiveState] ?? { label: effectiveState || "", tone: "slate" };
  // How long since the last conductor STEP BOUNDARY (task_motion_s). On `adrift`
  // that is a "last report" age, NOT an idle time — the driver is mid-step and
  // steps run 5-10min, so the clock must not be captioned as idleness. Only a
  // genuinely dead drive (`stalled`) earns the alarm wording.
  const idleClock = activity?.task_motion_s != null ? fmtClock(activity.task_motion_s) : "";
  const hb = activity?.heartbeat;
  // Task 0f090a6c: the SAME claimed-but-unobservable "we can't see it" cue
  // as ConductorPage's tile pill, from the ONE shared decision — 'adrift'
  // and 'stalled' are the two classifications it can fire for; checked
  // BEFORE either's own normal-state wording below.
  const signal = activityLabel(activity);
  // The pulse dot + stateLabel color below read effTone rather than
  // meta.tone directly while unobservable — never mutate `meta` itself,
  // it's the shared ACTIVITY_META map object.
  const effTone = signal.lost ? signal.tone : meta.tone;
  const stateLabel = signal.lost ? signal.label
    : state === "adrift" ? `driver active · last step report ${idleClock || "—"} ago`
    : state === "driving" && hb?.last_tool
      ? `driving · ${hb.last_tool}${hb.elapsed_s != null ? ` · ${fmtClock(hb.elapsed_s)} in step` : ""}`
    : state === "stalled" ? `no active driver · idle ${idleClock || "—"}`
    : state === "paused" ? `paused · ${phase?.children_done ?? 0}/${phase?.children_total ?? 0} done`
    : meta.label;
  // "live" = the task is genuinely being DRIVEN right now — a real recent
  // conductor transition ("working"), a live linked session mid-step
  // ("adrift"), OR a fresh task-attributed heartbeat past both those windows
  // ("driving", task e3b7ebf6), NEVER raw in_progress. `adrift` is included
  // because a 5-10min step crosses no boundary for most of its life, so
  // excluding it rendered a BUILDING task as motionless (owner 2026-07-21).
  // A `stalled` tile — nothing driving it at all — must still read as
  // still, not animated.
  const live = state === "working" || state === "adrift" || state === "driving";

  // Overall task progress toward DONE: completed steps + current phase
  // fraction. PURE — a function of the server's phase.pct + curIdx only, so
  // it changes exactly when the server reports a new value and never in
  // between. This is the number rendered in the caption below; it must never
  // creep on its own (see the 'children'/'time' comment on liveFraction —
  // that estimate drives the segment WIDTH tween, never the printed %).
  const overallSeed = basis === "children"
    // basis="children": the server's pct IS the epic's progress
    // (7/13 = 0.538462), not a position inside the current step.
    // Folding it into a step index printed "77.62% - 7/13" on the
    // same line: one measure, rendered twice, contradicting itself
    // (owner 2026-08-06: "9/11 ?!? it says 7/13 still").
    ? seed
    // For time/fanout the pct really does describe the CURRENT step,
    // so blending it with the step position is the right reading.
    : (curIdx >= 0 ? (curIdx + seed) / steps.length : seed);
  // The step clock measures EXECUTION time, not wall time (owner 2026-07-14:
  // 'when the ball is in our court it shouldn't be running'). It ticks while the
  // task is actually being WORKED — "working" AND "adrift", since on `adrift`
  // the ball IS in our court (a driver is mid-step). It FREEZES only when the
  // ball is genuinely elsewhere (awaiting review / stalled), at in_step_s minus
  // task_motion_s (both server-truth). Before this, the execution clock froze
  // DURING execution while the idle clock ticked beside it on the same line —
  // two clocks disagreeing about the same second (owner 2026-07-21).
  const counting = live && (tokens > 0 || (phase?.in_step_s ?? 0) < COUNT_GRACE_S);
  const frozenInStep = Math.max(
    0, (phase?.in_step_s ?? 0) - (activity?.task_motion_s ?? 0));
  const [liveInStep, setLiveInStep] = useState(
    counting ? (phase?.in_step_s ?? 0) : frozenInStep);
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

  const lastClockAt = useRef(0);
  useAnimationFrame((t) => {
    if (!phase || curIdx < 0) return;
    // Parked (not working): the execution clock and the segment fill hold
    // at the last conductor motion — never creep on wall time.
    if (!counting) {
      const wf = liveFraction(phase, 0);
      segFill.set(wf);
      if (t - lastClockAt.current > 500) {
        lastClockAt.current = t;
        setLiveInStep(frozenInStep);
      }
      return;
    }
    const elapsedS = (performance.now() - anchor.current.t0) / 1000;
    const wf = liveFraction(phase, elapsedS);
    // Segment WIDTH may tween toward this live estimate (cosmetic fill of
    // the current step) — but the PRINTED percentage never reads it; that
    // was the bug (owner: server pct=0.538 rendered 77.62%, then climbed
    // 78->86->95% on zero real progress). overallSeed (pure, server-only)
    // is what the caption renders below.
    segFill.set(wf);
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
          const done = curIdx >= 0 && i < curIdx;
          const current = i === curIdx;
          const base = "relative flex-1 rounded-full overflow-hidden";
          if (done) {
            return (
              <div
                key={s.id}
                className={base}
                title={segTitle(s)}
                style={{ background: "var(--accent-emerald-bg)", boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)" }}
              />
            );
          }
          if (current) {
            return (
              <div key={s.id} className={base} title={segTitle(s)} style={{ background: "var(--surface-3)" }}>
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
              title={segTitle(s)}
              style={{ background: "var(--surface-2)", opacity: isGate ? 0.45 : 0.7 }}
            />
          );
        })}
      </div>

      {showCaption && curIdx >= 0 && (
        <>
          <div className="mt-1 flex items-center justify-between text-2xs font-mono text-[color:var(--text-muted)]">
            <span className="uppercase tracking-wider truncate">
              {stepLabel(steps[curIdx].id)} · {(overallSeed * 100).toFixed(2)}%
              {basis === "children" && phase?.children_total ? ` · ${phase.children_done}/${phase.children_total}` : ""}
              {(basis === "fanout" || (phase?.fanout_dispatched ?? 0) > 0)
                ? ` · ${phase?.fanout_returned ?? 0}/${phase?.fanout_dispatched ?? 0} back`
                : ""}
            </span>
            <span className="flex items-center gap-1.5 shrink-0 tabular-nums">
              <motion.span
                className="inline-block h-1.5 w-1.5 rounded-full"
                style={{ background: `var(--accent-${effTone}-fg)` }}
                animate={!reduced && live ? { opacity: [1, 0.3, 1] } : { opacity: 1 }}
                transition={!reduced && live ? { duration: 1.2, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
              />
              {/* The clock is an EXECUTION metric (owner: for observing and
                  refining LLM process time). It counts through COUNT_GRACE_S
                  before the first token lands for a step (setup precedes the
                  first turn), then gates on real token flow via `counting`.
                  When NOT counting it still renders — a clock that vanishes
                  reads as broken, not as "nothing to see" — showing the
                  server-truth waiting duration (phase.in_step_s) instead of
                  the animated execution clock, so a long park never prints
                  the execution timer's minutes:seconds as if work were live. */}
              {counting ? (
                <span>{fmtClock(liveInStep)}</span>
              ) : (
                <span className="opacity-70">
                  {/* "waiting" contradicts a "working" state label (task
                      95474ec7, owner live: "still looks like you have now
                      been waiting for 90+ mins" on an epic whose subtree was
                      genuinely active) — an epic-rollup can be state=working
                      via _subtree_active (deep descendant motion) while THIS
                      task's own step never streams a token, so `counting`
                      stays false. Say "elapsed" there instead; every other
                      not-counting state (the true parked case) keeps
                      "waiting", unchanged. */}
                  {state === "working" ? "elapsed" : "waiting"} {fmtClock(phase?.in_step_s ?? 0)}
                </span>
              )}
              {counting && (phase?.eta_s ?? 0) > 5 && (
                <span
                  className="opacity-70"
                  style={{ color: "var(--accent-teal-fg)" }}
                  title={`ETA — forward-projected from learned per-step medians${phase?.eta_sample_n != null ? ` (current step n=${phase.eta_sample_n})` : ""}`}
                >
                  · ~{fmtEta(phase!.eta_s!)} left{(phase?.eta_sample_n ?? 0) < 2 ? " rough" : ""}
                </span>
              )}
              {!hideTokens && <span className="opacity-70">· {fmtTokens(tokens)} tok</span>}
              {stateLabel && <span style={{ color: `var(--accent-${effTone}-fg)` }}>· {stateLabel}</span>}
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
