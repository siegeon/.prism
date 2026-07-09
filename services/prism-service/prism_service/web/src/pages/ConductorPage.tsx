import { useEffect, useState, useCallback, useRef, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, type PillTone } from "@/components/ui";
import { domainTone } from "@/lib/domainTone";
import {
  stepLabel, gateLabel, stepChipClass,
} from "@/lib/workflowChips";
import { relativeTime } from "@/lib/relativeTime";
import { fmtTokens } from "@/lib/format";
import { motion, useReducedMotion } from "motion/react";
import { type PhaseProgress, type Activity } from "@/components/conductor/SdlcProgress";
import TokenTurns from "@/components/conductor/TokenTurns";

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

// Strip a leading "Slice X · " / "Slice X: " prefix and truncate so a slice
// chip reads at tile width. Falls back to the short id when a title is empty.
function sliceLabel(s: { id: string; title: string }): string {
  let t = (s.title || "").replace(/^\s*slice\s+\S+\s*[·:\-]\s*/i, "").trim();
  if (!t) t = s.id.slice(0, 6);
  return t.length > 22 ? t.slice(0, 21) + "…" : t;
}

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
            <TileBadge tone="amber">reorient</TileBadge>
            <span className="text-[12px] text-[color:var(--text-secondary)]">
              ⚠ {lowValueN} low-confidence slices in a row — reorient toward a milestone
            </span>
          </div>
        </Card>
      )}
      <Card>
        <SectionLabel>Under conductor</SectionLabel>
        <p className="text-[11px] opacity-60 mt-1 mb-3">
          Workflow-claimed tasks moving through the SDLC. Each tile leads with a completion ring and a
          labeled phase timeline (the task's current phase shown top-right) that advance automatically as the
          conductor drives it. Tasks worked without conductor (status flips only) don't appear here. Click a tile to open it.
        </p>
        {managed.length === 0 ? (
          <Empty>No tasks under conductor management. Call conductor_advance on a task to start one.</Empty>
        ) : (
          <div className="flex flex-col gap-4 items-stretch">
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
  const statusTone: PillTone = domainTone("taskStatus", status) ?? "slate";
  // Honest state drives the pill (fall back to raw status pre-activity).
  const actState = (task.activity?.state ?? status).toLowerCase();
  const actTone: PillTone = ACT_TILE[actState]?.tone ?? statusTone;
  const actWorking = actState === "working";
  // An ETA/countdown is only honest while the tile is ACTUALLY being driven and
  // has work left. A paused/done tile must NOT show "N left" (a frozen lie).
  const _ct = task.phase_progress?.children_total ?? 0;
  const workLeft = _ct === 0 || (task.phase_progress?.children_done ?? 0) < _ct;
  const showEta = actWorking && workLeft;
  // LIVE idle clock: server snapshot + seconds since the last poll, so it ticks
  // up every second instead of freezing between the 5s data refreshes.
  const liveMotionS = task.activity?.task_motion_s != null ? task.activity.task_motion_s + sinceFetchS : null;
  const idle = fmtIdle(liveMotionS);
  const kids = `${task.phase_progress?.children_done ?? 0}/${task.phase_progress?.children_total ?? 0}`;
  const actLabel =
    actState === "adrift" ? `session busy${idle ? ` · idle ${idle}` : ""}`
    : actState === "stalled" ? `stalled${idle ? ` · idle ${idle}` : ""}`
    : actState === "paused" ? `paused · ${kids} done${idle ? ` · idle ${idle}` : ""}`
    : (ACT_TILE[actState]?.label ?? (status || "—"));
  const gate = task.gate_state ?? "none";
  const showGate = gate !== "none";
  const gateTone: PillTone = domainTone("gate", gate) ?? "slate";
  const stepId = task.workflow_step ?? "";
  const phaseLabel = stepId ? stepLabel(stepId) : (status === "done" ? "done" : "queued");
  const priority = task.priority ?? 0;
  const updated = task.updated_at || task.created_at || "";
  const age = relativeTime(updated);
  const shortId = task.id.slice(0, 8);
  const owner = task.assigned_agent || "unassigned";
  const tags = (task.tags ?? []).slice(0, 3);
  const gateReason = task.gate_reason?.trim() || "";
  const [showReason, setShowReason] = useState(false);
  const title = `${task.title}\nid: ${task.id}`;
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onClick}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onClick(); } }}
      title={title}
      className="text-left w-full max-w-[880px] rounded-lg border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:border-[color:var(--border-strong)] p-5 flex flex-col gap-3 transition-colors cursor-pointer"
    >
      {/* Header — title (left) + current SDLC phase (top-right). */}
      <div className="flex items-start justify-between gap-2">
        <div className="text-[13px] leading-snug font-medium line-clamp-2 text-[color:var(--text-primary)]">
          {task.title}
        </div>
        <span className={`${stepChipClass(stepId)} shrink-0 whitespace-nowrap`} title={`SDLC phase: ${phaseLabel}`}>
          {phaseLabel}
        </span>
      </div>
      {/* Real-progress hero for epics: the focused slice + stage, above the
          at-a-glance ring so "which slice" reads first on an epic tile. */}
      {(task.subtasks?.length ?? 0) > 0 && (
        <SlicesBar subtasks={task.subtasks!} stage={phaseLabel} reduced={reduced} />
      )}
      {/* Hero (variant C): completion ring + 2×2 live metric grid. */}
      <TileHero task={task} sinceFetchS={sinceFetchS} />
      {/* Labeled phase timeline (variant D) — replaces the abstract SdlcDots,
          keeping the fanout subdivision + working-only pulse on the current step. */}
      <LabeledTimeline step={stepId} phase={task.phase_progress} reduced={reduced} live={actWorking} />
      <div className="flex flex-wrap items-center gap-1">
        <TileBadge tone={actTone}>{actLabel}</TileBadge>
        {showGate && (
          <TileBadge tone={gateTone}>{gateLabel(gate as any)}</TileBadge>
        )}
        {showEta && (task.phase_progress?.eta_s ?? 0) > 5 && (
          <span
            className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
            style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)", boxShadow: "inset 0 0 0 1px var(--accent-teal-ring)" }}
            title={`ETA to done — forward-projected from learned per-step medians${task.phase_progress?.eta_sample_n != null ? ` (current step n=${task.phase_progress.eta_sample_n})` : ""}`}
          >
            ~{fmtEtaTile(task.phase_progress!.eta_s!)} left{(task.phase_progress?.eta_sample_n ?? 0) < 2 ? " ~rough" : ""}
          </span>
        )}
        {/* Liveness heartbeat: pulses + counts 0..5s and RESETS each 5s poll, so
            the card visibly proves it is live-polling — even a paused/idle tile
            is never mistaken for frozen. */}
        <span
          className="ml-auto inline-flex items-center gap-1 text-[9px] font-mono text-[color:var(--text-muted)] tabular-nums"
          title={`live — data refreshed ${Math.floor(sinceFetchS)}s ago (auto-polls every 5s)`}
        >
          <motion.span
            className="inline-block h-1.5 w-1.5 rounded-full"
            style={{ background: "var(--accent-emerald-fg)" }}
            animate={reduced ? { opacity: 1 } : { opacity: [1, 0.2, 1] }}
            transition={reduced ? { duration: 0.2 } : { duration: 1, repeat: Infinity, ease: "easeInOut" }}
          />
          live {Math.floor(sinceFetchS)}s
        </span>
      </div>
      {gateReason && (
        <div className="text-[11px] text-[color:var(--text-muted)]">
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); setShowReason((v) => !v); }}
            className="font-mono uppercase tracking-wider text-[10px] opacity-70 hover:opacity-100 transition-opacity"
          >
            {showReason ? "▾" : "▸"} gate reason
          </button>
          {showReason && (
            <p className="mt-1 leading-snug text-[color:var(--text-secondary)] whitespace-pre-wrap break-words">
              {gateReason}
            </p>
          )}
        </div>
      )}

      {/* Two-column body: identity/meta on the LEFT, the live per-turn burn
          graph on the RIGHT. The graph is the only token surface on the tile
          (rate), the left is the only meta surface — no overlap. */}
      <div className="mt-1 grid grid-cols-[1fr_44%] gap-3 items-stretch">
        <div className="flex flex-col gap-1.5 min-w-0">
          <div className="text-[11px] font-mono text-[color:var(--text-muted)] truncate">
            p{priority} · {age} · id {shortId}
          </div>
          <div className="text-[11px] text-[color:var(--text-muted)] truncate">
            owner: <span className="text-[color:var(--text-secondary)]">{owner}</span>
          </div>
          {tags.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {tags.map((tag) => (
                <span
                  key={tag}
                  className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded bg-[color:var(--surface-3)] text-[color:var(--text-muted)]"
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>

        <div className="border-l border-[color:var(--border-default)]/60 pl-3">
          <TokenTurns
            turns={task.phase_progress?.token_turns}
            total={task.phase_progress?.turns}
            live={actWorking}
            reduced={reduced}
            tokens_source={task.phase_progress?.tokens_source}
            state={actState}
            session_quiet_s={task.activity?.session_quiet_s}
          />
        </div>
      </div>
      {showEta && (task.phase_progress?.eta_s ?? 0) > 5 && (task.phase_progress?.eta_total_s ?? 0) > 0 && (
        <EtaCountdownBar
          etaS={task.phase_progress!.eta_s!}
          totalS={task.phase_progress!.eta_total_s!}
          reduced={reduced}
        />
      )}
    </div>
  );
}

// Coarse remaining-time label for the tile ETA chip.
function fmtEtaTile(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}

// Prominent ETA countdown bar pinned to the bottom of the tile: a teal fill
// that DRAINS left→right as the projected time-to-done elapses, with the live
// MM:SS remaining centered in it. Ticks every second off a local anchor and
// re-anchors whenever a fresh poll changes eta_s, so it stays honest.
function EtaCountdownBar({ etaS, totalS, reduced }: {
  etaS: number; totalS: number; reduced: boolean | null;
}) {
  const [remaining, setRemaining] = useState(etaS);
  const anchor = useRef({ at: Date.now(), eta: etaS });
  useEffect(() => {
    anchor.current = { at: Date.now(), eta: etaS };
    setRemaining(etaS);
    const id = setInterval(() => {
      const elapsed = (Date.now() - anchor.current.at) / 1000;
      setRemaining(Math.max(0, anchor.current.eta - elapsed));
    }, 1000);
    return () => clearInterval(id);
  }, [etaS]);
  const frac = totalS > 0 ? Math.max(0.015, Math.min(1, remaining / totalS)) : 0;
  const mm = Math.floor(remaining / 60);
  const ss = Math.floor(remaining % 60);
  return (
    <div
      className="mt-2 relative h-5 w-full rounded-sm overflow-hidden"
      style={{ background: "var(--surface-2)" }}
      title="ETA to done — drains as the projected time-to-done elapses (learned per-step medians)"
    >
      <motion.div
        className="absolute inset-y-0 left-0"
        style={{ background: "var(--accent-teal-bg)", boxShadow: "inset 0 0 0 1px var(--accent-teal-ring)" }}
        initial={false}
        animate={{ width: `${frac * 100}%` }}
        transition={{ duration: reduced ? 0 : 1, ease: "linear" }}
      />
      <span
        className="absolute inset-0 grid place-items-center text-[10px] font-mono tabular-nums uppercase tracking-wider"
        style={{ color: "var(--text-secondary)" }}
      >
        ETA {mm}:{String(ss).padStart(2, "0")} left
      </span>
    </div>
  );
}

function TileBadge({ tone, children }: { tone: PillTone; children: ReactNode }) {
  return (
    <span
      className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
      style={{
        background: `var(--accent-${tone}-bg)`,
        color: `var(--accent-${tone}-fg)`,
        boxShadow: `inset 0 0 0 1px var(--accent-${tone}-ring)`,
      }}
    >
      {children}
    </span>
  );
}

// SlicesBar — the REAL-progress hero for an epic tile. Focused slice line: shows
// ONLY the slice we're working on (or the next up) + the stage — not all N
// chips. "Slice 3/4 · ▶ Bidirectional Jira sync · implement". Uses the shared
// Hermes accent vars — no per-surface palette.
function SlicesBar({ subtasks, stage, reduced }: {
  subtasks: { id: string; title: string; status: string }[];
  stage?: string;
  reduced: boolean | null;
}) {
  const done = subtasks.filter((s) => (s.status || "").toLowerCase() === "done").length;
  const active = subtasks.find((s) => (s.status || "").toLowerCase() === "in_progress");
  const next = subtasks.find((s) => (s.status || "").toLowerCase() === "pending");
  const focus = active ?? next;
  const allDone = done === subtasks.length && subtasks.length > 0;
  return (
    <div className="flex items-center gap-2 flex-wrap" title={`${done} of ${subtasks.length} slices done`}>
      <span className="text-[10px] uppercase tracking-[0.14em] font-mono text-[color:var(--text-secondary)] shrink-0">
        Slice <span className="text-[color:var(--accent-emerald-fg)]">{done}</span>/{subtasks.length}
      </span>
      {allDone ? (
        <span className="text-[11px] font-mono" style={{ color: "var(--accent-emerald-fg)" }}>✓ all slices done</span>
      ) : focus ? (
        <span className="inline-flex items-center gap-1.5 min-w-0">
          {active ? (
            <motion.span className="inline-block h-1.5 w-1.5 rounded-full shrink-0" style={{ background: "var(--accent-teal-fg)" }}
              animate={reduced ? { opacity: 1 } : { opacity: [1, 0.3, 1] }}
              transition={reduced ? { duration: 0.2 } : { duration: 1.2, repeat: Infinity, ease: "easeInOut" }} />
          ) : (
            <span className="text-[9px] font-mono uppercase tracking-wide text-[color:var(--text-muted)] shrink-0">next</span>
          )}
          <span className="text-[12px] font-medium truncate max-w-[13rem] text-[color:var(--text-primary)]" title={focus.title}>
            {sliceLabel(focus)}
          </span>
          {active && stage && (
            <span className="text-[9px] font-mono px-1.5 py-0.5 rounded-sm shrink-0"
              style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)", boxShadow: "inset 0 0 0 1px var(--accent-teal-ring)" }}>
              {stage}
            </span>
          )}
        </span>
      ) : null}
    </div>
  );
}

// TileHero — the completion RING (done SDLC steps / total over the REAL
// WORKFLOW_STEPS_ORDERED) beside a 2×2 metric grid. Every value is sourced from
// LIVE task.phase_progress / task.activity (never mock/hardcoded): current
// phase from workflow_step, time-left from eta_s (only while working with work
// left, else "—" so a paused tile shows no frozen lie), throughput from the
// token-turn series, idle from the honest motion clock.
// The tile speaks in HUMAN PHASES, not raw SDLC step-ids (matches the
// approved prototype): the 11 workflow steps fold into a legible lifecycle.
// The step-level detail lives on the task-detail Implementation rail.
const LIFECYCLE_PHASES: { key: string; label: string; steps: string[] }[] = [
  { key: "intake", label: "Intake", steps: ["intake"] },
  { key: "plan", label: "Plan", steps: ["review_previous_notes", "draft_story", "story_gate", "verify_plan", "plan_gate"] },
  { key: "test", label: "Test", steps: ["write_failing_tests", "red_gate"] },
  { key: "build", label: "Build", steps: ["implement_tasks"] },
  { key: "verify", label: "Verify", steps: ["verify_green_state"] },
  { key: "ship", label: "Ship", steps: ["green_gate"] },
];
function phaseIndexOf(stepId: string): number {
  return LIFECYCLE_PHASES.findIndex((p) => p.steps.includes(stepId));
}

function TileHero({ task, sinceFetchS }: { task: ManagedTask; sinceFetchS: number }) {
  const stepId = task.workflow_step ?? "";
  const status = (task.status ?? "").toLowerCase();
  const actState = (task.activity?.state ?? status).toLowerCase();
  const working = actState === "working";
  // Ring + metrics count PHASES, not steps (6-phase lifecycle).
  const total = LIFECYCLE_PHASES.length;
  const curPhaseIdx = phaseIndexOf(stepId);
  const done = curPhaseIdx < 0 ? (status === "done" ? total : 0) : curPhaseIdx;
  const frac = total > 0 ? done / total : 0;

  const pp = task.phase_progress;
  const _ct = pp?.children_total ?? 0;
  const workLeft = _ct === 0 || (pp?.children_done ?? 0) < _ct;
  const curPhase = curPhaseIdx >= 0 ? LIFECYCLE_PHASES[curPhaseIdx].label : status === "done" ? "done" : "queued";
  // Honest time-left: only while actually working with work left.
  const timeLeft = working && workLeft && (pp?.eta_s ?? 0) > 0 ? `~${fmtEtaTile(pp!.eta_s!)}` : "—";
  // Throughput as tok/s (the prototype's unit) — the latest turn's rate off
  // the live token-turn series; "—" when nothing has burned yet.
  const tt = pp?.token_turns ?? [];
  const rate = tt.length ? tt[tt.length - 1].tok_s : 0;
  const throughput = rate > 0 ? `${fmtTokens(Math.round(rate))}/s` : "—";
  // Honest idle: the live motion clock when the server serves activity;
  // "active" while working; else the time since the last board motion.
  const liveMotionS = task.activity?.task_motion_s != null ? task.activity.task_motion_s + sinceFetchS : null;
  const idle = working ? "active"
    : liveMotionS != null ? fmtIdle(liveMotionS)
    : relativeTime(task.updated_at || task.created_at || "");

  const SZ = 84, C = SZ / 2, R = 32, STROKE = 7, CIRC = 2 * Math.PI * R;
  return (
    <div className="mt-1 flex items-center gap-4">
      <div className="relative shrink-0" style={{ width: SZ, height: SZ }} title={`${done} of ${total} phases complete`}>
        <svg width={SZ} height={SZ} viewBox={`0 0 ${SZ} ${SZ}`} role="img" aria-label={`${done} of ${total} phases complete`}>
          <circle cx={C} cy={C} r={R} fill="none" stroke="var(--surface-3)" strokeWidth={STROKE} />
          <circle
            cx={C} cy={C} r={R} fill="none"
            stroke="var(--accent-emerald-fg)" strokeWidth={STROKE} strokeLinecap="round"
            strokeDasharray={CIRC} strokeDashoffset={CIRC * (1 - frac)}
            transform={`rotate(-90 ${C} ${C})`}
          />
        </svg>
        <div className="absolute inset-0 flex flex-col items-center justify-center leading-none">
          <span className="text-[20px] font-mono font-semibold tabular-nums text-[color:var(--text-primary)]">{done}/{total}</span>
          <span className="text-[8px] uppercase tracking-[0.16em] text-[color:var(--text-muted)] mt-0.5">phases</span>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2 flex-1 min-w-0">
        <MetricCell label="Current phase" value={curPhase} />
        <MetricCell label="Time left" value={timeLeft} />
        <MetricCell label="Throughput" value={throughput} />
        <MetricCell label="Idle" value={idle} />
      </div>
    </div>
  );
}

// One cell of the hero's 2×2 live-metric grid: a muted uppercase label over a
// single value, all in canonical --text-* tokens.
function MetricCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md bg-[color:var(--surface-3)]/50 px-2.5 py-1.5 min-w-0">
      <div className="text-[9px] uppercase tracking-[0.12em] font-mono text-[color:var(--text-muted)] truncate">{label}</div>
      <div className="text-[13px] font-mono tabular-nums text-[color:var(--text-secondary)] truncate">{value}</div>
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
  const phases = LIFECYCLE_PHASES;
  const curIdx = phaseIndexOf(step ?? "");
  const fd = phase?.fanout_dispatched ?? 0;
  const fr = phase?.fanout_returned ?? 0;
  const last = phases.length - 1;
  return (
    <div className="mt-2 pt-3 border-t border-[color:var(--border-default)]">
      <div className="text-[9px] uppercase tracking-[0.16em] font-mono text-[color:var(--text-muted)] mb-2.5">Phase timeline</div>
      <div
        className="flex items-start"
        role="img"
        aria-label={curIdx < 0 ? "not started" : `Phase ${curIdx + 1} of ${phases.length}: ${phases[curIdx].label}`}
      >
        {phases.map((p, i) => {
          const done = curIdx >= 0 && i < curIdx;
          const current = i === curIdx;
          return (
            <div key={p.key} className="flex flex-col items-center flex-1 min-w-0" title={p.label}>
              <div className="flex items-center w-full">
                {/* left connector */}
                <div className="h-[3px] flex-1 rounded-full" style={{ background: i === 0 ? "transparent" : (i <= curIdx ? "var(--accent-emerald-ring)" : "var(--surface-3)") }} />
                {current && fd > 0 ? (
                  <span className="inline-flex items-center gap-[2px] h-6 px-1 shrink-0" title={`fanout: ${fr}/${fd} sub-agents back`}>
                    {Array.from({ length: Math.min(fd, 8) }).map((_, k) => (
                      <span key={k} style={{ width: "4px", height: "22px", borderRadius: "2px", background: k < fr ? "var(--accent-teal-fg)" : "var(--surface-3)", boxShadow: k < fr ? "none" : "inset 0 0 0 1px var(--border-default)" }} />
                    ))}
                  </span>
                ) : (
                  <motion.span
                    className="w-6 h-6 rounded-full grid place-items-center shrink-0"
                    style={{
                      background: current ? "var(--accent-teal-fg)" : done ? "var(--accent-emerald-fg)" : "var(--surface-3)",
                      boxShadow: current ? "0 0 0 4px var(--accent-teal-ring)" : done ? "none" : "inset 0 0 0 1.5px var(--border-default)",
                    }}
                    animate={!reduced && current && live ? { opacity: [1, 0.45, 1] } : { opacity: 1 }}
                    transition={!reduced && current && live ? { duration: 1.2, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
                  >
                    {done && <span className="text-[12px] font-bold" style={{ color: "#06281c" }}>✓</span>}
                  </motion.span>
                )}
                {/* right connector */}
                <div className="h-[3px] flex-1 rounded-full" style={{ background: i === last ? "transparent" : (i < curIdx ? "var(--accent-emerald-ring)" : "var(--surface-3)") }} />
              </div>
              <span
                className="text-[11px] leading-tight text-center w-full truncate mt-2"
                style={{ color: current ? "var(--accent-teal-fg)" : done ? "var(--text-secondary)" : "var(--text-muted)" }}
              >
                {p.label}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
