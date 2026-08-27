// Compact, collapsible SDLC rail — the "Implementation" tab's default view.
// One line per phase down a git-graph spine; each GATE is a one-liner that
// expands in place to its full receipt (badge · verified/override actor ·
// proof · reason). Completed agent steps fold into a single pill so the panel
// stays short on the task screen. Reads REAL data only: workflow_step (current),
// gate_state (pending gate), and timeline.gates[] (resolved-gate receipts) —
// nothing per-step is fabricated. Companion to SdlcProgress (minimap) and
// TaskActivityGantt (the wall-clock Timeline sub-view).
import { useState } from "react";
import { Link } from "react-router-dom";
import { motion } from "motion/react";
import { stepLabel, personaLabel } from "@/lib/workflowChips";
import { useWorkflowSteps } from "@/lib/useWorkflowDef";
import { domainTone } from "@/lib/domainTone";
import { fmtTokens } from "@/lib/format";
import { Lozenge } from "@/components/Lozenge";
import { type EvidenceItem } from "@/components/EvidenceGallery";
import { useProject } from "@/lib/project";
import type { PhaseProgress, Activity } from "./SdlcProgress";
import { type GanttGate, useGateEvidence, GateEvidenceBlock } from "./TaskActivityGantt";
import { activityLabel } from "@/lib/activityLabel";
// ONE shared severity vocabulary (task 8e5aa63b) — this rail's gate row must
// read the SAME label the TaskDetailPage banner and ConductorPage tile
// derive, from the SAME gateReadiness input, so the three cannot disagree.
import { gateSeverity, type GateSeveritySnapshot } from "@/lib/gateSeverity";

function clockHM(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

// One raw audit turn (same rows as the Timeline card). Drilling into a step
// reveals the turns that fired WHILE it was current — the implementation view
// and the timeline are the same thing, disclosed hierarchically.
export type StepTurn = { actor?: string; action?: string; details?: string; timestamp?: string; turn_tokens?: number };

// Completion proof & risk, rendered in the completion gate's evidence area so
// the proof lives WITH the pipeline (not a duplicate overview card).
export type GateCompletion = {
  proofType?: string;
  proof?: string;
  misfire?: string;
  fullOutcome?: boolean;
  taskId?: string;
};

function fmtDur(ms: number): string {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
// Duration a task SAT on each step = gap between when it entered that step and
// entered the next. The terminal/current step has no "next", so no duration.
function stageDurations(turns: StepTurn[]): Record<string, number> {
  const entries: { step: string; ts: number }[] = [];
  let cur = "";
  for (const t of turns ?? []) {
    const dest = stepDestOf(t.action, t.details);
    if (dest && dest !== cur) {
      cur = dest;
      const ts = t.timestamp ? new Date(t.timestamp).getTime() : NaN;
      if (!isNaN(ts)) entries.push({ step: dest, ts });
    }
  }
  const out: Record<string, number> = {};
  for (let i = 0; i < entries.length - 1; i++) {
    out[entries[i].step] = Math.max(0, entries[i + 1].ts - entries[i].ts);
  }
  return out;
}
// Per-stage metric rendered as a RELATIVE bar (filled vs the TOTAL across the
// run) with the numbers tucked underneath — fills the row's empty middle and
// lets you eyeball where the tokens/time went across the run. A bar at 100%
// means "this step took ALL of the elapsed time/tokens so far", so every
// bar's width is directly comparable to every other bar's. The bar tracks
// tokens when any stage spent tokens, else duration; the caption shows both.
// Replaces the redundant passed-pill — the spine ✓ already says "done".
function StepMeta({ durMs, tokens, sumTokens, sumDur, hasTurns, open, isGate }: {
  durMs?: number; tokens: number; sumTokens: number; sumDur: number; hasTurns: boolean; open: boolean; isGate?: boolean;
}) {
  const useTokens = sumTokens > 0;
  const val = useTokens ? tokens : (durMs ?? 0);
  const max = useTokens ? sumTokens : sumDur;
  const pct = max > 0 ? Math.max(2, Math.round((val / max) * 100)) : 0;
  const hasCaption = tokens > 0 || (durMs != null && durMs >= 1000);
  // Owner 2026-08-27: "this bar is not taking up the length of the area
  // STILL ... it should be like 40%-60%" -- the track is HALF the row (same
  // on every row, so it stays label-independent), never a 220px stub at the
  // right edge.
  return (
    <div className="ml-auto flex items-center gap-2 w-[50%] min-w-[220px] flex-none pl-6">
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        {/* Owner 2026-07-19: render the bar ONLY when this step actually spent
            something. A gate with no real tokens gets NO grey track (was a
            full-width 'massive grey line'); the bars that DO show are
            proportional to the heaviest step.
            Task c5b70c27: a GATE's own duration is human wait time, not agent
            work — it is excluded from sumDur/sumTokens (see StepRail) AND
            never rendered as a proportional bar here (isGate skips the bar
            entirely), so a resolved gate reads as plain duration/token text,
            never a misleadingly-scaled fill. */}
        {val > 0 && !isGate && (
          <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--surface-3)", boxShadow: "inset 0 0 0 1px var(--border-default)" }}>
            <div className="h-full rounded-full" style={{
              width: `${pct}%`,
              background: `var(--accent-${useTokens ? "violet" : "teal"}-fg)`,
              boxShadow: `0 0 8px var(--accent-${useTokens ? "violet" : "teal"}-ring)`,
            }} />
          </div>
        )}
        <div className="flex items-center gap-2.5 text-2xs font-mono tabular-nums leading-none text-[color:var(--text-muted)] justify-end">
          {tokens > 0 && <span style={{ color: "var(--accent-violet-fg)" }}>{fmtTokens(tokens)} tok</span>}
          {durMs != null && durMs >= 1000 && <span>{fmtDur(durMs)}</span>}
          {!hasCaption && <span className="opacity-40">—</span>}
        </div>
      </div>
      {hasTurns && <span className="text-2xs font-mono text-[color:var(--text-muted)] inline-block transition-transform flex-none self-start mt-0.5" style={{ transform: open ? "rotate(90deg)" : "none" }}>▸</span>}
    </div>
  );
}

function clockISO(ts?: string): string {
  return ts ? String(ts).slice(11, 19) : "";
}
function shortDetail(details?: string): string {
  const d = (details ?? "").replace(/\s+/g, " ").trim();
  return d.length > 84 ? d.slice(0, 84) + "…" : d;
}
// The workflow_step this row moved the task INTO, or null (no step change).
function stepDestOf(action?: string, details?: string): string | null {
  const d = details ?? "";
  if (action === "advance_task") return /(?:^|;\s*)to=([^;]*)/.exec(d)?.[1]?.trim() ?? null;
  if (action === "updated") return /workflow_step:\s*'[^']*'\s*->\s*'([^']*)'/.exec(d)?.[1] ?? null;
  return null;
}
// Bucket every turn under the step that was current when it fired.
function turnsByStep(turns: StepTurn[]): Record<string, StepTurn[]> {
  const out: Record<string, StepTurn[]> = {};
  let cur = "";
  for (const t of turns ?? []) {
    const dest = stepDestOf(t.action, t.details);
    if (dest) cur = dest;
    (out[cur || "__pre__"] ??= []).push(t);
  }
  return out;
}

// Indented, smaller sub-step list under a drilled-open step (progressive
// disclosure). Kept deliberately compact so the rail stays scannable.
function TurnList({ rows }: { rows: StepTurn[] }) {
  if (!rows.length) return <div className="ml-1 pl-3 text-2xs text-[color:var(--text-muted)] py-1">no turns recorded on this step</div>;
  return (
    <div className="mt-1.5 mb-1 ml-1 pl-3 border-l border-[color:var(--border-default)] space-y-1">
      {rows.map((r, i) => {
        const tone = domainTone("action", r.action ?? "") ?? "slate";
        return (
          <div key={i} className="flex items-baseline gap-2 text-2xs leading-snug">
            <span className="font-mono tabular-nums text-[color:var(--text-muted)] flex-none">{clockISO(r.timestamp)}</span>
            <span className="uppercase tracking-wide text-2xs px-1 py-0.5 rounded flex-none"
              style={{ background: `var(--accent-${tone}-bg)`, color: `var(--accent-${tone}-fg)` }}>
              {(r.action ?? "—").replace(/_/g, " ")}
            </span>
            {r.actor && <span className="text-2xs font-mono text-[color:var(--text-muted)] flex-none opacity-70">{r.actor}</span>}
            <span className="text-[color:var(--text-secondary)] truncate">{shortDetail(r.details)}</span>
          </div>
        );
      })}
    </div>
  );
}

type GateInfo = { state: "passed" | "override" | "pending" | "future"; g?: GanttGate };

export default function StepRail({
  step, gateState, phase, status, activity, gates, turns, stepTokens, reduced, proofType, completion, gateReadiness,
}: {
  step?: string;
  gateState?: string;
  // The SAME live readiness payload TaskDetailPage's banner reads (task
  // 8e5aa63b) — passed through so the current gate's pill quotes the shared
  // gateSeverity() label instead of a bare hardcoded "pending".
  gateReadiness?: GateSeveritySnapshot["readiness"];
  phase?: PhaseProgress | null;
  status?: string;
  // Honest work state — the current-step node/% only pulses when "working".
  activity?: Activity | null;
  gates: GanttGate[];
  turns?: StepTurn[];
  // Server-scoped per-step token totals (api.tasks._step_token_totals),
  // windowed to each step's own [entry, exit) span — keyed by step id. NOT
  // derived from summing turns[].turn_tokens by step: that let a step-entry
  // row absorb whatever idle gap preceded it ("review previous notes" read
  // 7.5M tok for a 49s step). A step missing from this map renders no token
  // figure rather than a guessed one.
  stepTokens?: Record<string, number>;
  reduced?: boolean | null;
  // Superseded by the RECEIPT-driven GateEvidenceBlock (task 25a25d84), which
  // fetches GET /api/tasks/:id/gate_evidence directly off `completion.taskId`
  // — kept in the prop type only so existing callers passing the task's full
  // evidence-store listing (PlanView.tsx) still type-check unchanged.
  evidence?: EvidenceItem[];
  // Proof type (test / demo / ui) — tailors the completion gate's empty
  // evidence message when nothing was captured.
  proofType?: string;
  // Completion proof & risk — rendered in the completion gate's evidence area.
  completion?: GateCompletion;
}) {
  const steps = useWorkflowSteps();
  const byStep = turnsByStep(turns ?? []);
  const durByStep = stageDurations(turns ?? []);
  // Bar scale: the TOTAL across every AGENT stage in the run (so each bar's
  // width is that stage's share of the real work, and bars are directly
  // comparable to each other — not each independently normalized against
  // the single heaviest stage, which made bar lengths look arbitrary).
  // Task c5b70c27: GATE steps are EXCLUDED here — a gate's duration is human
  // approval wait time (observed: plan_gate sat 85h49m on one task while
  // real agent steps ran 30s-3min), not commensurable agent work, so letting
  // it into the sum crushed every real step's bar to the 2% floor.
  let sumTokens = 0, sumDur = 0;
  for (const s of steps) {
    if (s.type === "gate") continue;
    sumTokens += stepTokens?.[s.id] ?? 0;
    sumDur += durByStep[s.id] ?? 0;
  }
  const curIdx = steps.findIndex((s) => s.id === step);
  // Pulse ONLY when genuinely being driven now (a real recent conductor
  // transition on THIS task), never merely because status is in_progress.
  const live = (activity?.state ?? status ?? "").toLowerCase() === "working";
  // Task 0f090a6c: the SAME claimed-but-unobservable "we can't see it" cue
  // ConductorPage's tile pill and SdlcProgress's minimap render, from the
  // ONE shared decision — previously this rail carried no signal-loss cue
  // at all (only the boolean `live` pulse above).
  const signal = activityLabel(activity);
  // Auto-expand the completion gate on a DONE task so its evidence is visible
  // immediately — otherwise the gate reads as empty until you happen to click
  // the exact row, and the evidence looks like it isn't there at all.
  const [open, setOpen] = useState<string | null>(
    (status ?? "").toLowerCase() === "done" ? "green_gate" : null);
  // Default to EXPANDED so the per-stage bars + drill-downs are visible up
  // front; the toggle folds all completed agent steps into one pill.
  const [collapseDone, setCollapseDone] = useState(false);

  const gateFor = (id: string) => gates.find((g) => id === `${g.gate}_gate`);
  const gateInfo = (id: string, i: number): GateInfo => {
    const g = gateFor(id);
    if (g) return { state: g.override ? "override" : "passed", g };
    if (curIdx >= 0 && i < curIdx) return { state: "passed" };
    if (i === curIdx) return { state: (gateState === "pending" ? "pending" : gateState === "failed" ? "pending" : "future") };
    return { state: "future" };
  };

  // Build render items, folding runs of completed AGENT steps into one pill.
  type Item = { kind: "step"; s: (typeof steps)[number]; i: number } | { kind: "fold"; names: string[]; i: number };
  const items: Item[] = [];
  let pend: { s: (typeof steps)[number]; i: number }[] = [];
  const flush = () => {
    if (!pend.length) return;
    items.push({ kind: "fold", names: pend.map((p) => stepLabel(p.s.id)), i: pend[0].i });
    pend = [];
  };
  steps.forEach((s, i) => {
    const done = curIdx >= 0 && i < curIdx;
    const isDoneAgent = done && s.type !== "gate";
    // Collapse Done folds every completed agent step into one pill; expand to
    // get them back (with their per-stage bars + drilled-in turns).
    if (collapseDone && isDoneAgent) { pend.push({ s, i }); return; }
    flush();
    items.push({ kind: "step", s, i });
  });
  flush();

  const railHasCur = curIdx >= 0;
  // The VERIFIER for the upcoming gate — persona of the next gate step ahead of
  // the current one (honest: omitted when no gate remains). Surfaced on the
  // current step so you can see WHO signs off before you get there.
  const nextGate = curIdx >= 0 ? steps.slice(curIdx + 1).find((s) => s.type === "gate") : undefined;
  const verifierPersona = nextGate?.persona || "";

  return (
    <div className="mt-3 select-none">
      <div className="flex items-center justify-end mb-1">
        <button
          onClick={() => setCollapseDone((v) => !v)}
          className="text-2xs uppercase tracking-wider font-mono px-2.5 py-1 rounded border border-[color:var(--border-default)] text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]"
        >
          {collapseDone ? "⊞ Expand done" : "⊟ Collapse done"}
        </button>
      </div>

      {items.map((it) => {
        if (it.kind === "fold") {
          const first = it.i === 0;
          return (
            <div key={`fold-${it.i}`} className="flex gap-3">
              <Spine topHidden={first} filledTop={!first} filledBot />
              <div className="flex-1 py-1">
                <button
                  onClick={() => setCollapseDone(false)}
                  className="text-2xs font-mono text-[color:var(--text-muted)] hover:text-[color:var(--text-secondary)] border border-dashed border-[color:var(--border-default)] rounded-md px-2.5 py-1.5 inline-flex items-center gap-2"
                >
                  <span style={{ color: "var(--accent-emerald-fg)" }}>✓</span>
                  <span className="text-[color:var(--text-secondary)]">{it.names.length}</span>
                  completed steps · {it.names.join(" · ")} · show
                </button>
              </div>
            </div>
          );
        }
        const { s, i } = it;
        const done = curIdx >= 0 && i < curIdx;
        const cur = i === curIdx;
        const future = curIdx >= 0 && i > curIdx;
        const isGate = s.type === "gate";
        const gi = isGate ? gateInfo(s.id, i) : null;
        const persona = (s as { persona?: string }).persona || "";
        const rowOpen = open === s.id;

        return (
          <div key={s.id} className="flex gap-3">
            <Spine
              topHidden={i === 0}
              filledTop={railHasCur && i <= curIdx}
              filledBot={railHasCur && i < curIdx}
              botHidden={i === steps.length - 1}
              node={<Node isGate={isGate} gi={gi} done={done} cur={cur} live={live} reduced={reduced} />}
            />
            <div className="flex-1 min-w-0 py-1.5">
              {isGate && gi && gi.state !== "future" ? (
                <GateRow s={s} gi={gi} open={rowOpen} onToggle={() => setOpen(rowOpen ? null : s.id)} turns={byStep[s.id] ?? []} durMs={durByStep[s.id]} tokens={stepTokens?.[s.id] ?? 0} sumTokens={sumTokens} sumDur={sumDur} proofType={proofType} completion={completion} gateReadiness={cur ? gateReadiness : undefined} />
              ) : (
                (() => {
                  const stepTurns = byStep[s.id] ?? [];
                  const hasTurns = stepTurns.length > 0;
                  return (
                    <div>
                      <div
                        onClick={hasTurns ? () => setOpen(rowOpen ? null : s.id) : undefined}
                        className={"flex items-center gap-2 min-h-[22px] rounded-md px-1.5 -mx-1.5 " + (hasTurns ? "cursor-pointer hover:bg-[color:var(--surface-2)] transition-colors" : "")}
                      >
                        <Persona persona={persona} isGate={isGate} />
                        <span className={"text-[13px] " + (future ? "text-[color:var(--text-disabled)]" : "text-[color:var(--text-primary)]")}>
                          {stepLabel(s.id)}
                        </span>
                        <div className="ml-auto flex items-center gap-2 min-w-0">
                          {cur && !isGate && verifierPersona && (
                            <VerifiedBy persona={verifierPersona} decided={!!gateFor(nextGate?.id ?? "")} />
                          )}
                          {cur && !isGate && (phase?.fanout_dispatched ?? 0) > 0 && (
                            <span className="flex items-center gap-1 text-2xs font-mono tabular-nums flex-none" style={{ color: "var(--accent-teal-fg)" }}
                              title="ephemeral sub-agents dispatched vs returned for this step">
                              <span>{phase?.fanout_returned ?? 0}/{phase?.fanout_dispatched ?? 0} agents back</span>
                              <span className="h-[3px] w-8 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
                                <span className="block h-full rounded-full" style={{
                                  width: `${Math.min(1, (phase?.fanout_returned ?? 0) / Math.max(1, phase?.fanout_dispatched ?? 1)) * 100}%`,
                                  background: "var(--accent-teal-bg)",
                                }} />
                              </span>
                            </span>
                          )}
                          {cur && !isGate && signal.lost ? (
                            <span className="text-2xs font-mono flex items-center gap-1.5 flex-none" style={{ color: "var(--text-muted)" }}>
                              {signal.label}
                            </span>
                          ) : cur && !isGate && (
                            <span className="text-2xs font-mono tabular-nums flex items-center gap-1.5 flex-none" style={{ color: "var(--accent-teal-fg)" }}>
                              <motion.span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: "var(--accent-teal-fg)" }}
                                animate={!reduced ? { opacity: [1, 0.3, 1] } : { opacity: 1 }}
                                transition={!reduced ? { duration: 1.2, repeat: Infinity } : { duration: 0.2 }} />
                              {phase?.pct != null ? `${((curIdx >= 0 ? (curIdx + Math.min(1, phase.pct)) / steps.length : 0) * 100).toFixed(0)}%` : "working"}
                            </span>
                          )}
                          {!cur && <StepMeta durMs={durByStep[s.id]} tokens={stepTokens?.[s.id] ?? 0} sumTokens={sumTokens} sumDur={sumDur} hasTurns={hasTurns} open={rowOpen} />}
                          {cur && hasTurns && (
                            <span className="text-2xs font-mono text-[color:var(--text-muted)] inline-block transition-transform flex-none" style={{ transform: rowOpen ? "rotate(90deg)" : "none" }}>▸</span>
                          )}
                        </div>
                      </div>
                      {rowOpen && hasTurns && <TurnList rows={stepTurns} />}
                    </div>
                  );
                })()
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function Spine({ topHidden, botHidden, filledTop, filledBot, node }: {
  topHidden?: boolean; botHidden?: boolean; filledTop?: boolean; filledBot?: boolean;
  node?: React.ReactNode;
}) {
  const line = (fill?: boolean, hide?: boolean) => (
    <div className="w-[2px] flex-1 min-h-[8px]" style={{
      background: hide ? "transparent" : fill ? "var(--accent-emerald-ring)" : "var(--border-default)",
    }} />
  );
  return (
    <div className="w-[18px] flex-none flex flex-col items-center">
      {line(filledTop, topHidden)}
      {node ?? <span className="h-2 w-2 rounded-full my-0.5" style={{ background: "var(--accent-emerald-fg)" }} />}
      {line(filledBot, botHidden)}
    </div>
  );
}

function Node({ isGate, gi, done, cur, live, reduced }: {
  isGate: boolean; gi: GateInfo | null; done: boolean; cur: boolean; live: boolean; reduced?: boolean | null;
}) {
  const pulse = cur && live && !reduced
    ? { animate: { boxShadow: ["0 0 0 0 rgba(94,234,212,0.5)", "0 0 0 5px rgba(94,234,212,0)"] }, transition: { duration: 1.3, repeat: Infinity } }
    : {};
  if (isGate && gi) {
    const map: Record<string, { bg: string; fg: string; g: string }> = {
      passed: { bg: "var(--accent-emerald-fg)", fg: "#06281c", g: "✓" },
      override: { bg: "var(--accent-amber-fg)", fg: "#3a2a04", g: "!" },
      pending: { bg: "var(--accent-amber-fg)", fg: "#3a2a04", g: "‽" },
      future: { bg: "transparent", fg: "var(--text-muted)", g: "" },
    };
    const m = map[gi.state];
    if (gi.state === "future") {
      return <span className="mt-2 h-3 w-3 rotate-45 rounded-[2px] border-2" style={{ borderColor: "var(--accent-slate-ring)" }} />;
    }
    return (
      <motion.span className="mt-2 h-3.5 w-3.5 rotate-45 rounded-[3px] grid place-items-center text-2xs font-bold"
        style={{ background: m.bg, color: m.fg }} {...(gi.state === "pending" ? pulse : {})}>
        <span className="-rotate-45">{m.g}</span>
      </motion.span>
    );
  }
  if (done) return <span className="mt-2 h-3 w-3 rounded-full grid place-items-center text-2xs font-bold" style={{ background: "var(--accent-emerald-fg)", color: "#06281c" }}>✓</span>;
  if (cur) return <motion.span className="mt-2 h-3 w-3 rounded-full" style={{ background: "var(--accent-teal-fg)" }} {...pulse} />;
  return <span className="mt-2 h-3 w-3 rounded-full border-2" style={{ borderColor: "var(--accent-slate-ring)" }} />;
}

// One persona tag per row. Tone comes from the single domainTone("role") map
// (sm→teal, qa→violet, dev→amber) — no local palette. An agent step names its
// owning role (Steward/Verifier/Builder); a gate names its Steward reviewer
// with a ◇ so it reads "who signs off here" rather than a blank "gate".
function Persona({ persona, isGate }: { persona: string; isGate: boolean }) {
  const tone = domainTone("role", persona) ?? "slate";
  const label = persona ? personaLabel(persona) : isGate ? "gate" : "sdlc";
  const title = isGate && persona ? `${personaLabel(persona)} reviews this gate` : undefined;
  return (
    <span
      title={title}
      className="text-2xs font-mono uppercase tracking-wide px-1.5 py-0.5 rounded flex-none inline-flex items-center gap-1"
      style={{ background: `var(--accent-${tone}-bg)`, color: `var(--accent-${tone}-fg)` }}>
      {isGate && persona && <span aria-hidden>◇</span>}
      {label}
    </span>
  );
}

// The persona who reviews the UPCOMING gate, shown on the current step so the
// reviewer is legible before the gate is reached. `decided` is a REAL
// gate_decide record (gateFor(...) against the resolved `gates` array), never
// step position — until it exists this reads as a prediction ("{Role} reviews
// next"), not a completed-action claim (task 122ff356). Same domainTone
// ("role") palette + ◇ marker as the gate Persona tag.
function VerifiedBy({ persona, decided }: { persona: string; decided: boolean }) {
  const tone = domainTone("role", persona) ?? "slate";
  const label = personaLabel(persona);
  return (
    <span
      title={decided ? `${label} verified this step` : `${label} reviews next`}
      className="text-2xs font-mono uppercase tracking-wide px-1.5 py-0.5 rounded flex-none inline-flex items-center gap-1"
      style={{ color: `var(--accent-${tone}-fg)`, boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)` }}>
      <span aria-hidden>◇</span>
      {decided ? <>verified by {label}</> : <>{label} reviews next</>}
    </span>
  );
}

// Completion proof & risk, shown in the completion gate's evidence area:
// proof type, the recorded completion proof (link to the full proof page),
// the likely-misfire risk, and slice-vs-finished outcome. One home for what
// used to be a separate overview card.
function GateCompletionBlock({ c }: { c: GateCompletion }) {
  const proofLine = (c.proof ?? "").replace(/\s+/g, " ").trim();
  const short = proofLine.length > 160 ? proofLine.slice(0, 160) + "…" : proofLine;
  return (
    <div className="rounded-md border border-[color:var(--border-default)] p-2.5 space-y-2 text-[12px]" style={{ background: "var(--surface-1)" }}>
      <div className="flex items-center gap-2">
        <span className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>completion proof</span>
        {c.proofType && (
          <span className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded"
            style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}>{c.proofType}</span>
        )}
      </div>
      {proofLine
        ? (c.taskId
            ? <Link to={`/tasks/${c.taskId}/proof`} className="flex items-start gap-1.5 group" style={{ color: "var(--text-secondary)" }}>
                <span className="leading-relaxed group-hover:opacity-100 opacity-90">{short}</span>
                <span className="opacity-50 group-hover:opacity-100 shrink-0">→</span>
              </Link>
            : <span className="leading-relaxed" style={{ color: "var(--text-secondary)" }}>{short}</span>)
        : <span className="text-[color:var(--accent-amber-fg)] text-2xs">⚠ not yet recorded</span>}
      {c.misfire && (
        <div>
          <div className="text-2xs uppercase tracking-wider mb-0.5" style={{ color: "var(--text-muted)" }}>likely misfire — how this could pass-but-be-wrong</div>
          <div className="flex items-start gap-1.5 leading-relaxed" style={{ color: "var(--accent-amber-fg)" }}>
            <span className="shrink-0">⚠</span><span className="opacity-90">{c.misfire}</span>
          </div>
        </div>
      )}
      <div className="flex items-start gap-1.5 leading-relaxed" style={{ color: c.fullOutcome ? "var(--accent-emerald-fg)" : "var(--accent-amber-fg)" }}>
        <span className="shrink-0">{c.fullOutcome ? "✓" : "◐"}</span>
        <span className="opacity-90">{c.fullOutcome
          ? "Owner outcome: complete — the full outcome is mapped (slice green, no open children, strong proof)"
          : "Owner outcome: slice-only — a green slice is not yet proof the full owner outcome is met"}</span>
      </div>
    </div>
  );
}

function GateRow({ s, gi, open, onToggle, turns, durMs, tokens, sumTokens, sumDur, proofType, completion, gateReadiness }: {
  s: { id: string; persona?: string }; gi: GateInfo; open: boolean; onToggle: () => void; turns?: StepTurn[]; durMs?: number; tokens: number; sumTokens: number; sumDur: number; proofType?: string; completion?: GateCompletion;
  gateReadiness?: GateSeveritySnapshot["readiness"];
}) {
  const [receipt, setReceipt] = useState(false);
  const [project] = useProject();
  const g = gi.g;
  const override = gi.state === "override";
  const isCompletionGate = g?.gate === "green" || /green/i.test(g?.gate ?? "");
  // Task 25a25d84: the gate CARD renders what the trusted-runner RECEIPT
  // actually captured (screenshot/video/verbatim assertion) — never the
  // driving agent's hand-attached completion_proof markdown. One fetch per
  // gate row, keyed on the task (the receipt is the task's newest run, not
  // per-step) so the badge and the expanded panel always agree.
  const gateEv = useGateEvidence(completion?.taskId, project);
  const capturedCount = (gateEv.data?.artifacts ?? [])
    .filter((a) => a.kind === "screenshot" || a.kind === "video").length;
  return (
    <div>
      <button onClick={onToggle} className="w-full flex items-center gap-2 min-h-[22px] text-left rounded-md px-1.5 -mx-1.5 hover:bg-[color:var(--surface-2)] transition-colors">
        <Persona persona={s.persona ?? ""} isGate />
        <span className="text-[13px] text-[color:var(--text-primary)] whitespace-nowrap">{stepLabel(s.id)}</span>
        {/* Evidence indicator — makes it obvious a gate HAS a captured artifact
            to see, so it isn't hidden behind a click. */}
        {capturedCount > 0 && (
          <span className="flex items-center gap-1 text-2xs px-1.5 py-0.5 rounded flex-none"
            style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)" }}
            title="this gate has captured evidence — click to view">
            <span aria-hidden>{gateEv.data?.artifacts.some((a) => a.kind === "video") ? "▶" : "▣"}</span>
            {`${capturedCount} ${capturedCount === 1 ? "artifact" : "artifacts"}`}
          </span>
        )}
        {/* A resolved gate needs no "passed" pill or receipt line here — the
            spine node is already a green ✓ (amber ! on override). Show the
            useful stage metric instead; the receipt lives in the panel below. */}
        {gi.state === "pending" ? (
          <span className="ml-auto flex items-center gap-2 flex-none">
            <Lozenge tone="warn">
              {gateSeverity({ gate_state: "pending", readiness: gateReadiness ?? null }).label.toLowerCase()}
            </Lozenge>
            <span className="text-2xs font-mono text-[color:var(--text-muted)] inline-block transition-transform" style={{ transform: open ? "rotate(90deg)" : "none" }}>▸</span>
          </span>
        ) : (
          <StepMeta durMs={durMs} tokens={tokens} sumTokens={sumTokens} sumDur={sumDur} hasTurns={(turns?.length ?? 0) > 0} open={open} isGate />
        )}
      </button>
      {open && g && (
        <div className="mt-2 rounded-lg p-3 border"
          style={{
            borderColor: override ? "var(--accent-amber-ring)" : "var(--accent-emerald-ring)",
            background: `linear-gradient(0deg, var(--accent-${override ? "amber" : "emerald"}-bg), transparent)`,
          }}>
          <div className="flex items-center gap-2 flex-wrap text-2xs font-mono text-[color:var(--text-secondary)]">
            <span className="font-bold" style={{ color: override ? "var(--accent-amber-fg)" : "var(--accent-emerald-fg)" }}>
              {override ? "! override" : "✓ verified"}
            </span>
            <span>{g.actor || "—"}</span>
            {g.proof && <span className="px-1.5 py-0.5 rounded text-2xs uppercase" style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}>{g.proof}</span>}
            <span className="ml-auto text-[color:var(--text-muted)]">{clockHM(g.ts)}</span>
          </div>
          <div className="mt-2 text-[12.5px] text-[color:var(--text-secondary)] leading-relaxed">{g.reason}</div>
          {/* The gate CHIP expands (click) into the RECEIPT's captured
              evidence — screenshot/video via the shared lightbox, verbatim
              assertion source, and provenance. Shown on every gate (not just
              completion) so a gate with nothing captured reads honestly. */}
          <div className="mt-2.5 space-y-2.5">
            <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
              captured evidence{capturedCount > 0 ? ` · ${capturedCount}` : ""}
            </div>
            <GateEvidenceBlock taskId={completion?.taskId} project={project} proofType={proofType} />
            {/* Completion proof & risk lives here with the pipeline, not in a
                duplicate overview card. */}
            {isCompletionGate && completion && <GateCompletionBlock c={completion} />}
          </div>
          {g.reason && g.reason.length > 120 && (
            <>
              {receipt && (
                <div className="mt-2 border-l-2 pl-2.5 font-mono text-2xs text-[color:var(--text-muted)] whitespace-pre-wrap leading-relaxed"
                  style={{ borderColor: "var(--border-strong)" }}>{g.reason}</div>
              )}
              <button onClick={() => setReceipt((v) => !v)} className="mt-1.5 text-2xs font-mono" style={{ color: "var(--accent-teal-fg)" }}>
                {receipt ? "▾ hide receipt" : "▸ full receipt"}
              </button>
            </>
          )}
        </div>
      )}
      {open && (turns?.length ?? 0) > 0 && <TurnList rows={turns!} />}
    </div>
  );
}
