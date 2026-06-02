import { useEffect, useState, useCallback, useMemo, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, Kpi, SectionLabel, Empty, type PillTone } from "@/components/ui";
import {
  stepLabel, gateLabel, WORKFLOW_STEPS_ORDERED,
} from "@/lib/workflowChips";
import { relativeTime } from "@/lib/relativeTime";
import { useReducedMotion } from "motion/react";
import SdlcProgress, { type PhaseProgress } from "@/components/conductor/SdlcProgress";
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
};

type State = {
  managed_tasks?: ManagedTask[];
  step_buckets?: Record<string, number>;
};

const PERSONA_TONE: Record<string, string> = {
  sm: "teal",
  dev: "amber",
  qa: "violet",
};

const GATE_TONE: Record<string, PillTone> = {
  pending: "amber",
  passed: "emerald",
  failed: "rose",
};

// Mirrors TasksPage / TaskDetailPage status coloring so tiles read with
// the same status language used everywhere else in the SPA.
const STATUS_TONE: Record<string, PillTone> = {
  pending: "amber",
  in_progress: "teal",
  blocked: "rose",
  done: "emerald",
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

  const pendingGates = useMemo(() => managed.filter(t => t.gate_state === "pending").length, [managed]);
  const failedGates = useMemo(() => managed.filter(t => t.gate_state === "failed").length, [managed]);
  const modalStep = useMemo(() => {
    const buckets: Record<string, number> = {};
    for (const t of managed) {
      const s = t.workflow_step ?? "";
      if (s) buckets[s] = (buckets[s] ?? 0) + 1;
    }
    const entries = Object.entries(buckets);
    if (entries.length === 0) return "—";
    entries.sort((a, b) => b[1] - a[1]);
    return stepLabel(entries[0][0]);
  }, [managed]);

  // Group tasks by their workflow_step; preserves order matching the lane.
  const tasksByStep = useMemo(() => {
    const m: Record<string, ManagedTask[]> = {};
    for (const t of managed) {
      const s = t.workflow_step ?? "";
      if (!s) continue;
      (m[s] ??= []).push(t);
    }
    return m;
  }, [managed]);

  return (
    <Page>
      <section className="flex flex-wrap gap-3">
        <Kpi label="Under management" value={managed.length} />
        <Kpi label="Pending gates" value={pendingGates} />
        <Kpi label="Failed gates" value={failedGates} />
        <Kpi label="Modal step" value={modalStep} />
      </section>

      <Card>
        <SectionLabel>SDLC swimlanes</SectionLabel>
        <p className="text-[11px] opacity-60 mt-1 mb-3">
          A workflow-claimed task lands in <b>Intake</b> the moment it's picked up, then conductor
          forces it through the 8 SDLC steps top-to-bottom. Tasks worked without conductor
          (status flips only) don't appear here. Click a task pill to open it.
        </p>
        <div className="divide-y divide-[color:var(--midground-base)]/10">
          {WORKFLOW_STEPS_ORDERED.map((s) => {
            const isGate = s.type === "gate";
            const isIntake = s.type === "intake";
            const laneTone = isIntake ? "violet" : isGate ? "slate" : (PERSONA_TONE[s.persona] ?? "slate");
            const tasksHere = tasksByStep[s.id] ?? [];
            return (
              <div
                key={s.id}
                className="grid grid-cols-[14rem_1fr] gap-4 items-center py-4"
              >
                <div className="flex items-center gap-2">
                  <span
                    className="text-[10px] uppercase tracking-wider font-mono px-2 py-1 rounded ring-1"
                    style={{
                      background: `var(--accent-${laneTone}-bg)`,
                      color: `var(--accent-${laneTone}-fg)`,
                      boxShadow: `inset 0 0 0 1px var(--accent-${laneTone}-ring)`,
                    }}
                  >
                    {isGate ? `🚪 ${stepLabel(s.id)}` : isIntake ? "⏳ intake" : stepLabel(s.id)}
                  </span>
                  {isIntake && (
                    <span className="text-[10px] uppercase opacity-50 font-mono">claimed</span>
                  )}
                  {!isGate && !isIntake && (
                    <span className="text-[10px] uppercase opacity-50 font-mono">{s.persona}</span>
                  )}
                </div>
                <div className="min-h-[1.5rem]">
                  {tasksHere.length === 0 ? (
                    <span className="text-[11px] opacity-30 italic">empty</span>
                  ) : (
                    <div className="grid grid-cols-[repeat(auto-fill,minmax(380px,1fr))] gap-3">
                      {tasksHere.map((t) => (
                        <TaskTile key={t.id} task={t} reduced={reduced} onClick={() =>
                          navigate(`/tasks/${t.id}`, { state: { from: "/conductor" } })
                        } />
                      ))}
                    </div>
                  )}
                </div>
              </div>
            );
          })}
        </div>
        {managed.length === 0 && (
          <Empty>No tasks under conductor management. Call conductor_advance on a task to start one.</Empty>
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
  const statusTone: PillTone = STATUS_TONE[status] ?? "slate";
  const gate = task.gate_state ?? "none";
  const showGate = gate !== "none";
  const gateTone: PillTone = GATE_TONE[gate] ?? "slate";
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
      {/* Header — title + status/gate, full width above the split. */}
      <div className="text-[13px] leading-snug font-medium line-clamp-2 text-[color:var(--text-primary)]">
        {task.title}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <TileBadge tone={statusTone}>{status || "—"}</TileBadge>
        {showGate && (
          <TileBadge tone={gateTone}>{gateLabel(gate as any)}</TileBadge>
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
          {/* Real-time SDLC phase progress — bar fills the current step from the
              server phase_progress estimate. hideTokens: the graph owns token
              info, so the caption drops its tok readout (no duplication). */}
          <div className="mt-auto pt-2 border-t border-[color:var(--border-default)]/60 flex flex-col gap-1.5">
            <span className="text-[9px] uppercase tracking-[0.12em] font-mono text-[color:var(--text-muted)] opacity-70">SDLC</span>
            <SdlcProgress step={task.workflow_step} phase={task.phase_progress} status={task.status} reduced={reduced} hideTokens />
          </div>
        </div>

        <div className="border-l border-[color:var(--border-default)]/60 pl-3">
          <TokenTurns
            turns={task.phase_progress?.token_turns}
            total={task.phase_progress?.turns}
            live={status === "in_progress"}
            reduced={reduced}
          />
        </div>
      </div>
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
