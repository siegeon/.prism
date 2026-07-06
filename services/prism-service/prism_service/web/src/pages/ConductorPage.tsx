import { useEffect, useState, useCallback, useRef, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, type PillTone } from "@/components/ui";
import { domainTone } from "@/lib/domainTone";
import {
  stepLabel, gateLabel, stepChipClass, WORKFLOW_STEPS_ORDERED,
} from "@/lib/workflowChips";
import { relativeTime } from "@/lib/relativeTime";
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
};

// Honest activity STATE → tile pill label + tone. adrift/stalled append an idle
// mm:ss (from task_motion_s) so the pill says how long it's been dark.
const ACT_TILE: Record<string, { label: string; tone: PillTone }> = {
  working: { label: "working", tone: "teal" },
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

  const load = useCallback(() => {
    api.get<State>(`/api/conductor/state?project=${project}`).then(setData).catch(() => setData(null));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

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
          Workflow-claimed tasks moving through the 8-step SDLC. Each tile's stepper fills to the
          task's current phase (shown top-right) and advances automatically as the conductor drives
          it. Tasks worked without conductor (status flips only) don't appear here. Click a tile to open it.
        </p>
        {managed.length === 0 ? (
          <Empty>No tasks under conductor management. Call conductor_advance on a task to start one.</Empty>
        ) : (
          <div className="grid grid-cols-[repeat(auto-fill,minmax(380px,1fr))] gap-3">
            {managed.map((t) => (
              <TaskTile key={t.id} task={t} reduced={reduced} onClick={() =>
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
// TaskTile — uniform-sized swimlane tile (v6.0.43)
//
// Replaces the variable-width truncated pill that used to render each task
// inside its lane. Sized by the parent's auto-fill CSS grid (minmax 220px)
// so the lane reads as a row of equal cards; each tile then exposes the
// SDLC signal the swimlanes are trying to communicate at a glance:
//   - title (2-line clamp, font-medium)
//   - status badge + gate badge (when gate_state != 'none')
//   - p{priority} . {relative_age} . id {short_id}
//   - owner: {assigned_agent or 'unassigned'}
//   - up to 3 tag chips
// ---------------------------------------------------------------------------
function TaskTile({ task, reduced, onClick }: { task: ManagedTask; reduced: boolean | null; onClick: () => void }) {
  const status = (task.status ?? "").toLowerCase();
  const statusTone: PillTone = domainTone("taskStatus", status) ?? "slate";
  // Honest state drives the pill (fall back to raw status pre-activity).
  const actState = (task.activity?.state ?? status).toLowerCase();
  const actTone: PillTone = ACT_TILE[actState]?.tone ?? statusTone;
  const actWorking = actState === "working";
  const idle = fmtIdle(task.activity?.task_motion_s);
  const actLabel =
    actState === "adrift" ? `session busy${idle ? ` · idle ${idle}` : ""}`
    : actState === "stalled" ? `stalled${idle ? ` · idle ${idle}` : ""}`
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
      className="text-left rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] hover:border-[color:var(--border-strong)] p-3 flex flex-col gap-1.5 transition-colors cursor-pointer"
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
      {/* Animated SDLC stepper — dots fill to the current phase as the task advances. */}
      <SdlcDots step={stepId} reduced={reduced} />
      <div className="flex flex-wrap items-center gap-1">
        <TileBadge tone={actTone}>{actLabel}</TileBadge>
        {showGate && (
          <TileBadge tone={gateTone}>{gateLabel(gate as any)}</TileBadge>
        )}
        {status === "in_progress" && (task.phase_progress?.eta_s ?? 0) > 5 && (
          <span
            className="text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ring-1"
            style={{ background: "var(--accent-teal-bg)", color: "var(--accent-teal-fg)", boxShadow: "inset 0 0 0 1px var(--accent-teal-ring)" }}
            title={`ETA to done — forward-projected from learned per-step medians${task.phase_progress?.eta_sample_n != null ? ` (current step n=${task.phase_progress.eta_sample_n})` : ""}`}
          >
            ~{fmtEtaTile(task.phase_progress!.eta_s!)} left{(task.phase_progress?.eta_sample_n ?? 0) < 2 ? " ~rough" : ""}
          </span>
        )}
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

      {/* Two-column body: identity/meta + SDLC phase on the LEFT, the live
          per-turn burn graph on the RIGHT. The graph is the only token surface
          on the tile (rate), the left is the only meta surface — no overlap. */}
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
      {status === "in_progress" && (task.phase_progress?.eta_s ?? 0) > 5 && (task.phase_progress?.eta_total_s ?? 0) > 0 && (
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

// SdlcDots — the animated SDLC stepper. One node per WORKFLOW_STEPS_ORDERED
// step (circle for an agent/intake step, rounded square for a gate); nodes
// before the current step read "done" (emerald), the current node pulses
// teal with a ring, later nodes are muted. Color/size transition on advance
// so a task visibly moves between phases (motion suppressed under reduced).
function SdlcDots({ step, reduced }: { step?: string; reduced: boolean | null }) {
  const steps = WORKFLOW_STEPS_ORDERED;
  const curIdx = steps.findIndex((s) => s.id === (step ?? ""));
  return (
    <div
      className="flex items-center"
      role="img"
      aria-label={curIdx < 0 ? "SDLC not started" : `SDLC phase ${curIdx + 1} of ${steps.length}: ${stepLabel(steps[curIdx].id)}`}
    >
      {steps.map((s, i) => {
        const done = curIdx >= 0 && i < curIdx;
        const current = i === curIdx;
        const reached = done || current;
        const isGate = s.type === "gate";
        return (
          <div key={s.id} className="flex items-center" title={stepLabel(s.id) || s.id}>
            {i > 0 && (
              <span
                className={reduced ? "" : "transition-colors duration-500"}
                style={{ height: "1px", width: "0.55rem", background: reached ? "var(--accent-emerald-fg)" : "var(--border-default)" }}
              />
            )}
            <span
              className={[
                isGate ? "rounded-[2px]" : "rounded-full",
                current ? "w-2.5 h-2.5" : "w-2 h-2",
                reduced ? "" : "transition-all duration-500",
              ].join(" ")}
              style={{
                background: current ? "var(--accent-teal-fg)" : done ? "var(--accent-emerald-fg)" : "var(--surface-3)",
                boxShadow: current ? "0 0 0 2px var(--accent-teal-ring)" : "none",
              }}
            />
          </div>
        );
      })}
    </div>
  );
}
