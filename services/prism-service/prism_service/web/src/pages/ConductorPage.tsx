import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, lozengeTone, type PillTone } from "@/components/ui";
import { Lozenge } from "@/components/Lozenge";
import { domainTone } from "@/lib/domainTone";
import { motion, useReducedMotion } from "motion/react";
import { type PhaseProgress, type Activity } from "@/components/conductor/SdlcProgress";

type ManagedTask = {
  id: string;
  title: string;
  workflow_step?: string;
  gate_state?: string;
  gate_reason?: string;
  status?: string;
  // v6.0.43: extra fields backing the uniform tile redesign
  priority?: number;
  assigned_agent?: string;
  created_at?: string;
  updated_at?: string;
  tags?: string[];
  phase_progress?: PhaseProgress | null;
  // Honest work state (server: conductor_service.activity_for). The tile pill
  // + burn graph read THIS, not the raw status — a task nobody is driving must
  // NOT read as a teal "in progress".
  activity?: Activity | null;
  // Epic slices (server: managed_tasks). Done-first ordered non-cancelled
  // children; drives the SLICES hero bar. Empty/absent for leaf tasks.
  subtasks?: { id: string; title: string; status: string }[];
};

// Honest activity STATE → tile pill label + tone. adrift/stalled append an idle
// mm:ss (from task_motion_s) so the pill says how long it's been dark.
const ACT_TILE: Record<string, { label: string; tone: PillTone }> = {
  working: { label: "working", tone: "teal" },
  paused: { label: "paused", tone: "teal" },   // epic between slices — progress, NOT stalled
  awaiting_gate: { label: "awaiting review", tone: "amber" },
  adrift: { label: "session busy", tone: "slate" },
  stalled: { label: "stalled", tone: "rose" },
  done: { label: "done", tone: "emerald" },
  blocked: { label: "blocked", tone: "rose" },
  pending: { label: "pending", tone: "amber" },
};

function fmtIdle(s?: number | null): string {
  if (s == null) return "";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

type BoardHealth = {
  consecutive_low_value?: number;
  reorient?: boolean;
  reason?: string;
};

type State = {
  managed_tasks?: ManagedTask[];
  step_buckets?: Record<string, number>;
  board_health?: BoardHealth;
};

export default function ConductorPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  const reduced = useReducedMotion();
  const [data, setData] = useState<State | null>(null);
  // Liveness: a card that isn't being driven still must VISIBLY tick, or it's
  // indistinguishable from frozen. fetchedAt marks the last successful poll;
  // sinceFetchS counts up every second and RESETS each 5s poll — a heartbeat the
  // eye can see. It also drives the per-second idle clock on paused/adrift tiles.
  const fetchedAt = useRef(0);
  const [sinceFetchS, setSinceFetchS] = useState(0);

  const load = useCallback(() => {
    api.get<State>(`/api/conductor/state?project=${project}`)
      .then((d) => { setData(d); fetchedAt.current = performance.now(); setSinceFetchS(0); })
      .catch(() => setData(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);
  useEffect(() => {
    const t = setInterval(() => setSinceFetchS(Math.max(0, (performance.now() - fetchedAt.current) / 1000)), 1000);
    return () => clearInterval(t);
  }, []);

  const managed = data?.managed_tasks ?? [];
  const boardHealth = data?.board_health;
  const reorient = boardHealth?.reorient === true;
  const lowValueN = boardHealth?.consecutive_low_value ?? 0;

  return (
    <Page>
      {/* GoalBuddy GAP-5: cross-task reorient badge — silent unless the board
          has shipped >= 2 low-confidence slices in a row (composed from the
          per-task '⚠' advisory notes). Hermes primitives, no raw JSON. */}
      {reorient && (
        <Card>
          <div className="flex items-center gap-2">
            <Lozenge tone="warn">reorient</Lozenge>
            <span className="text-[12px] text-[color:var(--text-secondary)]">
              ⚠ {lowValueN} low-confidence slices in a row — reorient toward a milestone
            </span>
          </div>
        </Card>
      )}
      <Card>
        <SectionLabel>Under conductor</SectionLabel>
        <p className="text-2xs opacity-60 mt-1 mb-3">
          Workflow-claimed tasks moving through the SDLC. Each tile leads with a completion ring and a
          labeled phase timeline (the task's current phase shown top-right) that advance automatically as the
          conductor drives it. Tasks worked without conductor (status flips only) don't appear here. Click a tile to open it.
        </p>
        {managed.length === 0 ? (
          <Empty>No tasks under conductor management. Call conductor_work() to pull the next task and start the loop.</Empty>
        ) : (
          // Up to 4 tiles per row, container-relative so it fills to 4 on a wide
          // board and drops to 3/2/1 as width shrinks — independent of viewport/DPR
          // (each column is at least a 4-across share, min 280px).
          <div className="grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(max(280px,calc((100%-3rem)/4)),1fr))]">
            {managed.map((t) => (
              <TaskTile key={t.id} task={t} reduced={reduced} sinceFetchS={sinceFetchS} onClick={() =>
                navigate(`/tasks/${t.id}`, { state: { from: "/conductor" } })
              } />
            ))}
          </div>
        )}
      </Card>
    </Page>
  );
}

// ---------------------------------------------------------------------------
// TaskTile — uniform-sized swimlane tile.
//
// Leads with the approved ring+timeline design (variant C hero: completion
// ring + 2×2 live metric grid; variant D: a labeled phase timeline over the
// REAL WORKFLOW_STEPS_ORDERED) while preserving the honest-activity signals:
// the pill reads task.activity.state (working/awaiting_gate/adrift/stalled),
// the current timeline node pulses only when working and subdivides in fanout,
// the idle clock ticks only when paused/adrift, and the burn graph + ETA are
// gated on real motion so a paused tile never shows a frozen "N left" lie.
// ---------------------------------------------------------------------------
function TaskTile({ task, reduced, sinceFetchS, onClick }: { task: ManagedTask; reduced: boolean | null; sinceFetchS: number; onClick: () => void }) {
  const status = (task.status ?? "").toLowerCase();
  const actState = (task.activity?.state ?? status).toLowerCase();
  const actTone: PillTone = ACT_TILE[actState]?.tone ?? domainTone("taskStatus", status) ?? "slate";
  const actWorking = actState === "working";
  const liveMotionS = task.activity?.task_motion_s != null ? task.activity.task_motion_s + sinceFetchS : null;
  const idle = fmtIdle(liveMotionS);
  const kids = `${task.phase_progress?.children_done ?? 0}/${task.phase_progress?.children_total ?? 0}`;
  // Queued sub-tasks for THIS conductor item — the card is a summary, so it
  // shows the pending count; click the tile to open the task and see the list.
  const qTotal = task.phase_progress?.children_total ?? 0;
  const qDone = task.phase_progress?.children_done ?? 0;
  const qPending = Math.max(0, qTotal - qDone);
  const actLabel =
    actState === "adrift" ? `session busy${idle ? ` · idle ${idle}` : ""}`
    : actState === "stalled" ? `stalled${idle ? ` · idle ${idle}` : ""}`
    : actState === "paused" ? `paused · ${kids} done${idle ? ` · idle ${idle}` : ""}`
    : (ACT_TILE[actState]?.label ?? (status || "—"));
  const gate = task.gate_state ?? "none";
  const showGate = gate !== "none";
  const stepId = task.workflow_step ?? "";
  const gateReason = task.gate_reason?.trim() || "";
  const [showReason, setShowReason] = useState(false);
  const title = `${task.title}\nid: ${task.id}`;
  const curIdx = phaseIndexOf(stepId);
  const curStep = curIdx >= 0 ? SDLC_STEPS[curIdx] : null;
  const worker = task.assigned_agent || "claude-code";
  const handoff = showGate
    ? "paused at gate — awaiting a distinct reviewer"
    : curStep ? `ready → ${worker} on deck for ${curStep.label}`
    : status === "done" ? "all steps complete" : "queued — awaiting first turn";
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } }}
      title={title}
      className="text-left w-full rounded-lg border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:border-[color:var(--border-strong)] p-5 flex flex-col gap-3 transition-colors cursor-pointer"
    >
      {/* Header — title + "conductor task · SDLC drive" + honest status pill. */}
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[17px] font-semibold leading-snug text-[color:var(--text-primary)] line-clamp-1">{task.title}</div>
          <div className="text-2xs font-mono text-[color:var(--text-muted)] mt-0.5">conductor task · SDLC drive</div>
        </div>
        <Lozenge tone={lozengeTone(actTone)} className="shrink-0">{actLabel}</Lozenge>
      </div>
      {/* ring + "N/10 steps complete · the conductor drives this task…" */}
      <TileHero task={task} />
      {/* the 10-step SDLC timeline with gate diamonds + roles */}
      <LabeledTimeline step={stepId} phase={task.phase_progress} reduced={reduced} live={actWorking} />
      {/* HANDOFF strip — which worker is on deck for the current step. */}
      <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/40 px-4 py-3 flex items-center gap-3 min-w-0">
        <span className="font-mono text-2xs uppercase tracking-wider text-[color:var(--text-muted)] shrink-0">Handoff</span>
        <span className="font-mono text-[12.5px] truncate" style={{ color: showGate ? "var(--accent-amber-fg)" : "var(--accent-teal-fg)" }}>{handoff}</span>
      </div>
      {/* QUEUE strip — the count of sub-tasks queued under this item. The card
          summarizes; clicking the tile opens the task where each queued item is
          listed (child-task checklist). Only shown for epics with children. */}
      {qTotal > 0 && (
        <div className="rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-3)]/40 px-4 py-3 flex items-center gap-3 min-w-0">
          <span className="font-mono text-2xs uppercase tracking-wider text-[color:var(--text-muted)] shrink-0">Queue</span>
          <span className="font-mono text-[12.5px] truncate" style={{ color: qPending > 0 ? "var(--accent-teal-fg)" : "var(--accent-emerald-fg)" }}>
            {qPending > 0 ? `${qPending} task${qPending > 1 ? "s" : ""} pending` : "all queued tasks done"}
          </span>
          <span className="ml-auto font-mono text-2xs text-[color:var(--text-muted)] shrink-0">{qDone}/{qTotal} · open →</span>
        </div>
      )}
      {/* worked by + heartbeat. HONEST: the emerald "live Ns" tick renders ONLY
          when the work is actually live (working/awaiting_gate). A stalled/adrift/
          paused tile shows a MUTED grey "board Ns" poll tick instead — it still
          proves the board isn't frozen, but can't be mistaken for work-liveness
          (fixes the "STALLED · IDLE" pill contradicted by a green live tick). */}
      <div className="flex items-center gap-2 text-[12px] flex-wrap">
        <span className="text-[color:var(--text-muted)]">{actWorking ? "worked by:" : "last worked by:"}</span>
        <span className="font-mono text-[12px] px-2 py-0.5 rounded bg-[color:var(--surface-3)] text-[color:var(--text-secondary)] border border-[color:var(--border-default)]">{worker}</span>
        {actWorking ? (
          <span className="ml-auto inline-flex items-center gap-1 text-2xs font-mono text-[color:var(--text-muted)] tabular-nums" title="work is live — recent conductor transition on this task">
            <motion.span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: "var(--accent-emerald-fg)" }}
              animate={reduced ? { opacity: 1 } : { opacity: [1, 0.2, 1] }} transition={reduced ? { duration: 0.2 } : { duration: 1, repeat: Infinity, ease: "easeInOut" }} />
            live {Math.floor(sinceFetchS)}s
          </span>
        ) : (
          <span className="ml-auto inline-flex items-center gap-1 text-2xs font-mono text-[color:var(--text-muted)] opacity-60 tabular-nums" title="board poll only — the task is idle, not being worked">
            <span className="inline-block h-1.5 w-1.5 rounded-full" style={{ background: "var(--text-muted)" }} />
            board {Math.floor(sinceFetchS)}s
          </span>
        )}
      </div>
      <div className="text-2xs text-[color:var(--text-muted)] font-mono">one MCP loop verb · conductor_work (call until done)</div>
      {gateReason && (
        <div className="text-2xs text-[color:var(--text-muted)]">
          <button type="button" onClick={(e) => { e.stopPropagation(); setShowReason((v) => !v); }} className="font-mono uppercase tracking-wider text-2xs opacity-70 hover:opacity-100 transition-opacity">
            {showReason ? "▾" : "▸"} gate reason
          </button>
          {showReason && (
            <p className="mt-1 leading-snug text-[color:var(--text-secondary)] whitespace-pre-wrap break-words">{gateReason}</p>
          )}
        </div>
      )}
    </div>
  );
}


// TileHero — the completion RING (done SDLC steps / total over the REAL
// WORKFLOW_STEPS_ORDERED) beside a 2×2 metric grid. Every value is sourced from
// LIVE task.phase_progress / task.activity (never mock/hardcoded): current
// phase from workflow_step, time-left from eta_s (only while working with work
// left, else "—" so a paused tile shows no frozen lie), throughput from the
// token-turn series, idle from the honest motion clock.
// The tile mirrors the e825e00a prototype: the 10 real SDLC steps, gates
// drawn as [G] diamonds, a role caption under each, and an N/10 ring — "the
// conductor drives this task through its SDLC steps and pauses only at gates".
const SDLC_STEPS: { id: string; label: string; role: string; gate: boolean }[] = [
  { id: "review_previous_notes", label: "Review notes", role: "Steward", gate: false },
  { id: "draft_story", label: "Draft story", role: "Steward", gate: false },
  { id: "story_gate", label: "Story gate", role: "gate", gate: true },
  { id: "verify_plan", label: "Verify plan", role: "Steward", gate: false },
  { id: "plan_gate", label: "Plan gate", role: "gate", gate: true },
  { id: "write_failing_tests", label: "Write failing tests", role: "Verifier", gate: false },
  { id: "red_gate", label: "Red gate", role: "gate", gate: true },
  { id: "implement_tasks", label: "Implement", role: "Builder", gate: false },
  { id: "verify_green_state", label: "Verify green", role: "Verifier", gate: false },
  { id: "green_gate", label: "Green gate", role: "gate", gate: true },
];
function phaseIndexOf(stepId: string): number {
  return SDLC_STEPS.findIndex((s) => s.id === stepId);
}

function TileHero({ task }: { task: ManagedTask }) {
  const stepId = task.workflow_step ?? "";
  const status = (task.status ?? "").toLowerCase();
  const total = SDLC_STEPS.length;
  const curPhaseIdx = phaseIndexOf(stepId);
  const done = curPhaseIdx < 0 ? (status === "done" ? total : 0) : curPhaseIdx;
  const frac = total > 0 ? done / total : 0;
  const SZ = 84, C = SZ / 2, R = 32, STROKE = 7, CIRC = 2 * Math.PI * R;
  return (
    <div className="flex items-center gap-5">
      <div className="relative shrink-0" style={{ width: SZ, height: SZ }} title={`${done} of ${total} steps complete`}>
        <svg width={SZ} height={SZ} viewBox={`0 0 ${SZ} ${SZ}`} role="img" aria-label={`${done} of ${total} steps complete`}>
          <circle cx={C} cy={C} r={R} fill="none" stroke="var(--surface-3)" strokeWidth={STROKE} />
          <circle
            cx={C} cy={C} r={R} fill="none"
            stroke="var(--accent-teal-fg)" strokeWidth={STROKE} strokeLinecap="round"
            strokeDasharray={CIRC} strokeDashoffset={CIRC * (1 - frac)}
            transform={`rotate(-90 ${C} ${C})`}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center leading-none">
          <span className="text-[22px] font-semibold tabular-nums text-[color:var(--text-primary)]">{done}</span>
          <span className="text-2xs text-[color:var(--text-muted)] mt-0.5">of {total}</span>
        </div>
      </div>
      <div className="flex-1 min-w-0">
        <div className="text-[14px] text-[color:var(--text-primary)]">
          <span className="font-semibold tabular-nums">{done}/{total} steps</span>{" "}
          <span className="text-[color:var(--text-muted)]">complete</span>
        </div>
        <div className="text-[12px] text-[color:var(--text-muted)] mt-1 leading-snug">
          the conductor drives this task through its SDLC steps and pauses only at gates
        </div>
      </div>
    </div>
  );
}

// LabeledTimeline — the variant-D phase index: one node per real
// WORKFLOW_STEPS_ORDERED step with a VISIBLE stepLabel caption under each
// (circle for an agent/intake step, rounded square for a gate). Steps before
// the current read done (emerald ✓), the current node is teal and pulses ONLY
// while the task is live (working), later nodes are muted. When the current
// step dispatched sub-agents, its node SUBDIVIDES into one cell per unit
// (returned filled teal) so "5 of 8 back" is visible inline.
function LabeledTimeline({ step, phase, reduced, live }: { step?: string; phase?: PhaseProgress | null; reduced: boolean | null; live: boolean }) {
  const steps = SDLC_STEPS;
  const curIdx = phaseIndexOf(step ?? "");
  const fd = phase?.fanout_dispatched ?? 0;
  const fr = phase?.fanout_returned ?? 0;
  const last = steps.length - 1;
  return (
    <div className="mt-2 pt-3 border-t border-[color:var(--border-default)]">
      <div className="text-2xs uppercase tracking-[0.16em] font-mono text-[color:var(--text-muted)] mb-2.5">SDLC steps · gates pause for a distinct reviewer</div>
      <div
        className="flex items-start"
        role="img"
        aria-label={curIdx < 0 ? "not started" : `Step ${curIdx + 1} of ${steps.length}: ${steps[curIdx].label}`}
      >
        {steps.map((s, i) => {
          const done = curIdx >= 0 && i < curIdx;
          const current = i === curIdx;
          const isGate = s.gate;
          const nodeBg = current ? (isGate ? "var(--accent-amber-fg)" : "var(--accent-teal-fg)")
            : done ? (isGate ? "var(--accent-amber-fg)" : "var(--accent-emerald-fg)") : "var(--surface-3)";
          return (
            <div key={s.id} className="flex flex-col items-center flex-1 min-w-0" title={s.label}>
              <div className="flex items-center w-full">
                {/* left connector */}
                <div className="h-[2px] flex-1 rounded-full" style={{ background: i === 0 ? "transparent" : (i <= curIdx ? "var(--accent-emerald-ring)" : "var(--surface-3)") }} />
                {current && fd > 0 ? (
                  <span className="inline-flex items-center gap-[2px] h-5 px-1 shrink-0" title={`fanout: ${fr}/${fd} sub-agents back`}>
                    {Array.from({ length: Math.min(fd, 8) }).map((_, k) => (
                      <span key={k} style={{ width: "3px", height: "18px", borderRadius: "1px", background: k < fr ? "var(--accent-teal-fg)" : "var(--surface-3)", boxShadow: k < fr ? "none" : "inset 0 0 0 1px var(--border-default)" }} />
                    ))}
                  </span>
                ) : (
                  <motion.span
                    className={(isGate ? "w-4 h-4 rotate-45 rounded-[3px]" : "w-4 h-4 rounded-full") + " grid place-items-center shrink-0"}
                    style={{ background: nodeBg, boxShadow: current ? `0 0 0 3px ${isGate ? "var(--accent-amber-ring)" : "var(--accent-teal-ring)"}` : done ? "none" : "inset 0 0 0 1.5px var(--border-default)" }}
                    animate={!reduced && current && live ? { opacity: [1, 0.45, 1] } : { opacity: 1 }}
                    transition={!reduced && current && live ? { duration: 1.2, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
                  >
                    <span className="text-2xs font-bold leading-none" style={{ transform: isGate ? "rotate(-45deg)" : "none", color: isGate ? "#3a2a04" : "#06281c" }}>
                      {isGate ? "G" : (done ? "✓" : "")}
                    </span>
                  </motion.span>
                )}
                {/* right connector */}
                <div className="h-[2px] flex-1 rounded-full" style={{ background: i === last ? "transparent" : (i < curIdx ? "var(--accent-emerald-ring)" : "var(--surface-3)") }} />
              </div>
              <span
                className="text-2xs leading-tight text-center w-full px-0.5 mt-2 line-clamp-2"
                style={{ color: current ? "var(--accent-teal-fg)" : done ? "var(--text-secondary)" : "var(--text-muted)" }}
              >
                {s.label}
              </span>
              <span className="text-2xs uppercase tracking-wide text-center w-full truncate mt-0.5 text-[color:var(--text-muted)]">
                {s.role}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
