import { useEffect, useState, useCallback, useRef } from "react";
import { useParams, useNavigate, useLocation } from "react-router-dom";
import { motion, AnimatePresence, useReducedMotion } from "motion/react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, toneFromLabel, type PillTone } from "@/components/ui";
import { domainTone, priorityTone } from "@/lib/domainTone";
import PlanView, { parseAc } from "@/components/plan/PlanView";
import Markdown from "@/components/Markdown";
import { type PhaseProgress, type Activity } from "@/components/conductor/SdlcProgress";
import { type Timeline } from "@/components/conductor/TaskActivityGantt";
import { EASE_OUT, DUR, SPRING_SNAPPY, staggerDelay } from "@/lib/motion";
// "2.9B" / "476.9k" / "512" — compact token count (shared k/M/B formatter).
import { fmtTokens } from "@/lib/format";

// Same status → tone map as TasksPage so the detail-page status chip
// matches the kanban column header it came from.
type Task = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  tags?: string[];
  assigned_agent?: string;
  description?: string;
  story_file?: string;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
  blocked_reason?: string;
  dependencies?: string[];
  workflow_step?: string;
  gate_state?: string;
  gate_reason?: string;
  parent_id?: string;
  oracle?: string;
  proof_type?: string;
  completion_proof?: string;
  likely_misfire?: string;
  full_outcome_complete?: boolean;
  allowed_files?: string[];
  verify?: string[];
  stop_if?: string[];
  plan_doc?: string;
  plan_diagram?: string;
  phase_progress?: PhaseProgress | null;
  // Honest work state — rides top-level on the detail response (see load()).
  activity?: Activity | null;
  has_prototype?: boolean;
};

// One PINNING/RED test surfaced next to the oracle: the committed test whose
// failure currently proves the work is NOT done. Discovered server-side from
// the real test file (GET /api/tasks/:id/tests) — name + its docstring.
type PinTest = {
  name: string;
  doc?: string;
  file?: string;
};

// Slim shape for the child-task list — only what the row renders.
type ChildTask = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  parent_id?: string;
};

// Matches the /api/tasks/:id history rows: an append-only audit log where
// each turn carries who acted (actor), what kind of turn (action), a free-text
// `details` blob (the transition + validation/reason), and a timestamp. The
// API also stamps `turn_tokens` = output_tokens spent in this turn's window
// when the linked-session transcripts are readable (best-effort, often unset).
// (The old shape assumed from_status/to_status/reason fields that the API
// never sends — which is why every row used to render as a bare "— → —".)
type HistoryRow = {
  id?: string | number;
  task_id?: string;
  actor?: string;
  action?: string;
  details?: string;
  timestamp?: string;
  turn_tokens?: number;
};

type SessionRow = {
  session_id: string;
  started_at?: string | null;
  ended_at?: string | null;
  duration_s?: number;
  tokens_used?: number;
  files_read?: number;
  files_modified?: number;
  skills_invoked?: number;
};

// Staggered Card-stack wrapper: each card fades + rises into place with a
// capped per-index delay, so the detail page assembles top-to-bottom on mount
// instead of snapping in all at once. Collapses to opacity-only when reduced.
function Stagger({ i, reduced, children }: { i: number; reduced: boolean | null; children: React.ReactNode }) {
  return (
    <motion.div
      initial={reduced ? { opacity: 0 } : { opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: reduced ? 0 : DUR.enter, ease: EASE_OUT, delay: reduced ? 0 : staggerDelay(i) }}
    >
      {children}
    </motion.div>
  );
}

// Animated checkbox for the child-task checklist — empty square when pending,
// emerald fill + spring-tick check when the child is done (feeds the
// current-segment sub-step fill server-side via children_done/total).
function Checkbox({ done, reduced }: { done: boolean; reduced: boolean | null }) {
  return (
    <span
      className="inline-flex items-center justify-center h-4 w-4 rounded shrink-0"
      style={{
        background: done ? "var(--accent-emerald-bg)" : "var(--surface-3)",
        boxShadow: `inset 0 0 0 1px var(--accent-${done ? "emerald" : "slate"}-ring)`,
      }}
    >
      <AnimatePresence>
        {done && (
          <motion.svg
            key="tick"
            viewBox="0 0 16 16"
            className="h-3 w-3"
            initial={reduced ? { opacity: 0 } : { scale: 0, opacity: 0 }}
            animate={reduced ? { opacity: 1 } : { scale: 1, opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={reduced ? { duration: 0 } : SPRING_SNAPPY}
          >
            <path
              d="M3.5 8.5l3 3 6-6.5"
              fill="none"
              stroke="var(--accent-emerald-fg)"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </motion.svg>
        )}
      </AnimatePresence>
    </span>
  );
}

// Long receipt-style text (gate validation, completion proof) is NOT shown
// inline or in a collapse — clicking the summary row navigates to a DEDICATED
// screen (TaskTextPage at /tasks/:id/:section). oneLine builds that summary.
function oneLine(s: string, n = 96): string {
  const f = s.replace(/\s+/g, " ").trim();
  return f.length > n ? f.slice(0, n) + "…" : f;
}

// ── Timeline (task turns) ──────────────────────────────────────────────
// The History card renders the audit log as a readable turn-by-turn
// timeline: each turn shows its wall-clock time, how long after the
// previous turn it fired (elapsed), who acted, the kind of turn, the
// state transition it carried, the tokens spent in that window (when the
// transcript is readable), and an expandable validation/reason.

// "11:54:41" from an ISO timestamp (keeps the recorded local wall clock).
function clockOf(ts?: string): string {
  return ts ? String(ts).slice(11, 19) : "";
}
// "2026-06-02" from an ISO timestamp.
function dayOf(ts?: string): string {
  return ts ? String(ts).slice(0, 10) : "";
}
// Human gap between two turns: "24s" / "3m 12s" / "1h 4m".
function fmtGap(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}
// Flow fields whose `from -> to` is short enough to show as transition
// pills (everything else is a content edit summarized as "edited <field>").
const FLOW_FIELDS = ["workflow_step", "status", "gate_state", "priority", "assigned_agent"];

type Transition = { field?: string; from?: string; to?: string };

// Pull a short state transition out of the free-text `details` blob, if any.
function parseTransition(action?: string, details?: string): Transition | null {
  const d = details ?? "";
  if (action === "advance_task") {
    const from = /(?:^|;\s*)from=([^;]*)/.exec(d)?.[1]?.trim();
    const to = /(?:^|;\s*)to=([^;]*)/.exec(d)?.[1]?.trim();
    return from || to ? { field: "step", from, to } : null;
  }
  if (action === "gate_decide") {
    const gate = /(?:^|;\s*)gate=([^;]*)/.exec(d)?.[1]?.trim();
    const act = /(?:^|;\s*)action=([^;]*)/.exec(d)?.[1]?.trim();
    return { field: gate ? `gate · ${gate}` : "gate", to: act };
  }
  if (action === "updated") {
    // Flow-field values are simple tokens (no quotes/arrows), so [^']* is safe.
    for (const f of FLOW_FIELDS) {
      const m = new RegExp(`${f}:\\s*'([^']*)'\\s*->\\s*'([^']*)'`).exec(d);
      if (m) return { field: f === "workflow_step" ? "step" : f.replace(/_/g, " "), from: m[1], to: m[2] };
    }
  }
  return null;
}

// Grab the trailing "<key>=..." value (validation / reason live at the end).
function grabKV(key: string, d: string): string {
  return new RegExp(`${key}=([\\s\\S]*)$`).exec(d)?.[1]?.trim() ?? "";
}

// One-line gist of the turn: the validation/reason for advances & gates, the
// title for creation, or "edited <field>" for content updates.
function turnSummary(action?: string, details?: string): string {
  const d = (details ?? "").trim();
  if (action === "advance_task") return grabKV("validation", d);
  if (action === "gate_decide") return grabKV("reason", d) || grabKV("validation", d);
  if (action === "created") return /title='([\s\S]*?)'/.exec(d)?.[1] ?? d;
  if (action === "updated") {
    if (parseTransition(action, d)) return "";
    const f = /^([a-z_]+):/.exec(d)?.[1];
    return f ? `edited ${f.replace(/_/g, " ")}` : d;
  }
  return d;
}

function TonePill({ tone, children }: { tone: PillTone; children: React.ReactNode }) {
  return (
    <span
      className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
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

function StateChip({ children, tone }: { children: React.ReactNode; tone?: PillTone }) {
  return (
    <code
      className="text-2xs font-mono px-1.5 py-0.5 rounded"
      style={{
        background: tone ? `var(--accent-${tone}-bg)` : "var(--surface-2)",
        color: tone ? `var(--accent-${tone}-fg)` : "var(--text-secondary)",
      }}
    >
      {children}
    </code>
  );
}

function TimelineRow({ row, prev, isFirst }: { row: HistoryRow; prev?: HistoryRow; isFirst: boolean }) {
  const [open, setOpen] = useState(false);
  const tone = domainTone("action", row.action ?? "") ?? "slate";
  const trans = parseTransition(row.action, row.details);
  const summary = turnSummary(row.action, row.details);
  const full = (row.details ?? "").trim();
  // Expand is offered when the gist is truncated, or when the raw details
  // carry more than the gist (e.g. a long override reason / plan rewrite).
  const expandable = summary.length > 140 || (full.length > 0 && full !== summary && full.length > 140);

  const gap = prev?.timestamp && row.timestamp
    ? new Date(row.timestamp).getTime() - new Date(prev.timestamp).getTime()
    : NaN;
  const tokens = typeof row.turn_tokens === "number" && row.turn_tokens > 0 ? row.turn_tokens : 0;

  return (
    <li className="relative pl-5 py-3">
      {/* rail dot */}
      <span
        className="absolute left-0 top-[18px] h-2 w-2 rounded-full -translate-x-1/2"
        style={{ background: `var(--accent-${tone}-fg)`, boxShadow: `0 0 0 3px var(--background-base)` }}
      />
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-mono text-[12px] opacity-80">{clockOf(row.timestamp)}</span>
          {isFirst && <span className="font-mono text-2xs opacity-40">{dayOf(row.timestamp)}</span>}
          <TonePill tone={tone}>{(row.action ?? "—").replace(/_/g, " ")}</TonePill>
          {row.actor ? <span className="text-2xs opacity-60 font-mono">{row.actor}</span> : null}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {tokens > 0 && (
            <span
              className="text-2xs font-mono px-1.5 py-0.5 rounded"
              style={{ background: "var(--accent-violet-bg)", color: "var(--accent-violet-fg)" }}
              title="output tokens spent in this turn's window"
            >
              {fmtTokens(tokens)} tok
            </span>
          )}
          {Number.isFinite(gap) && !isFirst && (
            <span className="text-2xs font-mono opacity-40" title="time since previous turn">
              +{fmtGap(gap)}
            </span>
          )}
        </div>
      </div>

      {trans && (
        <div className="flex items-center gap-1.5 mt-1.5 flex-wrap">
          {trans.field && <span className="text-2xs uppercase tracking-wider opacity-40 mr-0.5">{trans.field}</span>}
          {trans.from ? <StateChip>{trans.from}</StateChip> : <span className="text-2xs opacity-40">start</span>}
          <span className="opacity-40 text-[12px]">→</span>
          {trans.to ? <StateChip tone={tone}>{trans.to}</StateChip> : <span className="opacity-40 text-[12px]">—</span>}
        </div>
      )}

      {summary && (
        <button
          type="button"
          disabled={!expandable}
          onClick={() => expandable && setOpen((o) => !o)}
          className={`mt-1.5 text-left w-full text-[12px] leading-relaxed opacity-75 ${expandable ? "hover:opacity-100 cursor-pointer" : "cursor-default"}`}
        >
          {open ? full : oneLine(summary, 140)}
          {expandable && <span className="ml-1.5 opacity-50 text-2xs uppercase tracking-wider">{open ? "less" : "more"}</span>}
        </button>
      )}
    </li>
  );
}

function Timeline({ rows, tokens }: { rows: HistoryRow[]; tokens?: number }) {
  const first = rows[0]?.timestamp;
  const last = rows[rows.length - 1]?.timestamp;
  const span = first && last ? new Date(last).getTime() - new Date(first).getTime() : NaN;
  // Prefer the sum of per-turn attributed tokens; fall back to the task-total
  // the conductor reports (phase_progress.tokens_since_step) when unattributed.
  const attributed = rows.reduce((a, r) => a + (typeof r.turn_tokens === "number" ? r.turn_tokens : 0), 0);
  const headerTokens = attributed > 0 ? attributed : (typeof tokens === "number" ? tokens : 0);
  return (
    <div className="mt-2">
      <div className="flex items-center gap-x-4 gap-y-1 flex-wrap text-2xs opacity-50 mb-1">
        <span>{rows.length} turn{rows.length === 1 ? "" : "s"}</span>
        {Number.isFinite(span) && span > 0 && <span>· spanning {fmtGap(span)}</span>}
        {headerTokens > 0 && <span>· ~{fmtTokens(headerTokens)} tokens total</span>}
      </div>
      <ul className="border-l border-[color:var(--midground-base)]/15 ml-1 divide-y divide-[color:var(--midground-base)]/10">
        {rows.map((h, i) => (
          <TimelineRow key={String(h.id ?? i)} row={h} prev={rows[i - 1]} isFirst={i === 0} />
        ))}
      </ul>
    </div>
  );
}

const STATUS_CYCLE: Record<string, string[]> = {
  pending: ["in_progress", "blocked", "done"],
  in_progress: ["done", "blocked", "pending"],
  blocked: ["in_progress", "pending", "done"],
  done: ["pending"],
};

export default function TaskDetailPage() {
  const { id = "" } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  // `from` is the path the back button returns to. A child opened from its
  // parent's detail carries from=/tasks/<parentId>, so back goes up to the
  // parent rather than all the way out to the board.
  const fromState = (location.state as { from?: string } | null)?.from;
  const from = fromState || "/tasks";
  const backLabel = from === "/conductor"
    ? "back to conductor"
    : from.startsWith("/tasks/")
      ? "back to parent"
      : "back to tasks";
  const [project] = useProject();
  const [task, setTask] = useState<Task | null>(null);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [children, setChildren] = useState<ChildTask[]>([]);
  // The RED tests that pin this task's oracle (empty unless a committed test
  // file names the task) — rendered beneath the oracle panel.
  const [pinTests, setPinTests] = useState<PinTest[]>([]);
  // Clicking the oracle's compact "N RED" summary drives PlanView to its Tests
  // tab (bump the nonce so a repeat click re-fires) and scrolls it into view.
  const [tabRequest, setTabRequest] = useState<{ tab: string; n: number } | null>(null);
  const planRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Operable conductor gate: a REQUIRED reason + an override checkbox feed
  // POST /api/conductor/gate (the same path the MCP conductor_gate tool uses).
  const [gateReason, setGateReason] = useState("");
  const [gateOverride, setGateOverride] = useState(false);
  // Inline title rename (customer bug 11040b39): click the title to edit.
  const [editingTitle, setEditingTitle] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  // Reduced-motion guard for the staggered Card stack, toast, and status flash.
  const reduced = useReducedMotion();
  // Status-chip flash: keyed on the task.status VALUE change (NOT the click).
  // The setStatus handler does a PATCH + reload round-trip, so the value only
  // changes after the server confirms — this effect fires off that confirmed
  // change, decoupled from the optimistic click.
  const prevStatus = useRef<string | undefined>(undefined);
  const [statusFlash, setStatusFlash] = useState(0);
  const taskStatusValue = task?.status;
  useEffect(() => {
    if (taskStatusValue === undefined) return;
    if (prevStatus.current !== undefined && prevStatus.current !== taskStatusValue) {
      setStatusFlash((n) => n + 1);
    }
    prevStatus.current = taskStatusValue;
  }, [taskStatusValue]);

  const load = useCallback(async () => {
    if (!id) return;
    try {
      const d = await api.get<{ task: Task; history: HistoryRow[]; sessions?: SessionRow[]; phase_progress?: PhaseProgress | null; activity?: Activity | null; timeline?: Timeline | null; has_prototype?: boolean }>(
        `/api/tasks/${id}?project=${project}`,
      );
      // phase_progress + activity + has_prototype ride at the TOP LEVEL of the
      // response (not nested in task) — merge onto the task so the SDLC bar, the
      // honest work-state pill, and the prototype iframe read them off task.*.
      setTask(d.task ? { ...d.task, phase_progress: d.phase_progress ?? d.task.phase_progress ?? null, activity: d.activity ?? d.task.activity ?? null, has_prototype: d.has_prototype ?? false } : d.task);
      setHistory(d.history ?? []);
      setSessions(d.sessions ?? []);
      setTimeline(d.timeline ?? null);
      setError(null);
      // Children aren't on the detail payload — derive them from the task
      // list (parent_id === this id). Cheap, and keeps the API unchanged.
      try {
        const all = await api.get<{ tasks: ChildTask[] }>(`/api/tasks?project=${project}`);
        setChildren((all.tasks ?? []).filter((t) => t.parent_id === id));
      } catch {
        setChildren([]);
      }
      // Pinning/RED tests: the committed test file(s) whose failure proves this
      // task is NOT done. Best-effort — a task with no test file yields [].
      try {
        const tr = await api.get<{ tests: PinTest[] }>(`/api/tasks/${id}/tests`);
        setPinTests(tr.tests ?? []);
      } catch {
        setPinTests([]);
      }
    } catch (e) {
      setError((e as Error).message ?? "task not found");
    }
  }, [id, project]);

  // Poll every 5s so the SDLC progress bar, child checklist, and token-effort
  // label update in real-time as the conductor advances the task.
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  const setStatus = async (status: string) => {
    setBusy(true);
    try {
      await fetch(`/api/tasks/${id}?project=${project}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      setNotice(`Moved to ${status}.`);
      load();
    } catch (e) {
      setNotice(`Update failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  // Rename the task. Blank/whitespace drafts are ignored (the server also
  // guards) so a rename can never blank a title.
  const renameTitle = async () => {
    const next = titleDraft.trim();
    setEditingTitle(false);
    if (!next || next === (task?.title ?? "")) return;
    setBusy(true);
    try {
      await fetch(`/api/tasks/${id}?project=${project}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: next }),
      });
      setNotice("Title updated.");
      load();
    } catch (e) {
      setNotice(`Rename failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  // Approve / Reject a pending gate. action='approve' releases the gate (and
  // auto-advances); 'reject' flips gate_state to 'failed' and stores the
  // reason on task.gate_reason. Reason is REQUIRED for both.
  const gateDecide = async (action: "approve" | "reject") => {
    if (!gateReason.trim()) {
      setNotice("A reason is required to resolve the gate.");
      return;
    }
    setBusy(true);
    try {
      const r = await fetch(`/api/conductor/gate?project=${project}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: id,
          action,
          reason: gateReason.trim(),
          override: gateOverride,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (body.ok === false) {
        setNotice(`Gate ${action} refused: ${body.reason ?? "unknown"}`);
      } else {
        setNotice(`Gate ${action}d. ${body.to_step ? `→ ${body.to_step}` : ""}`);
        setGateReason("");
        setGateOverride(false);
      }
      load();
    } catch (e) {
      setNotice(`Gate ${action} failed: ${(e as Error).message ?? e}`);
    } finally {
      setBusy(false);
    }
  };

  // Oracle "N RED" summary → drive PlanView to the Tests tab + scroll to it.
  const showTests = () => {
    setTabRequest((p) => ({ tab: "tests", n: (p?.n ?? 0) + 1 }));
    planRef.current?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
  };

  if (error) {
    return (
      <Page>
        <button
          onClick={() => navigate(from)}
          className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100"
        >
          ← {backLabel}
        </button>
        <Card>
          <Empty>{error}</Empty>
        </Card>
      </Page>
    );
  }

  if (!task) {
    return (
      <Page>
        <Card><Empty>Loading…</Empty></Card>
      </Page>
    );
  }

  const transitions = STATUS_CYCLE[task.status ?? "pending"] ?? [];
  const taskStatus = task.status ?? "pending";
  const statusTone = domainTone("taskStatus", taskStatus) ?? "slate";
  const pTone = priorityTone(task.priority);
  const conductorOn = (task.workflow_step ?? "") !== "" || (task.gate_state ?? "none") !== "none";
  // Only transcript-backed (UUID) sessions are real work sessions. Synthetic
  // gate-actor labels (qa-red-gate-*, *-verifier-*) surface as gate markers in
  // the Activity Gantt, never as bare session rows.
  const realSessions = sessions.filter((s) =>
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s.session_id));

  return (
    <Page>
      <button
        onClick={() => navigate(from)}
        className="text-2xs uppercase tracking-wider opacity-60 hover:opacity-100 self-start"
      >
        ← {backLabel}
      </button>

      <AnimatePresence>
        {notice && (
          <motion.div
            key="notice"
            initial={reduced ? { opacity: 0 } : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={reduced ? { opacity: 0 } : { opacity: 0, y: 12 }}
            transition={{ duration: reduced ? 0 : DUR.enter, ease: EASE_OUT }}
            className="fixed bottom-6 right-6 z-40 max-w-[420px] rounded-md border border-[color:var(--midground-base)]/20 bg-[color:var(--background-base)]/95 backdrop-blur-sm shadow-lg px-4 py-3 text-[12px]"
          >
            {notice}
          </motion.div>
        )}
      </AnimatePresence>

      <div className="flex items-baseline justify-between gap-4">
        <div>
          {editingTitle ? (
            <input
              autoFocus
              className="font-serif text-3xl tracking-tight bg-transparent border-b border-current outline-none w-full"
              value={titleDraft}
              onChange={(e) => setTitleDraft(e.target.value)}
              onBlur={renameTitle}
              onKeyDown={(e) => {
                if (e.key === "Enter") renameTitle();
                if (e.key === "Escape") setEditingTitle(false);
              }}
            />
          ) : (
            <h1
              className="font-serif text-3xl tracking-tight cursor-text hover:opacity-70"
              title="Click to rename"
              onClick={() => { setTitleDraft(task.title ?? ""); setEditingTitle(true); }}
            >
              {task.title ?? "Untitled task"}
            </h1>
          )}
          <div className="flex items-center gap-2 mt-2 flex-wrap">
            <motion.span
              // Flash on the confirmed task.status value change (statusFlash
              // bumps off the load() round-trip, not the optimistic click).
              key={`status-${statusFlash}`}
              initial={reduced ? false : { scale: 1 }}
              animate={reduced ? {} : { scale: [1, 1.12, 1] }}
              transition={{ duration: reduced ? 0 : DUR.chip, ease: EASE_OUT }}
              className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded inline-block"
              style={{
                background: `var(--accent-${statusTone}-bg)`,
                color: `var(--accent-${statusTone}-fg)`,
                boxShadow: `inset 0 0 0 1px var(--accent-${statusTone}-ring)`,
              }}
            >
              {taskStatus}
            </motion.span>
            {typeof task.priority !== "undefined" && (
              <span
                className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded"
                style={{
                  background: `var(--accent-${pTone}-bg)`,
                  color: `var(--accent-${pTone}-fg)`,
                  boxShadow: `inset 0 0 0 1px var(--accent-${pTone}-ring)`,
                }}
              >
                priority {task.priority}
              </span>
            )}
            {task.assigned_agent && (
              <span
                className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded"
                style={{
                  background: "var(--accent-violet-bg)",
                  color: "var(--accent-violet-fg)",
                }}
              >
                {task.assigned_agent}
              </span>
            )}
            {(task.tags ?? []).map((tag) => {
              const tTone = toneFromLabel(tag);
              return (
                <span
                  key={tag}
                  className="text-2xs font-mono px-1.5 py-0.5 rounded"
                  style={{
                    background: `var(--accent-${tTone}-bg)`,
                    color: `var(--accent-${tTone}-fg)`,
                  }}
                >
                  #{tag}
                </span>
              );
            })}
          </div>
        </div>
        <div className="flex flex-wrap gap-1 shrink-0">
          {transitions.map((target) => (
            <button
              key={target}
              disabled={busy}
              onClick={() => setStatus(target)}
              className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded bg-[color:var(--midground-base)]/15 hover:bg-[color:var(--midground-base)]/30 disabled:opacity-40"
            >
              → {target}
            </button>
          ))}
        </div>
      </div>

      {task.blocked_reason && (
        <Card>
          <SectionLabel>Blocked because</SectionLabel>
          <div className="text-sm text-[color:var(--accent-rose-fg)] mt-1">{task.blocked_reason}</div>
        </Card>
      )}

      {(conductorOn || task.plan_doc || task.plan_diagram || task.has_prototype || pinTests.length > 0) && (
        <Stagger i={0} reduced={reduced}>
        <div ref={planRef}>
        <Card>
          <PlanView
            diagram={task.plan_diagram}
            doc={task.plan_doc}
            prototypeSrc={task.has_prototype ? `/api/tasks/${id}/prototype` : undefined}
            reduced={reduced}
            pinTests={pinTests}
            tabRequest={tabRequest}
            conductor={conductorOn ? {
              step: task.workflow_step,
              gateState: task.gate_state,
              gateReason: task.gate_reason,
              phase: task.phase_progress,
              status: task.status,
              activity: task.activity,
              timeline,
              turns: history,
            } : null}
            gate={{
              reason: gateReason,
              setReason: setGateReason,
              override: gateOverride,
              setOverride: setGateOverride,
              decide: gateDecide,
              busy,
            }}
            onValidation={() => navigate(`/tasks/${id}/validation`, { state: { from: `/tasks/${id}` } })}
          />
        </Card>
        </div>
        </Stagger>
      )}

      {!(task.plan_doc || task.plan_diagram || task.has_prototype) && (
        <Stagger i={1} reduced={reduced}>
        <Card>
          <SectionLabel>Description</SectionLabel>
          {task.description ? (
            <div className="mt-2">
              <Markdown text={task.description} />
            </div>
          ) : (
            <Empty>No description.</Empty>
          )}
        </Card>
        </Stagger>
      )}

      {(task.oracle || task.proof_type || task.completion_proof || task.likely_misfire || task.full_outcome_complete !== undefined || pinTests.length > 0) && (
        <Card>
          <SectionLabel>Oracle — observable completion signal</SectionLabel>
          <div className="mt-2 space-y-3 text-[13px]">
            <div className="flex items-start gap-2 flex-wrap">
              {task.proof_type && (
                <span
                  className="text-2xs uppercase tracking-wider px-2 py-0.5 rounded shrink-0"
                  style={{
                    background: "var(--accent-violet-bg)",
                    color: "var(--accent-violet-fg)",
                    boxShadow: "inset 0 0 0 1px var(--accent-violet-ring)",
                  }}
                  title="proof type"
                >
                  {task.proof_type}
                </span>
              )}
              <span className="opacity-90 leading-relaxed">{task.oracle || <span className="opacity-50">— no oracle defined —</span>}</span>
            </div>
            <div>
              <div className="opacity-50 mb-1 text-2xs uppercase tracking-wider">completion proof</div>
              {task.completion_proof
                ? <button
                    onClick={() => navigate(`/tasks/${id}/proof`, { state: { from: `/tasks/${id}` } })}
                    className="flex items-center gap-1.5 text-left w-full group"
                  >
                    <span className="leading-relaxed opacity-80 group-hover:opacity-100 truncate">{oneLine(task.completion_proof)}</span>
                    <span className="opacity-50 group-hover:opacity-100 shrink-0">→</span>
                  </button>
                : <div className="text-[color:var(--accent-amber-fg)] text-[12px]">⚠ not yet recorded — green_gate will flag this</div>}
            </div>
            {task.likely_misfire && (
              <div>
                <div className="opacity-50 mb-1 text-2xs uppercase tracking-wider">likely misfire — how this could pass-but-be-wrong</div>
                <div className="flex items-start gap-1.5 text-[color:var(--accent-amber-fg)] leading-relaxed">
                  <span className="shrink-0">⚠</span>
                  <span className="opacity-90">{task.likely_misfire}</span>
                </div>
              </div>
            )}
            <div>
              <div className="opacity-50 mb-1 text-2xs uppercase tracking-wider">owner outcome — slice vs finished outcome</div>
              {task.full_outcome_complete
                ? <div className="flex items-center gap-1.5 text-[color:var(--accent-emerald-fg)] leading-relaxed">
                    <span className="shrink-0">✓</span>
                    <span className="opacity-90">Owner outcome: complete — the full owner outcome is mapped (slice green, no open children, strong proof)</span>
                  </div>
                : <div className="flex items-center gap-1.5 text-[color:var(--accent-amber-fg)] leading-relaxed">
                    <span className="shrink-0">◐</span>
                    <span className="opacity-90">Owner outcome: slice-only — a green slice is not yet proof the full owner outcome is met</span>
                  </div>}
            </div>
            {pinTests.length > 0 && (() => {
              // One-line, highlight-free summary: "Pinning tests · 3 RED ·
              // AC-1, AC-4, AC-2". Clicking opens the full readable list in the
              // Tests tab of the work panel above (progressive disclosure).
              const acs = pinTests.map((t) => parseAc(t.doc).badge).filter(Boolean) as string[];
              return (
                <div className="pt-3" style={{ borderTop: "1px solid var(--surface-3)" }}>
                  <div className="opacity-50 mb-1 text-2xs uppercase tracking-wider">
                    pinning tests — what currently proves it&apos;s NOT done
                  </div>
                  <button
                    type="button"
                    onClick={showTests}
                    className="flex items-center gap-2 text-left w-full group"
                    title="view the pinning tests, correlated to their acceptance criteria"
                  >
                    <span className="shrink-0 text-[color:var(--accent-rose-fg)]" aria-hidden>✗</span>
                    <span className="leading-relaxed text-[color:var(--text-primary)]">
                      Pinning tests · {pinTests.length} RED
                      {acs.length > 0 && (
                        <span className="text-[color:var(--text-secondary)]"> · {acs.join(", ")}</span>
                      )}
                    </span>
                    <span className="ml-auto text-2xs uppercase tracking-wider text-[color:var(--text-muted)] group-hover:text-[color:var(--text-secondary)] shrink-0">
                      view →
                    </span>
                  </button>
                </div>
              );
            })()}
          </div>
        </Card>
      )}

      {((task.allowed_files?.length ?? 0) > 0 || (task.verify?.length ?? 0) > 0 || (task.stop_if?.length ?? 0) > 0) && (
        <Card>
          <SectionLabel>Worker contract — bounded slice</SectionLabel>
          <div className="mt-2 grid grid-cols-1 md:grid-cols-3 gap-4 text-[12px]">
            {[
              ["allowed_files", task.allowed_files, "emerald"],
              ["verify", task.verify, "teal"],
              ["stop_if", task.stop_if, "rose"],
            ].map(([label, items, tone]) => (
              <div key={label as string}>
                <div className="opacity-50 mb-1 uppercase tracking-wider text-2xs">{label as string}</div>
                {((items as string[] | undefined)?.length ?? 0) > 0 ? (
                  <ul className="space-y-1">
                    {(items as string[]).map((it, i) => (
                      <li key={i} className="font-mono text-2xs px-1.5 py-0.5 rounded inline-block mr-1 mb-1"
                          style={{ background: `var(--accent-${tone as string}-bg)`, color: `var(--accent-${tone as string}-fg)` }}>
                        {it}
                      </li>
                    ))}
                  </ul>
                ) : <span className="opacity-40">—</span>}
              </div>
            ))}
          </div>
        </Card>
      )}

      {task.parent_id && (
        <Card>
          <SectionLabel>Parent</SectionLabel>
          <button
            onClick={() => navigate(`/tasks/${task.parent_id}`, { state: { from: "/tasks" } })}
            className="mt-2 text-sm underline decoration-dotted underline-offset-2 hover:opacity-100 font-mono"
          >
            ↑ {String(task.parent_id).slice(0, 8)}
          </button>
        </Card>
      )}

      {children.length > 0 && (
        <Card>
          <SectionLabel>
            Slices ({children.filter((c) => (c.status ?? "") === "done").length}/{children.length} done)
          </SectionLabel>
          <div className="space-y-2 mt-2">
            {children.map((c) => {
              const cTone = domainTone("taskStatus", c.status ?? "pending") ?? "slate";
              return (
                <button
                  key={c.id}
                  onClick={() => c.id && navigate(`/tasks/${c.id}`, { state: { from: `/tasks/${id}` } })}
                  className="w-full text-left rounded-md border border-[color:var(--midground-base)]/10 bg-[color:var(--background-base)]/30 p-3 hover:border-[color:var(--midground-base)]/40 hover:bg-[color:var(--background-base)]/50 transition-colors flex items-center justify-between gap-3"
                >
                  <span className="flex items-center gap-3 min-w-0">
                    <Checkbox done={(c.status ?? "") === "done"} reduced={reduced} />
                    <span className="text-sm font-medium truncate">{c.title ?? c.id}</span>
                  </span>
                  <span className="flex items-center gap-2 shrink-0">
                    {typeof c.priority !== "undefined" && (
                      <span className="text-2xs opacity-50 font-mono">p{c.priority}</span>
                    )}
                    <span
                      className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded"
                      style={{
                        background: `var(--accent-${cTone}-bg)`,
                        color: `var(--accent-${cTone}-fg)`,
                        boxShadow: `inset 0 0 0 1px var(--accent-${cTone}-ring)`,
                      }}
                    >
                      {c.status ?? "pending"}
                    </span>
                  </span>
                </button>
              );
            })}
          </div>
        </Card>
      )}

      <Card>
        <SectionLabel>Metadata</SectionLabel>
        <div className="grid grid-cols-2 md:grid-cols-3 gap-x-4 gap-y-2 text-[12px] mt-2">
          <div><span className="opacity-50">id:</span> <span className="font-mono break-all">{task.id}</span></div>
          {task.story_file && <div><span className="opacity-50">story:</span> <span className="font-mono">{task.story_file}</span></div>}
          {task.created_at && <div><span className="opacity-50">created:</span> {String(task.created_at).slice(0, 19)}</div>}
          {task.updated_at && <div><span className="opacity-50">updated:</span> {String(task.updated_at).slice(0, 19)}</div>}
          {task.completed_at && <div><span className="opacity-50">completed:</span> {String(task.completed_at).slice(0, 19)}</div>}
          {(task.dependencies ?? []).length > 0 && (
            <div className="col-span-2 md:col-span-3">
              <span className="opacity-50">dependencies:</span>{" "}
              {(task.dependencies ?? []).map((d, i) => (
                <span key={d} className="font-mono">
                  {i > 0 && ", "}
                  <button
                    onClick={() => navigate(`/tasks/${d}`)}
                    className="underline decoration-dotted underline-offset-2 hover:opacity-100"
                  >
                    {d.slice(0, 8)}
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>
      </Card>

      <Card>
        <SectionLabel>Sessions ({realSessions.length})</SectionLabel>
        {realSessions.length === 0 ? (
          <Empty>No Claude sessions linked to this task yet.</Empty>
        ) : (
          <ul className="divide-y divide-[color:var(--midground-base)]/10 mt-2">
            {realSessions.map((s) => (
              <li key={s.session_id} className="py-3">
                <div className="flex items-baseline justify-between gap-3 flex-wrap">
                  <span className="font-mono text-[12px] break-all">{s.session_id}</span>
                  <span className="text-2xs opacity-50">
                    {s.started_at ? String(s.started_at).slice(0, 19) : "—"}
                    {s.ended_at ? ` → ${String(s.ended_at).slice(0, 19)}` : ""}
                  </span>
                </div>
                <div className="flex flex-wrap gap-2 mt-2">
                  {[
                    ["duration", `${s.duration_s ?? 0}s`],
                    ["tokens", fmtTokens(s.tokens_used ?? 0)],
                    ["read", String(s.files_read ?? 0)],
                    ["modified", String(s.files_modified ?? 0)],
                    ["skills", String(s.skills_invoked ?? 0)],
                  ].map(([label, value]) => (
                    <span
                      key={label}
                      className="text-2xs px-2 py-0.5 rounded bg-[color:var(--midground-base)]/10"
                    >
                      <span className="opacity-50">{label}</span>{" "}
                      <span className="font-mono">{value}</span>
                    </span>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {/* The timeline now lives INSIDE the Implementation tab, drilled per
          step (hierarchical). Keep the flat standalone card only for tasks
          that never entered the conductor (no Implementation tab to hold it). */}
      {!conductorOn && history.length > 0 && (
        <Card>
          <SectionLabel>Timeline ({history.length})</SectionLabel>
          <Timeline rows={history} tokens={task.phase_progress?.tokens_since_step} />
        </Card>
      )}
    </Page>
  );
}

