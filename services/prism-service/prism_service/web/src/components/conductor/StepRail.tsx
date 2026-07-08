// Compact, collapsible SDLC rail — the "Implementation" tab's default view.
// One line per phase down a git-graph spine; each GATE is a one-liner that
// expands in place to its full receipt (badge · verified/override actor ·
// proof · reason). Completed agent steps fold into a single pill so the panel
// stays short on the task screen. Reads REAL data only: workflow_step (current),
// gate_state (pending gate), and timeline.gates[] (resolved-gate receipts) —
// nothing per-step is fabricated. Companion to SdlcProgress (minimap) and
// TaskActivityGantt (the wall-clock Timeline sub-view).
import { useState } from "react";
import { motion } from "motion/react";
import { WORKFLOW_STEPS_ORDERED, stepLabel, personaLabel } from "@/lib/workflowChips";
import { domainTone } from "@/lib/domainTone";
import { fmtTokens } from "@/lib/format";
import type { PhaseProgress, Activity } from "./SdlcProgress";
import type { GanttGate } from "./TaskActivityGantt";

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
function stepTokens(rows: StepTurn[]): number {
  return (rows ?? []).reduce((a, r) => a + (typeof r.turn_tokens === "number" ? r.turn_tokens : 0), 0);
}

// Per-stage metric rendered as a RELATIVE bar (filled vs the heaviest stage)
// with the numbers tucked underneath — fills the row's empty middle and lets
// you eyeball where the tokens/time went across the run. The bar tracks tokens
// when any stage spent tokens, else duration; the caption shows both. Replaces
// the redundant passed-pill — the spine ✓ already says "done".
function StepMeta({ durMs, tokens, maxTokens, maxDur, hasTurns, open }: {
  durMs?: number; tokens: number; maxTokens: number; maxDur: number; hasTurns: boolean; open: boolean;
}) {
  const useTokens = maxTokens > 0;
  const val = useTokens ? tokens : (durMs ?? 0);
  const max = useTokens ? maxTokens : maxDur;
  const pct = max > 0 ? Math.max(2, Math.round((val / max) * 100)) : 0;
  const hasCaption = tokens > 0 || (durMs != null && durMs >= 1000);
  return (
    <div className="ml-auto flex items-center gap-2 flex-1 min-w-0 max-w-[420px] pl-6">
      <div className="flex-1 min-w-0 flex flex-col gap-1">
        <div className="h-1.5 rounded-full overflow-hidden" style={{ background: "var(--surface-2)" }}>
          {val > 0 && <div className="h-full rounded-full" style={{ width: `${pct}%`, background: useTokens ? "var(--accent-violet-bg)" : "var(--accent-teal-bg)" }} />}
        </div>
        <div className="flex items-center gap-2.5 text-[9px] font-mono tabular-nums leading-none text-[color:var(--text-muted)] justify-end">
          {tokens > 0 && <span style={{ color: "var(--accent-violet-fg)" }}>{fmtTokens(tokens)} tok</span>}
          {durMs != null && durMs >= 1000 && <span>{fmtDur(durMs)}</span>}
          {!hasCaption && <span className="opacity-40">—</span>}
        </div>
      </div>
      {hasTurns && <span className="text-[10px] font-mono text-[color:var(--text-muted)] inline-block transition-transform flex-none self-start mt-0.5" style={{ transform: open ? "rotate(90deg)" : "none" }}>▸</span>}
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
  if (!rows.length) return <div className="ml-1 pl-3 text-[10px] text-[color:var(--text-muted)] py-1">no turns recorded on this step</div>;
  return (
    <div className="mt-1.5 mb-1 ml-1 pl-3 border-l border-[color:var(--border-default)] space-y-1">
      {rows.map((r, i) => {
        const tone = domainTone("action", r.action ?? "") ?? "slate";
        return (
          <div key={i} className="flex items-baseline gap-2 text-[10.5px] leading-snug">
            <span className="font-mono tabular-nums text-[color:var(--text-muted)] flex-none">{clockISO(r.timestamp)}</span>
            <span className="uppercase tracking-wide text-[8.5px] px-1 py-0.5 rounded flex-none"
              style={{ background: `var(--accent-${tone}-bg)`, color: `var(--accent-${tone}-fg)` }}>
              {(r.action ?? "—").replace(/_/g, " ")}
            </span>
            {r.actor && <span className="text-[9px] font-mono text-[color:var(--text-muted)] flex-none opacity-70">{r.actor}</span>}
            <span className="text-[color:var(--text-secondary)] truncate">{shortDetail(r.details)}</span>
          </div>
        );
      })}
    </div>
  );
}

type GateInfo = { state: "passed" | "override" | "pending" | "future"; g?: GanttGate };

export default function StepRail({
  step, gateState, phase, status, activity, gates, turns, reduced,
}: {
  step?: string;
  gateState?: string;
  phase?: PhaseProgress | null;
  status?: string;
  // Honest work state — the current-step node/% only pulses when "working".
  activity?: Activity | null;
  gates: GanttGate[];
  turns?: StepTurn[];
  reduced?: boolean | null;
}) {
  const steps = WORKFLOW_STEPS_ORDERED;
  const byStep = turnsByStep(turns ?? []);
  const durByStep = stageDurations(turns ?? []);
  // Bar scale: the heaviest stage across the run (so bars are relative to it).
  let maxTokens = 0, maxDur = 0;
  for (const s of steps) {
    const tk = stepTokens(byStep[s.id] ?? []);
    if (tk > maxTokens) maxTokens = tk;
    const d = durByStep[s.id] ?? 0;
    if (d > maxDur) maxDur = d;
  }
  const curIdx = steps.findIndex((s) => s.id === step);
  // Pulse ONLY when genuinely being driven now (a real recent conductor
  // transition on THIS task), never merely because status is in_progress.
  const live = (activity?.state ?? status ?? "").toLowerCase() === "working";
  const [open, setOpen] = useState<string | null>(null);
  const [collapseDone, setCollapseDone] = useState(true);

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
    const stepHasTurns = (byStep[s.id]?.length ?? 0) > 0;
    const isDoneAgent = done && s.type !== "gate";
    // Fold only EMPTY done agent steps; a done step that carries turns stays
    // visible so its drilled-in timeline is reachable (hierarchy over brevity).
    if (collapseDone && isDoneAgent && !stepHasTurns) { pend.push({ s, i }); return; }
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
          className="text-[10px] uppercase tracking-wider font-mono px-2.5 py-1 rounded border border-[color:var(--border-default)] text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]"
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
                  className="text-[10.5px] font-mono text-[color:var(--text-muted)] hover:text-[color:var(--text-secondary)] border border-dashed border-[color:var(--border-default)] rounded-md px-2.5 py-1.5 inline-flex items-center gap-2"
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
                <GateRow s={s} gi={gi} open={rowOpen} onToggle={() => setOpen(rowOpen ? null : s.id)} turns={byStep[s.id] ?? []} durMs={durByStep[s.id]} maxTokens={maxTokens} maxDur={maxDur} />
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
                          {cur && !isGate && verifierPersona && <VerifiedBy persona={verifierPersona} />}
                          {cur && !isGate && (phase?.fanout_dispatched ?? 0) > 0 && (
                            <span className="flex items-center gap-1 text-[10px] font-mono tabular-nums flex-none" style={{ color: "var(--accent-teal-fg)" }}
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
                          {cur && !isGate && (
                            <span className="text-[10px] font-mono tabular-nums flex items-center gap-1.5 flex-none" style={{ color: "var(--accent-teal-fg)" }}>
                              <motion.span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: "var(--accent-teal-fg)" }}
                                animate={!reduced ? { opacity: [1, 0.3, 1] } : { opacity: 1 }}
                                transition={!reduced ? { duration: 1.2, repeat: Infinity } : { duration: 0.2 }} />
                              {phase?.pct != null ? `${((curIdx >= 0 ? (curIdx + Math.min(1, phase.pct)) / steps.length : 0) * 100).toFixed(0)}%` : "working"}
                            </span>
                          )}
                          {!cur && <StepMeta durMs={durByStep[s.id]} tokens={stepTokens(stepTurns)} maxTokens={maxTokens} maxDur={maxDur} hasTurns={hasTurns} open={rowOpen} />}
                          {cur && hasTurns && (
                            <span className="text-[10px] font-mono text-[color:var(--text-muted)] inline-block transition-transform flex-none" style={{ transform: rowOpen ? "rotate(90deg)" : "none" }}>▸</span>
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
      <motion.span className="mt-2 h-3.5 w-3.5 rotate-45 rounded-[3px] grid place-items-center text-[8px] font-bold"
        style={{ background: m.bg, color: m.fg }} {...(gi.state === "pending" ? pulse : {})}>
        <span className="-rotate-45">{m.g}</span>
      </motion.span>
    );
  }
  if (done) return <span className="mt-2 h-3 w-3 rounded-full grid place-items-center text-[8px] font-bold" style={{ background: "var(--accent-emerald-fg)", color: "#06281c" }}>✓</span>;
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
      className="text-[9px] font-mono uppercase tracking-wide px-1.5 py-0.5 rounded flex-none inline-flex items-center gap-1"
      style={{ background: `var(--accent-${tone}-bg)`, color: `var(--accent-${tone}-fg)` }}>
      {isGate && persona && <span aria-hidden>◇</span>}
      {label}
    </span>
  );
}

// "verified by {Role}" — the persona that reviews the UPCOMING gate, shown on
// the current step so the reviewer is legible before the gate is reached. Same
// domainTone("role") palette + ◇ marker as the gate Persona tag (subtle: the
// tag is outlined, not filled, so it reads as a forward-looking hint).
function VerifiedBy({ persona }: { persona: string }) {
  const tone = domainTone("role", persona) ?? "slate";
  return (
    <span
      title={`${personaLabel(persona)} verifies the next gate`}
      className="text-[9px] font-mono uppercase tracking-wide px-1.5 py-0.5 rounded flex-none inline-flex items-center gap-1"
      style={{ color: `var(--accent-${tone}-fg)`, boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)` }}>
      <span aria-hidden>◇</span>
      verified by {personaLabel(persona)}
    </span>
  );
}

function GateRow({ s, gi, open, onToggle, turns, durMs, maxTokens, maxDur }: {
  s: { id: string; persona?: string }; gi: GateInfo; open: boolean; onToggle: () => void; turns?: StepTurn[]; durMs?: number; maxTokens: number; maxDur: number;
}) {
  const [receipt, setReceipt] = useState(false);
  const g = gi.g;
  const override = gi.state === "override";
  return (
    <div>
      <button onClick={onToggle} className="w-full flex items-center gap-2 min-h-[22px] text-left rounded-md px-1.5 -mx-1.5 hover:bg-[color:var(--surface-2)] transition-colors">
        <Persona persona={s.persona ?? ""} isGate />
        <span className="text-[13px] text-[color:var(--text-primary)] whitespace-nowrap">{stepLabel(s.id)}</span>
        {/* A resolved gate needs no "passed" pill or receipt line here — the
            spine node is already a green ✓ (amber ! on override). Show the
            useful stage metric instead; the receipt lives in the panel below. */}
        {gi.state === "pending" ? (
          <span className="ml-auto flex items-center gap-2 flex-none">
            <span className="text-[9px] font-mono font-bold uppercase tracking-wide px-1.5 py-0.5 rounded" style={{ background: "var(--accent-amber-fg)", color: "#3a2a04" }}>pending</span>
            <span className="text-[10px] font-mono text-[color:var(--text-muted)] inline-block transition-transform" style={{ transform: open ? "rotate(90deg)" : "none" }}>▸</span>
          </span>
        ) : (
          <StepMeta durMs={durMs} tokens={stepTokens(turns ?? [])} maxTokens={maxTokens} maxDur={maxDur} hasTurns={(turns?.length ?? 0) > 0} open={open} />
        )}
      </button>
      {open && g && (
        <div className="mt-2 rounded-lg p-3 border"
          style={{
            borderColor: override ? "var(--accent-amber-ring)" : "var(--accent-emerald-ring)",
            background: `linear-gradient(0deg, var(--accent-${override ? "amber" : "emerald"}-bg), transparent)`,
          }}>
          <div className="flex items-center gap-2 flex-wrap text-[10.5px] font-mono text-[color:var(--text-secondary)]">
            <span className="font-bold" style={{ color: override ? "var(--accent-amber-fg)" : "var(--accent-emerald-fg)" }}>
              {override ? "! override" : "✓ verified"}
            </span>
            <span>{g.actor || "—"}</span>
            {g.proof && <span className="px-1.5 py-0.5 rounded text-[9px] uppercase" style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}>{g.proof}</span>}
            <span className="ml-auto text-[color:var(--text-muted)]">{clockHM(g.ts)}</span>
          </div>
          <div className="mt-2 text-[12.5px] text-[color:var(--text-secondary)] leading-relaxed">{g.reason}</div>
          {g.reason && g.reason.length > 120 && (
            <>
              {receipt && (
                <div className="mt-2 border-l-2 pl-2.5 font-mono text-[11px] text-[color:var(--text-muted)] whitespace-pre-wrap leading-relaxed"
                  style={{ borderColor: "var(--border-strong)" }}>{g.reason}</div>
              )}
              <button onClick={() => setReceipt((v) => !v)} className="mt-1.5 text-[10px] font-mono" style={{ color: "var(--accent-teal-fg)" }}>
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
