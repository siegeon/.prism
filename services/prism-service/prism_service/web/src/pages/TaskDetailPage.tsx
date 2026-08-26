import { useEffect, useState, useCallback, useRef, useMemo } from "react";
import { useParams, useNavigate, useLocation, Link } from "react-router-dom";
import { motion, AnimatePresence, useReducedMotion } from "motion/react";
import { api, approveDesignPacket } from "@/lib/api";
import { useProject } from "@/lib/project";
import { Page, Card, SectionLabel, Empty, type PillTone } from "@/components/ui";
import { Lozenge, type LozengeTone } from "@/components/Lozenge";
import { EntityChip } from "@/components/EntityChip";
import { domainTone } from "@/lib/domainTone";
import PlanView, { parseAc, gateEvidenceLines } from "@/components/plan/PlanView";
import DecisionPacket from "@/components/plan/DecisionPacket";
import DesignPacket from "@/components/plan/DesignPacket";
import { stepLabel } from "@/lib/workflowChips";
import Markdown from "@/components/Markdown";
import { type PhaseProgress, type Activity } from "@/components/conductor/SdlcProgress";
import { type Timeline } from "@/components/conductor/TaskActivityGantt";
import { EASE_OUT, DUR, SPRING_SNAPPY, staggerDelay } from "@/lib/motion";
// "2.9B" / "476.9k" / "512" — compact token count (shared k/M/B formatter).
import { fmtTokens } from "@/lib/format";
import { relativeTime } from "@/lib/relativeTime";
import SpendPanel, { type SpendData } from "@/components/SpendPanel";
import { gateSeverity } from "../lib/gateSeverity";
import { subscribeStream } from "@/lib/sharedStream";

// The task's real counterpart issue (task a7c989c6) — built server-side from
// the active WorkItemExternalLink, never a client-derived field.
type TaskMirror = {
  provider: string;
  issue: string;
  url: string;
  last_synced_at: string;
  state: string;
};

// Same status → tone map as TasksPage so the detail-page status chip
// matches the kanban column header it came from.
type Task = {
  id?: string;
  title?: string;
  status?: string;
  priority?: number | string;
  tags?: string[];
  // Set by the API for an imported external item whose linked provider context
  // the viewer is not authorized to see — the UI shows a Restricted placeholder
  // instead of the metadata, never inferring authorization client-side.
  restricted?: boolean;
  // external_url (removed, task a7c989c6): had zero backend producers —
  // superseded by `mirror` below, built from the real active-link lookup.
  mirror?: TaskMirror | null;
  // mirrors (task 6fbbec35): ALL active counterparts, plural — `mirror`
  // survives as a read-only mirrors[0] alias so a task linked to both
  // github and jira shows both, not just the first.
  mirrors?: TaskMirror[];
  assigned_agent?: string;
  description?: string;
  story_file?: string;
  created_at?: string;
  updated_at?: string;
  completed_at?: string;
  blocked_reason?: string;
  dependencies?: string[];
  workflow_step?: string;
  // Which PRISM workflow drives this task (task af396b2c). Absent only for
  // an older service; the rail header falls back to "implement".
  workflow?: string;
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
  // Honest per-field, per-turn-model dollar spend — rides top-level too
  // (see load()). null/undefined = no linked-session transcript usage yet.
  spend?: SpendData | null;
  // Server-scoped per-step token totals (api.tasks._step_token_totals),
  // windowed to each step's own [entry, exit) span — rides top-level too.
  step_tokens?: Record<string, number>;
};

// One PINNING/RED test surfaced next to the oracle: the committed test whose
// failure currently proves the work is NOT done. Discovered server-side from
// the real test file (GET /api/tasks/:id/tests) — name + its docstring.
type PinTest = {
  name: string;
  doc?: string;
  file?: string;
  status?: string;
  passed?: boolean;
  verified_by?: string;
};

// GET /api/tasks/:id/tests → `receipt`: the gate's trusted-runner
// EvidenceReceipt that the pin statuses reflect (task 45e04fad trust fix) —
// so the Tests tab shows the SAME result the gate observed, and cites it.
type TestReceipt = {
  job_id: string;
  tree_sha: string;
  passed: boolean;
  status: string;
  ended_at: string;
  runner: string;
  reason: string;
  // Freshness (owner 2026-07-21). A receipt is bound to the tree it ran
  // against; when tree_sha != current_tree_sha its counts are HISTORY, not the
  // current verdict, and the card must never present them as "now".
  current_tree_sha?: string;
  stale?: boolean;
} | null;

// GET /api/conductor/gate/readiness — the evidence tooth evaluated LIVE, so
// the gate card never contradicts itself with a stale stored decision.
type GateReadiness = {
  receipt_ok: boolean;
  receipt_refusal?: string;
  // A human review is the ACCEPTED evidence route for this proof_type
  // (owner 2026-07-29: "damnnit, how are we at a green wait state") — this
  // does NOT mean a machine checked anything. Combined with
  // receipt.adapter === "human" it names the bare-entitlement receipt (no
  // machine tooth ran at all); an epic roll-up sets this too but over REAL
  // aggregated child evidence (adapter="epic-rollup"), so it is NOT this case.
  manual_review?: boolean;
  // A passing-but-unshipped receipt (task 8a06e121): the oracle genuinely
  // passed, but this task's own [task:<id8>] commit has not reached
  // origin/main yet — not stale, not failed, not missing. ship_on_approve
  // distinguishes an enabled ship-on-click card from a flat refusal.
  unshipped?: boolean;
  ship_on_approve?: boolean;
  receipt?: {
    adapter?: string;
    passed?: boolean;
    status?: string;
    ended_at?: string;
    reason?: string;
  };
  // Receipt-backed test rows (task b8703343): one per pytest id in the
  // task's derived oracle — the tests that DECIDE this gate. passed=null
  // means listed-but-unevidenced (no matching receipt yet).
  tests?: {
    id: string; label: string; href: string;
    passed: boolean | null; status: string;
    ended_at: string; receipt_job_id: string;
  }[];
  // Epic-rollup adapter (task a646cbd1): the specific unfinished child(ren)
  // blocking a green_gate roll-up, named by id/title so the banner can link
  // them instead of only quoting "N child task(s) not done" prose.
  blocking_children?: { id: string; title: string }[];
};

// GET /api/okf/task_concepts — the OKF concepts this task recalled (from the
// memory recall_log), resolved to live Understand nodes. Backs the rail's
// "Knowledge · Understand" group; each row deep-links to /understand?concept=.
type KnowledgeConcept = {
  id: string;
  title: string;
  type: string;
  domain?: string;
  path?: string;
  recall_count?: number;
  last_recalled?: string;
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
  // Server-resolved identity behind `actor` (api/tasks.py:712-724,
  // actor_service.py) — additive alongside the raw string. kind is
  // "machine" | "human" | "agent" | "unknown" (models/actor.py:15-22).
  actor_identity?: {
    id?: string;
    kind?: string;
    display_name?: string;
    user_id?: string;
    external_ref?: string;
  };
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

// GET /api/tasks/:id/trace — the drive-scoped token trace backing the Trace
// tab: agent_runs grouped session → SDLC step, tokens on every row.
type TraceStep = {
  step: string;
  role?: string | null;
  model?: string | null;
  tokens: number;
  gate_state?: string | null;
  // Provenance of a PASSING gate_state, derived server-side: "machine" (the
  // conductor's own seat, server-clock stamped) vs "unattributed" (written
  // by a producing actor before the ingest refusal landed).
  gate_source?: string | null;
  ts?: string | null;
};
type TraceSession = {
  session_id: string;
  tokens_total: number;
  steps: TraceStep[];
};
type TaskTrace = {
  sessions: TraceSession[];
  totals: { tokens: number; steps: number; sessions: number };
};

// Staggered Card-stack wrapper: each card fades + rises into place with a
// capped per-index delay, so the detail page assembles top-to-bottom on mount
// instead of snapping in all at once. Collapses to opacity-only when reduced.
// Controlled disclosure, replacing native <details>/<summary> (owner QA
// finding, 2026-08-24: on the Evidence tab, "Acceptance criteria", "how a
// pass could still be wrong" and "audit detail" all rendered their body
// content visibly EXPANDED on a genuinely fresh page load — confirmed
// reproducible across full remounts, not a stale-state or screenshot-
// timing artifact, and confirmed NOT present in DecisionPacket.tsx's own
// Row component next to it on the same page, which uses this exact
// controlled-state pattern and collapses correctly. Root cause in the
// live tab's native <details> handling wasn't pinned down; this sidesteps
// it with the pattern already proven correct elsewhere on this page,
// rather than continuing to rely on an element behaving inconsistently.
function Disclosure({ summary, summaryClassName, summaryStyle, className, children }: {
  summary: React.ReactNode; summaryClassName?: string; summaryStyle?: React.CSSProperties;
  className?: string; children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className={className}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={`cursor-pointer text-left ${summaryClassName ?? ""}`}
        style={summaryStyle}
        aria-expanded={open}
      >
        {summary}
      </button>
      {open && children}
    </div>
  );
}

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

// EntityChip is single-line (no truncation), so a repo path is shortened to
// its last two segments for the 300px rail; the full path rides in `title`.
function shortPath(p: string): string {
  const parts = p.split("/").filter(Boolean);
  return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : p;
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

// Actor identity chip: a machine seat (conductor-adjudicator etc, resolved
// server-side against actor_service.MACHINE_SEATS) renders visibly
// differently from a human approval at GLANCE level — a gate_decide row
// used to print the raw actor string as opaque mono text with no cue who
// (or what) decided it (task 934af569). Branches on actor_identity.kind,
// never on the raw actor string, so a newly added machine seat is picked
// up for free without a client-side edit.
function ActorIdentityChip({ actor, identity }: { actor?: string; identity?: HistoryRow["actor_identity"] }) {
  if (!actor) return null;
  const kind = identity?.kind;
  const label = identity?.display_name || actor;
  if (kind === "machine") {
    return (
      <span
        className="text-2xs font-mono px-1.5 py-0.5 rounded uppercase tracking-wider"
        style={{
          background: "var(--accent-violet-bg)",
          color: "var(--accent-violet-fg)",
          boxShadow: "inset 0 0 0 1px var(--accent-violet-ring)",
        }}
        title="machine seat"
      >
        machine · {label}
      </span>
    );
  }
  if (kind === "human") {
    return <span className="text-2xs opacity-60 font-mono">{label}</span>;
  }
  // agent / unknown / unresolved — deliberately distinct from BOTH the
  // machine tone and the plain human text, so it never gets mistaken for
  // either (AC-4: nothing may silently collapse into "looks machine").
  return (
    <span
      className="text-2xs font-mono px-1.5 py-0.5 rounded opacity-70"
      style={{ background: "var(--surface-2)", color: "var(--text-secondary)" }}
      title="unresolved actor identity"
    >
      {label}
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
          <ActorIdentityChip actor={row.actor} identity={row.actor_identity} />
          {row.action === "gate_decide" && summary && (
            <span
              className="text-2xs opacity-70 truncate max-w-[240px]"
              title={summary}
            >
              {oneLine(summary, 64)}
            </span>
          )}
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

// ── Lozenge tone mappers ───────────────────────────────────────────────
// The header + Details rail speak the six-tone Lozenge vocabulary
// (neutral/info/ok/warn/danger/new), so task lifecycle values map here
// rather than through domainTone's seven Hermes hues.
function statusLoz(s: string): LozengeTone {
  return s === "done" ? "ok" : s === "in_progress" ? "info" : s === "blocked" ? "danger" : "warn";
}
function gateLoz(g: string): LozengeTone {
  return g === "passed" ? "ok" : g === "failed" ? "danger" : g === "pending" ? "warn" : "neutral";
}
function priorityLoz(p: number | string | undefined): LozengeTone {
  const n = typeof p === "number" ? p : Number(p);
  if (!Number.isFinite(n)) return "neutral";
  return n <= 1 ? "danger" : n === 2 ? "warn" : n === 3 ? "info" : "neutral";
}

// ── Rail primitives (artifact .railcard / .rt / .field / .relrow) ───────
// Bordered panel on the right column; a header strip, 88px-label field
// rows, and typed group subheaders above EntityChip rows.
function RailCard({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-[color:var(--border-default)] bg-[color:var(--surface-1)] overflow-hidden">
      <div className="flex items-center gap-2 px-3.5 pt-3 pb-1 text-2xs font-semibold uppercase tracking-[0.1em] text-[color:var(--text-label)]">
        {title}
        {typeof count === "number" && <span className="ml-auto font-mono text-[color:var(--text-muted)] normal-case tracking-normal">{count}</span>}
      </div>
      <div className="pb-1.5">{children}</div>
    </div>
  );
}

function Field({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-2.5 px-3.5 py-1.5">
      <span className="w-[88px] shrink-0 text-xs text-[color:var(--text-muted)]">{k}</span>
      <span className="min-w-0 flex items-center gap-1.5 text-[13px] text-[color:var(--text-primary)]">{children}</span>
    </div>
  );
}

// Typed-relation group: a small subheader then one padded row per chip.
function RelGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <>
      <div className="px-3.5 pt-2.5 pb-1 text-2xs font-semibold uppercase tracking-[0.08em] text-[color:var(--text-muted)] opacity-80">{label}</div>
      {children}
    </>
  );
}

function RelRow({ children, why }: { children: React.ReactNode; why?: string }) {
  return (
    <div className="flex items-center gap-2 px-3.5 py-1 min-w-0 hover:bg-[color:var(--surface-2)]">
      <span className="min-w-0">{children}</span>
      {why && <span className="ml-auto shrink-0 text-2xs text-[color:var(--text-muted)]">{why}</span>}
    </div>
  );
}

// ── Trace tab (artifact traceBody / .trace-kpis / .kpi / .trow / .tokbar) ──
// SDLC role → the friendly actor name + Lozenge tone the artifact's KPI tiles
// speak (Steward/Verifier/Builder). Unknown roles fall through to neutral.
function roleName(r?: string | null): string {
  return r === "sm" ? "Steward" : r === "qa" ? "Verifier" : r === "dev" ? "Builder" : (r || "—");
}
function roleLoz(r?: string | null): LozengeTone {
  return r === "sm" ? "new" : r === "qa" ? "warn" : r === "dev" ? "info" : "neutral";
}

// One KPI tile (.kpi): tiny label, big tabular value, faint hint.
function TraceKpi({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="rounded-lg border border-[color:var(--border-default)] bg-[color:var(--surface-1)] px-3.5 py-3">
      <div className="text-2xs font-semibold uppercase tracking-[0.08em] text-[color:var(--text-label)]">{label}</div>
      <div className="text-xl font-[650] tabular-nums mt-0.5 text-[color:var(--text-primary)]">{value}</div>
      {hint && <div className="text-2xs text-[color:var(--text-muted)] mt-0.5">{hint}</div>}
    </div>
  );
}

// One indented step row (.trow.lvl1): step name, role Lozenge, model, an
// optional gate Lozenge, then a FULL-WIDTH token track whose fill is the
// step's share of the session's max step (owner 2026-07-16: "the blue bars
// should be proportional and much longer"). `max` is that session's largest
// step-token value. A genuinely token-less row renders "—" and no bar —
// never a dishonest "0 tok" stub.
function TraceStepRow({ step, max }: { step: TraceStep; max: number }) {
  const tokens = step.tokens || 0;
  const pct = Math.min(100, Math.max(tokens > 0 ? 1.5 : 0, (100 * tokens) / Math.max(1, max)));
  const gate = step.gate_state && step.gate_state !== "none" ? step.gate_state : "";
  return (
    <div className="flex items-center gap-2.5 pl-6 py-1 min-w-0 hover:bg-[color:var(--surface-2)]">
      <span className="truncate text-[13px] text-[color:var(--text-primary)] shrink-0 max-w-[220px]">{step.step}</span>
      <Lozenge tone={roleLoz(step.role)}>{roleName(step.role)}</Lozenge>
      {step.model && (
        <span className="font-mono text-2xs text-[color:var(--text-muted)] truncate max-w-[140px] shrink-0" title={step.model}>{step.model}</span>
      )}
      {gate && <Lozenge tone={gateLoz(gate)}>{gate}</Lozenge>}
      {step.gate_source && (
        <Lozenge tone={step.gate_source === "machine" ? "info" : "warn"} className="shrink-0">
          {step.gate_source === "machine" ? "machine seat" : "unattributed"}
        </Lozenge>
      )}
      <span className="flex items-center gap-2 flex-1 min-w-[110px]">
        <span className="h-[5px] flex-1 rounded-[3px]" style={{ background: tokens > 0 ? "var(--surface-3)" : "transparent" }}>
          {tokens > 0 && (
            <span className="block h-full rounded-[3px]" style={{ width: `${pct}%`, background: "var(--accent-teal-fg)", opacity: 0.75 }} />
          )}
        </span>
        <span className="font-mono text-2xs tabular-nums text-[color:var(--text-muted)] w-[56px] text-right shrink-0">
          {tokens > 0 ? fmtTokens(tokens) : "—"}
        </span>
      </span>
    </div>
  );
}

// Evidence screenshots cited in the completion proof (![alt](src) markdown) —
// rendered wherever the proof is judged, so the approver SEES what the proof
// cites right on the task page (owner 2026-07-16: "i still dont see the
// screenshot on the task"). Click opens the full-size image.
// A cited artifact is not always a picture: a pytest log, a diff, or a JSON
// receipt is evidence too. Before this, EVERY citation was handed to <img>, so
// a cited .txt painted a BROKEN TILE under "visual evidence" (task 63d0086e).
function ProofText({ src, full }: { src: string; full?: boolean }) {
  const [body, setBody] = useState<string>("");
  useEffect(() => {
    let cancelled = false;
    fetch(src)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((t) => { if (!cancelled) setBody(t); })
      .catch(() => { if (!cancelled) setBody("(could not read artifact)"); });
    return () => { cancelled = true; };
  }, [src]);
  return (
    <pre
      className={"m-0 p-3 font-mono whitespace-pre-wrap break-words rounded-md border border-[color:var(--border-default)] " +
        (full
          ? "text-[12px] leading-relaxed max-h-[88vh] overflow-auto"
          : "text-2xs leading-snug max-h-[380px] overflow-hidden")}
      style={{ background: "var(--surface-1)", color: "var(--text-secondary)" }}
    >
      {body || "loading…"}
    </pre>
  );
}

const TEXT_PROOF_RE = /\.(txt|log|diff|patch|md|json)(\?.*)?$/i;

function ProofShots({ text, className }: { text?: string; className?: string }) {
  const shots = useMemo(
    () => [...(text ?? "").matchAll(/!\[([^\]]*)\]\(([^)\s]+)\)/g)].map(([, alt, src]) => ({
      alt, src, kind: TEXT_PROOF_RE.test(src) ? "text" as const : "image" as const,
    })),
    [text],
  );
  // In-app lightbox (owner 2026-07-16: "not redirect to an image, its hard
  // to get back on a desktop") — Esc / click closes, ←/→ steps between shots.
  const [open, setOpen] = useState<number | null>(null);
  // Fit ↔ 100% zoom toggle (owner: "when we are looking at the image we need
  // to see it") — fit fills the viewport; clicking the image shows it at
  // natural size with scrolling.
  const [zoom, setZoom] = useState(false);
  useEffect(() => { setZoom(false); }, [open]);
  useEffect(() => {
    if (open === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(null);
      if (e.key === "ArrowRight") setOpen((v) => (v === null ? v : (v + 1) % shots.length));
      if (e.key === "ArrowLeft") setOpen((v) => (v === null ? v : (v + shots.length - 1) % shots.length));
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, shots.length]);
  if (shots.length === 0) return null;
  const shown = open !== null ? shots[open] : null;
  // Side-by-side grid (owner 2026-07-16), one column only on narrow screens.
  return (
    <div className={`grid grid-cols-1 min-[720px]:grid-cols-2 gap-3 items-start ${className ?? "mt-3"}`}>
      {shots.map(({ alt, src, kind }, i) => (
        <button key={i} type="button" onClick={() => setOpen(i)} className="block min-w-0 text-left"
                title={`${alt || "evidence"} — click to view`}>
          {/* Natural-size box (w-auto, never w-full): the border hugs the
              actual pixels, so a tall image can't letterbox dead space into
              a wide cell (owner 2026-07-16: "look at the dead space"). */}
          {kind === "text" ? (
            <ProofText src={src} />
          ) : (
            <img
              src={src}
              alt={alt || "evidence screenshot"}
              loading="lazy"
              className="max-w-full max-h-[380px] w-auto rounded-md border border-[color:var(--border-default)]"
            />
          )}
          {/* The caption is the proof's own claim — the APP says what the
              image proves (owner 2026-07-16), the reader never has to guess. */}
          {alt && (
            <div className="text-xs mt-1.5 leading-relaxed" style={{ color: "var(--text-secondary)" }}>
              <span className="text-2xs uppercase tracking-wider mr-1.5 font-semibold" style={{ color: "var(--accent-teal-fg)" }}>proves</span>
              {alt}
            </div>
          )}
        </button>
      ))}
      {shown && (
        <div
          className="fixed inset-0 z-50"
          style={{ background: "rgba(0,0,0,0.88)" }}
          role="dialog"
          aria-modal="true"
          aria-label={shown.alt || "evidence screenshot"}
          onClick={() => setOpen(null)}
        >
          {shown.kind === "text" ? (
            // A log opens as SCROLLABLE TEXT — never the zoomable image viewer.
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 p-4">
              <div
                className="w-[min(1100px,96vw)] max-h-[88vh] overflow-auto"
                onClick={(e) => e.stopPropagation()}
              >
                <ProofText src={shown.src} full />
              </div>
              {shown.alt && (
                <div className="text-[13px] leading-relaxed text-center max-w-[80vw]" style={{ color: "rgba(255,255,255,0.92)" }}>
                  <span className="text-2xs uppercase tracking-wider mr-1.5 font-semibold" style={{ color: "var(--accent-teal-fg)" }}>proves</span>
                  {shown.alt}
                </div>
              )}
            </div>
          ) : zoom ? (
            <div className="absolute inset-0 overflow-auto">
              <img
                src={shown.src}
                alt={shown.alt || "evidence screenshot"}
                className="block mx-auto my-4 cursor-zoom-out max-w-none"
                onClick={(e) => { e.stopPropagation(); setZoom(false); }}
              />
            </div>
          ) : (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 p-2">
              <img
                src={shown.src}
                alt={shown.alt || "evidence screenshot"}
                className="max-w-[98vw] max-h-[92vh] object-contain rounded-sm shadow-2xl cursor-zoom-in"
                onClick={(e) => { e.stopPropagation(); setZoom(true); }}
              />
              <div className="flex flex-col items-center gap-1 max-w-[80vw]">
                {shown.alt && (
                  <div className="text-[13px] leading-relaxed text-center" style={{ color: "rgba(255,255,255,0.92)" }}>
                    <span className="text-2xs uppercase tracking-wider mr-1.5 font-semibold" style={{ color: "var(--accent-teal-fg)" }}>proves</span>
                    {shown.alt}
                  </div>
                )}
                <div className="flex items-center gap-3 text-2xs" style={{ color: "rgba(255,255,255,0.6)" }}>
                  {shots.length > 1 && <span className="font-mono tabular-nums">{(open ?? 0) + 1} / {shots.length} · ← →</span>}
                  <span className="uppercase tracking-wider shrink-0">click image to zoom · esc closes</span>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// The Trace tab body: 4 KPI tiles + a per-session tree (session header row →
// indented step rows). Honest empty state when the task has no agent_runs.
function TraceView({ trace, loading, spend }: { trace: TaskTrace | null; loading: boolean; spend?: SpendData | null }) {
  if (loading && !trace) return <Card><Empty>Loading trace…</Empty></Card>;
  if (!trace || trace.sessions.length === 0) {
    return <Card><Empty>No trace yet — this task has no recorded agent runs.</Empty></Card>;
  }
  const t = trace.totals;
  const perStep = t.steps > 0 ? Math.round(t.tokens / t.steps) : 0;
  return (
    <div className="space-y-4">
      <SpendPanel spend={spend} />
      <div className="grid grid-cols-2 min-[560px]:grid-cols-4 gap-3">
        <TraceKpi label="Total tokens" value={fmtTokens(t.tokens)} hint="across this task's drives" />
        <TraceKpi label="Steps" value={String(t.steps)} />
        <TraceKpi label="Sessions" value={String(t.sessions)} />
        <TraceKpi label="Tokens / step" value={fmtTokens(perStep)} />
      </div>
      <Card>
        <div className="divide-y divide-[color:var(--border-default)]">
          {trace.sessions.map((s) => {
            const max = Math.max(1, ...s.steps.map((st) => st.tokens || 0));
            return (
              <div key={s.session_id} className="py-1.5 first:pt-0 last:pb-0">
                <div className="flex items-center gap-2 py-1.5">
                  {/* Server-actor rows (conductor auto-clear, etc.) carry an
                      EMPTY session_id — render them as a neutral machine
                      lozenge, never a blank/unlinked session chip. */}
                  {s.session_id && s.session_id.trim()
                    ? <EntityChip kind="session" label={`${s.session_id.slice(0, 8)} · drive`} to={`/sessions/${s.session_id}`} />
                    : <Lozenge tone="neutral">conductor · machine</Lozenge>}
                  {/* Honest zero: a session with no attributable tokens says
                      "—", never "0 tok" (there was no transcript to read). */}
                  <span className="ml-auto font-mono text-xs text-[color:var(--text-muted)] tabular-nums">
                    {s.tokens_total > 0 ? `${fmtTokens(s.tokens_total)} tok` : "—"}
                  </span>
                </div>
                {s.steps.map((st, i) => (
                  <TraceStepRow key={`${st.step}-${i}`} step={st} max={max} />
                ))}
              </div>
            );
          })}
        </div>
      </Card>
      <p className="text-2xs text-[color:var(--text-muted)] leading-relaxed">
        Drive-scoped: session → SDLC step, joined from the agent_runs telemetry spine. Every row carries its token cost; cross-task totals live on Sessions.
      </p>
    </div>
  );
}

// Gate-arrival re-fetch (task 8e5aa63b): the SAME runKey/re-observe pattern
// already proven at ranPinTestsFor (below) — a page opened BEFORE a gate
// mints must not freeze on a stale readiness payload for the rest of the
// session. readinessRunKey names the transition; shouldRefetchReadiness is
// a strict change check (never a timer). Kept as PLAIN JS (no inline type
// annotations) so this exact block is directly executable by node in tests
// (D-4) — types for the call sites live outside the markers.
// --- READINESS-REFRESH-BLOCK-START ---
// @ts-expect-error -- untyped on purpose: this block is executed verbatim
// under a bare `node` in tests/unit/test_gate_banner_refreshes_on_gate_arrival.py,
// so it must contain no TypeScript-only syntax. Callers below pass real types.
function readinessRunKey(id, task) {
  return `${id || ""}:${(task && task.workflow_step) || ""}:${(task && task.gate_state) || ""}:${(task && task.status) || ""}`;
}
// @ts-expect-error -- see readinessRunKey above; same reason.
function shouldRefetchReadiness(prevKey, key) {
  return prevKey !== key;
}
// --- READINESS-REFRESH-BLOCK-END ---

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
  // Doc-column tab (artifact .itabs): Overview = the existing content, Trace =
  // the drive-scoped token trace, Evidence = the gate decision packet (task
  // d9f082fe follow-up, owner live 2026-08-24: "this evidence stuff should
  // not be a expanding panel, but rather a tab on the work screen" —
  // SUPERSEDES the 2026-07-something decision at this file's old ~2296
  // comment that put the gate decision inline on Overview "top-level and
  // actionable in place"; that inline toggle turned out to be a real safety
  // hazard too — a stray click on the wrong nearby control (the header's
  // bare, no-confirmation "-> done" status button) silently closed a task
  // while reviewing it, live, on the owner's own screen). The rail (Details
  // + Connections) persists across all three. Trace is fetched lazily the
  // first time its tab is opened.
  const [docTab, setDocTab] = useState<"overview" | "trace" | "evidence">("overview");
  const [trace, setTrace] = useState<TaskTrace | null>(null);
  const [traceLoading, setTraceLoading] = useState(false);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [sessions, setSessions] = useState<SessionRow[]>([]);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [children, setChildren] = useState<ChildTask[]>([]);
  // OKF concepts this task recalled (recall_log attribution) — the rail's
  // "Knowledge · Understand" group. Empty when the task recalled nothing.
  const [knowledge, setKnowledge] = useState<KnowledgeConcept[]>([]);
  // The RED tests that pin this task's oracle (empty unless a committed test
  // file names the task) — rendered beneath the oracle panel.
  const [pinTests, setPinTests] = useState<PinTest[]>([]);
  const [testReceipt, setTestReceipt] = useState<TestReceipt>(null);
  // WHEN the counts on the card were observed, and from WHICH tree. Without
  // these the card states a number with no way to tell how old it is or what
  // it was measured against (owner 2026-07-21: "fake facts in prism").
  const [pinTestsAt, setPinTestsAt] = useState<string>("");
  // Explicit, on-demand test run (task f3e8d477). Page load stays cheap; this
  // is how a reviewer PRODUCES the evidence. The server persists the outcome,
  // so the badge survives a reload instead of resetting to NOT RUN.
  const [runningTests, setRunningTests] = useState(false);
  const [pinSource, setPinSource] = useState<{ source?: string; source_sha?: string }>({});
  // LIVE gate-card truth: the evidence tooth checked at render time (never a
  // stale stored decision string) — GET /api/conductor/gate/readiness.
  const [gateReadiness, setGateReadiness] = useState<GateReadiness | null>(null);
  // SINGLE choke point for GET /api/conductor/gate/readiness (task
  // 8e5aa63b, AC-4): both the transition-keyed effect below and the manual
  // "re-run oracle" button call this ONE function, so the readiness URL
  // literal exists exactly once in this file.
  const refreshReadiness = useCallback(async (): Promise<GateReadiness | null> => {
    if (!id) return null;
    try {
      const gr = await api.get<GateReadiness>(
        `/api/conductor/gate/readiness?task_id=${id}&project=${project}`);
      setGateReadiness(gr);
      return gr;
    } catch {
      setGateReadiness(null);
      return null;
    }
  }, [id, project]);
  // Last ↻ re-run outcome, rendered INLINE in the evidence table (owner
  // 2026-07-16: a silent corner toast reads as "the button does not work" —
  // the result must appear where the click happened).
  const [mintResult, setMintResult] = useState<string>("");
  // Delivery pipeline — WHERE the work lives and what's left to ship (owner
  // 2026-07-16: done means SHIPPED; the app must visually show where you are
  // and what still needs to be done). GET /api/tasks/:id/delivery.
  type DeliveryStage = { key: string; label: string; state: "done" | "next" | "pending"; detail: string };
  type Delivery = {
    branch: string;
    commits: { sha: string; subject: string; pushed: boolean; merged_to_main: boolean; released_in: string }[];
    stages: DeliveryStage[];
    delivered: boolean;
  };
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  // In-panel decision feedback: 'checking' while the machine check runs
  // (minutes), then the persistent result — never just a transient toast.
  const [gateResult, setGateResult] = useState<{ kind: "checking" | "ok" | "refused"; text: string } | null>(null);
  // ONE authoritative verdict (owner design: the layout must be unable to
  // contradict itself). verifierRefusal = the shell verifier's last recorded
  // refusal, parsed for humans; verdict is READY only when the evidence
  // receipt passes AND no verifier refusal stands.
  const verifierRefusal = useMemo(() => {
    const gr = task?.gate_reason || "";
    if (!gr.includes("verifier rejected")) return null;
    const m = /(\d+)\s*\/\s*(\d+)\s*pass(?:[,;]?\s*(\d+)\s*fail)?(?:[,;]?\s*(\d+)\s*skipped)?/.exec(gr);
    if (m) {
      return {
        summary: `${m[1]} / ${m[2]} pass${m[3] ? ` · ${m[3]} fail` : ""}${m[4] ? ` · ${m[4]} skipped` : ""}`,
        detail: `${m[3] ?? 0} assertion(s) failed, ${m[4] ?? 0} skipped`,
      };
    }
    return { summary: "refused", detail: "" };
  }, [task?.gate_reason]);
  // READY follows the LIVE evidence alone — the engine's receipt tooth now
  // decides first (v7.0.18), so a stale verifier refusal from a previous
  // decision must not lock the button (it is shown as history below).
  const gateVerdict: "ready" | "blocked" =
    gateReadiness?.receipt_ok ? "ready" : "blocked";
  // HONEST GATE HEADLINE (task 72d3e0d1, owner 2026-07-29): receipt_ok=true
  // only means "a human review is the accepted evidence route for this
  // proof_type" — it does NOT mean anything passed. isAwaitingReview names
  // the bare-entitlement receipt (no machine tooth ran) so the headline can
  // say so instead of claiming "evidence passing". A real fresh receipt or
  // an epic roll-up's aggregated child evidence is NOT this case and keeps
  // the affirmative headline.
  const isAwaitingReview =
    gateVerdict === "ready" &&
    !!gateReadiness?.manual_review &&
    gateReadiness?.receipt?.adapter === "human";
  // HONEST PLAN_GATE HEADLINE (task 0c49c385, owner 2026-08-07): at
  // plan_gate a populated-but-unapproved design packet reads
  // receipt_ok=false from the design-packet adapter (api/conductor.py:
  // 171-188) — that means "not yet approved by YOU", never "no evidence
  // exists", so it must not fall into the green_gate-shaped "evidence not
  // on file" branch. packetParts names what the owner actually has to
  // review; isAwaitingDesignApproval requires real content so a genuinely
  // empty packet still falls through to the missing-content copy.
  const packetParts = [
    task?.plan_doc ? "story/plan" : null,
    task?.plan_diagram ? "diagram" : null,
    task?.has_prototype ? "prototype" : null,
  ].filter(Boolean).join(", ");
  const isAwaitingDesignApproval =
    gateReadiness?.receipt?.adapter === "design-packet" &&
    gateReadiness?.receipt?.passed === false &&
    !!(task?.plan_doc || task?.plan_diagram);
  // HONEST STORY_GATE HEADLINE (task a646cbd1, mx-a31a3d convention): a
  // story_gate rubric refusal is a MACHINE-OWNED, healthily re-swept state
  // (the rubric re-sweep clears it, not an owner click) — not the same as a
  // genuinely stuck gate. Keyed on receipt.adapter (never workflow_step
  // string equality) so this generalizes to any future rubric-shaped gate.
  const isStoryRubricPending =
    gateReadiness?.receipt?.adapter === "story-rubric" &&
    gateVerdict !== "ready";
  // Calm-but-not-a-pass tone (owner: awaiting-review is a normal, correct
  // state — it must never read as alarming/failed, and must never be
  // visually indistinguishable from a real pass).
  const bannerTone: "sage" | "amber" | "rose" =
    isAwaitingDesignApproval ? "amber" :
    isStoryRubricPending ? "amber" :
    gateVerdict !== "ready" ? "rose" : isAwaitingReview ? "amber" : "sage";
  // Clicking the oracle's compact "N RED" summary drives PlanView to its Tests
  // tab (bump the nonce so a repeat click re-fires) and scrolls it into view.
  const [tabRequest, setTabRequest] = useState<{ tab: string; n: number } | null>(null);
  const planRef = useRef<HTMLDivElement>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped ONLY inside gateDecide's success branch, after both
  // approveDesignPacket() and the gate POST resolve (task fa7735bd) - lets
  // the Design tab's own <DesignPacket> card refetch and drop its
  // not-approved branch without a full page reload.
  const [designPacketRefreshToken, setDesignPacketRefreshToken] = useState(0);
  // Operable conductor gate: a REQUIRED reason + an override checkbox feed
  // POST /api/conductor/gate (the same path the MCP conductor_gate tool uses).
  const [gateReason, setGateReason] = useState("");
  const [gateOverride, setGateOverride] = useState(false);
  // Recovery lever for a gate decided in error (owner 2026-08-25: "the user
  // [must] be able to intuitively recover from this state" — a wrongly
  // approved gate, e.g. green_gate, had NO in-app recovery path at all; the
  // only lever was POST /api/conductor/rewind, callable via raw curl only).
  // Always offered on the Evidence tab (never gated on gate_state, unlike
  // the decision card above — the whole point is undoing a decision that
  // has ALREADY been made, so gate_state is typically 'passed' by then).
  const [rewindReason, setRewindReason] = useState("");
  const [rewindBusy, setRewindBusy] = useState(false);
  const [rewindResult, setRewindResult] = useState<{ kind: "ok" | "refused"; text: string } | null>(null);
  // Real signed-in identity (task 98d38111): the browser's actual approver,
  // never boilerplate reason text - forwarded into gateDecide's two wire
  // calls so a gate_decide history row resolves to a real HUMAN, not
  // unknown:conductor. Same pattern as PageHeader.tsx's IdentityChip.
  const [me, setMe] = useState<{ id: string; email: string } | null>(null);
  useEffect(() => {
    let cancel = false;
    api.get<{ user?: { id: string; email: string } }>("/api/auth/me")
      .then((r) => { if (!cancel) setMe(r.user ?? null); })
      .catch(() => { if (!cancel) setMe(null); });
    return () => { cancel = true; };
  }, []);
  const approverIdentity = me?.email || me?.id || "";
  // Pre-fill a truthful suggested reason on the Evidence tab (task c7ce0fc3)
  // — a render-time effect of the tab being open, never a side effect of
  // the banner's own navigate-there click. Approve stays one click in the
  // ready case regardless (its disabled-check never requires a reason
  // there); this only saves typing. Never claim a "passing receipt" for
  // the bare-entitlement case. The owner edits or replaces it at will.
  useEffect(() => {
    if (docTab !== "evidence" || gateReason.trim()) return;
    setGateReason(isAwaitingReview
      ? "Approving on my own review — no machine evidence exists at this tree; my read of the artifacts is the sign-off."
      : gateReadiness?.receipt_ok
        ? `Approving on live evidence: fresh passing oracle receipt (${gateReadiness.receipt?.adapter || "trusted run"}) + drive record.`
        : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docTab]);
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

  // Two-phase load (owner 2026-08-13: "clicking on a task still does not
  // instant load"): scope=core is sqlite+stat only and paints the page in
  // well under a second; scope=full carries the transcript-parsed panels
  // (spend, timeline, per-turn/step tokens, phase_progress/activity) whose
  // in-process caches are cold after every daemon bounce (~30s first hit).
  const load = useCallback(async (scope: "core" | "full" = "full") => {
    if (!id) return;
    try {
      const d = await api.get<{ task: Task; history: HistoryRow[]; sessions?: SessionRow[]; phase_progress?: PhaseProgress | null; activity?: Activity | null; timeline?: Timeline | null; has_prototype?: boolean; spend?: SpendData | null; step_tokens?: Record<string, number>; mirror?: TaskMirror | null; mirrors?: TaskMirror[] }>(
        `/api/tasks/${id}?project=${project}${scope === "core" ? "&scope=core" : ""}`,
      );
      // phase_progress + activity + has_prototype + spend + step_tokens +
      // mirror(s) ride at the TOP LEVEL of the response (not nested in task) —
      // merge onto the task so the SDLC bar, the honest work-state pill, the
      // prototype iframe, the Spend panel, the StepRail's per-step tokens,
      // and the linked-issue block all read them off task.*.
      // Prev-fallback on the heavy fields: a scope=core response omits them,
      // and clobbering already-loaded panels back to null would flash the
      // page empty on every core refresh.
      setTask((prev) => d.task ? { ...d.task, phase_progress: d.phase_progress ?? prev?.phase_progress ?? null, activity: d.activity ?? prev?.activity ?? null, has_prototype: d.has_prototype ?? prev?.has_prototype ?? false, spend: d.spend ?? prev?.spend ?? null, step_tokens: d.step_tokens ?? prev?.step_tokens ?? {}, mirror: d.mirror ?? d.task.mirror ?? null, mirrors: d.mirrors ?? d.task.mirrors ?? [] } : d.task);
      setHistory(d.history ?? []);
      setSessions(d.sessions ?? []);
      setTimeline((prev) => d.timeline ?? prev ?? null);
      setError(null);
      // Children aren't on the detail payload — derive them from the task
      // list, scoped server-side to THIS epic's direct children and
      // projected to a lean field set (task 842248bd: this used to fetch the
      // WHOLE unscoped board just to filter client-side by parent_id, the
      // single biggest offender behind "task detail fetches 2.47 MB").
      try {
        const all = await api.get<{ tasks: ChildTask[] }>(
          `/api/tasks?project=${project}&parent_id=${id}&fields=id,title,status,priority,parent_id`,
        );
        setChildren(all.tasks ?? []);
      } catch {
        setChildren([]);
      }
    } catch (e) {
      setError((e as Error).message ?? "task not found");
    }
  }, [id, project]);

  // Paint from core immediately, then let the transcript-heavy full payload
  // land behind it — never make the first paint wait on a cold spend parse.
  useEffect(() => { (async () => { await load("core"); await load("full"); })(); }, [load]);

  // Real-time push (task 2d480b08): subscribe to THIS task's /sse/tasks
  // stream and PATCH local state from the pushed event's `fields` (D-4) so
  // the SDLC progress bar, child checklist, and token-effort label update
  // within ~1s of a backend change — never refetch on every event, that
  // is the SessionsPage.tsx:80 `es.onmessage = () => load();`
  // refetch-everything anti-pattern this task's likely_misfire names.
  useEffect(() => {
    if (!id) return;
    return subscribeStream(`/sse/tasks?project=${project}&task_id=${id}`, (data) => {
      try {
        const payload = JSON.parse(data) as { task_id?: string; fields?: Partial<Task> };
        if (payload.task_id !== id || !payload.fields) return;
        const fields = payload.fields;
        setTask((prev) => (prev ? { ...prev, ...fields } : prev));
      } catch { /* ignore malformed payloads */ }
    });
  }, [id, project]);

  // ONE-SHOT per task, all in PARALLEL and all CHEAP: test discovery
  // (run=false — AST scan only), readiness, delivery. The old shape awaited
  // tests?run=true FIRST, so every page open ran a real pytest suite and
  // readiness/delivery queued behind it — with the browser's 6-connection
  // limit + SSE + the 5s poll, the page sat on "Loading…" for 30-60s
  // (owner 2026-07-16: "it's not even running?").
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const tr = await api.get<{ tests: PinTest[]; receipt?: TestReceipt | null }>(`/api/tasks/${id}/tests?project=${project}`);
        if (!cancelled) { setPinTests(tr.tests ?? []); setTestReceipt(tr.receipt ?? null); }
      } catch { if (!cancelled) setPinTests([]); }
    })();
    (async () => {
      try {
        const dv = await api.get<Delivery>(
          `/api/tasks/${id}/delivery?project=${project}`);
        if (!cancelled) setDelivery(dv);
      } catch { if (!cancelled) setDelivery(null); }
    })();
    return () => { cancelled = true; };
  }, [id, project]);

  // Re-fetch readiness on a gate_state/workflow_step/status TRANSITION —
  // never a fixed-interval timer (stop_if). Same runKey/re-observe shape as
  // ranPinTestsFor just below: covers BOTH the initial mount (task lands
  // undefined -> loaded is itself a transition) AND a gate arriving/
  // resolving mid-session via the SSE push above, so a page opened before a
  // gate mints does not freeze on a stale/legacy readiness payload for the
  // rest of the session (task 8e5aa63b).
  const readinessRunFor = useRef<string>("");
  useEffect(() => {
    if (!id) return;
    const key = readinessRunKey(id, task);
    if (!shouldRefetchReadiness(readinessRunFor.current || null, key)) return;
    readinessRunFor.current = key;
    refreshReadiness();
  }, [id, task?.workflow_step, task?.gate_state, task?.status, refreshReadiness]);

  // HEAVY, deferred: actually EXECUTE the pinned tests (one pytest run,
  // one-shot per task) only when the task is parked AT a gate — that is
  // when live statuses inform a decision. Done/idle tasks read their
  // receipts; casual browsing never spawns a test run.
  const ranPinTestsFor = useRef<string>("");
  useEffect(() => {
    const step = task?.workflow_step || "";
    // Key the one-shot on the task's GATE POSITION, not just its id (owner
    // 2026-07-21). Keyed on id alone the evidence was fetched once and never
    // again, so a card could sit on minutes-old counts indefinitely while the
    // footer's /api/version poll advertised a newer build — the panel
    // contradicting itself. A step/gate transition now re-observes.
    const runKey = `${id}:${step}:${task?.gate_state || ""}:${task?.status || ""}`;
    if (!id || !task || ranPinTestsFor.current === runKey) return;
    // Run live when parked AT a gate (statuses inform the decision), OR when a
    // DONE task still has discovered tests the gate's receipt did NOT cover —
    // so no badge stays a non-observation ("not run") on finished work. The
    // receipt already stamps the pinned-oracle rows instantly; this fills the
    // rest with their real pass/fail (one-shot per task).
    const atGate = step.endsWith("_gate") && task.status !== "done" &&
      task.status !== "cancelled" && task.status !== "archived" && task.status !== "deleted";
    const hasUnverified = pinTests.some((t) => !(t.status && t.status.toLowerCase() !== "not-run"));
    const doneWithGaps = task.status === "done" && pinTests.length > 0 && hasUnverified;
    if (!atGate && !doneWithGaps) return;
    ranPinTestsFor.current = runKey;
    let cancelled = false;
    (async () => {
      try {
        const tr = await api.get<{ tests: PinTest[]; receipt?: TestReceipt | null; source?: string; source_sha?: string }>(`/api/tasks/${id}/tests?run=true&project=${project}`);
        if (!cancelled) {
          setPinTests(tr.tests ?? []); setTestReceipt(tr.receipt ?? null);
          setPinSource({ source: tr.source, source_sha: tr.source_sha });
          setPinTestsAt(new Date().toLocaleTimeString());
        }
      } catch { /* keep the discovery rows — statuses stay honestly not-run */ }
    })();
    return () => { cancelled = true; };
  }, [id, task?.workflow_step, task?.status, pinTests]);

  // Run the pinned tests ON DEMAND and refresh the rows. The server records
  // the outcome (test_run_store), so the badges keep showing this run after a
  // reload — the fix for a TESTS tab that could only ever say NOT RUN.
  const runTests = useCallback(async () => {
    setRunningTests(true);
    try {
      const tr = await api.get<{ tests: PinTest[]; receipt?: TestReceipt | null; source?: string; source_sha?: string }>(
        `/api/tasks/${id}/tests?run=true&project=${project}`);
      setPinTests(tr.tests ?? []);
      setTestReceipt(tr.receipt ?? null);
      setPinSource({ source: tr.source, source_sha: tr.source_sha });
      setPinTestsAt(new Date().toLocaleTimeString());
    } catch {
      /* leave the discovery rows — statuses stay honestly not-run */
    } finally {
      setRunningTests(false);
    }
  }, [id, project]);

  // Navigating task → task (the route reuses this component) resets the tab
  // back to Overview and drops the previous task's trace so it re-fetches.
  useEffect(() => { setDocTab("overview"); setTrace(null); setMintResult(""); }, [id]);

  // Lazy trace fetch: only when the Trace tab is first opened for this task.
  // `trace` in the deps gates it to a single fetch (a failure caches an empty
  // trace so the tab shows the honest empty state instead of a spinner loop).
  useEffect(() => {
    if (docTab !== "trace" || trace !== null || !id) return;
    let cancelled = false;
    setTraceLoading(true);
    (async () => {
      try {
        const d = await api.get<TaskTrace>(`/api/tasks/${id}/trace?project=${project}`);
        if (!cancelled) setTrace(d);
      } catch {
        if (!cancelled) setTrace({ sessions: [], totals: { tokens: 0, steps: 0, sessions: 0 } });
      } finally {
        if (!cancelled) setTraceLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [docTab, trace, id, project]);

  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => setNotice(null), 4000);
    return () => clearTimeout(t);
  }, [notice]);

  // Only transcript-backed (UUID) sessions are real work sessions. Synthetic
  // gate-actor labels (qa-red-gate-*, *-verifier-*) surface as gate markers
  // elsewhere, never as bare session rows.
  const realSessions = useMemo(
    () => sessions.filter((s) =>
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(s.session_id)),
    [sessions],
  );

  // "Code touched" rail group: union of files_modified_paths across the linked
  // sessions' detail endpoint. Fetched once per session-id set, 404-tolerant,
  // repo-relative paths only (absolute C:\ / POSIX roots are dropped).
  const [codePaths, setCodePaths] = useState<string[]>([]);
  const [codeExpanded, setCodeExpanded] = useState(false);
  const sessionIdsKey = realSessions.map((s) => s.session_id).join(",");
  useEffect(() => {
    const ids = sessionIdsKey ? sessionIdsKey.split(",") : [];
    if (ids.length === 0) { setCodePaths([]); return; }
    let cancelled = false;
    (async () => {
      const acc = new Set<string>();
      for (const sid of ids) {
        try {
          const d = await api.get<{ files_modified_paths?: string[] }>(`/api/sessions/${sid}?project=${project}`);
          for (const p of d.files_modified_paths ?? []) {
            if (!p || /^[a-zA-Z]:[\\/]/.test(p) || p.startsWith("/")) continue;
            acc.add(p);
          }
        } catch { /* tolerate 404 / missing detail */ }
      }
      if (!cancelled) setCodePaths([...acc].sort());
    })();
    return () => { cancelled = true; };
  }, [sessionIdsKey, project]);

  // "Knowledge · Understand" rail group: the OKF concepts this task recalled,
  // read from the memory recall_log (guarded — a task with no recalls, or a
  // store without a recall db, yields []).
  useEffect(() => {
    if (!id) { setKnowledge([]); return; }
    let cancelled = false;
    (async () => {
      try {
        const d = await api.get<{ concepts: KnowledgeConcept[] }>(
          `/api/okf/task_concepts?task_id=${encodeURIComponent(id)}&project=${project}`,
        );
        if (!cancelled) setKnowledge(d.concepts ?? []);
      } catch {
        if (!cancelled) setKnowledge([]);
      }
    })();
    return () => { cancelled = true; };
  }, [id, project]);

  const setStatus = async (status: string) => {
    setBusy(true);
    try {
      const r = await fetch(`/api/tasks/${id}?project=${project}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status }),
      });
      // fetch() only rejects on a network error, never on a non-2xx status
      // -- unchecked, a refused PATCH (e.g. the open-gate close guard)
      // still fell through to "Moved to done.", a false-success toast for
      // a status change that never actually happened (2026-08-25 live
      // near-miss). Read the real body and surface a refusal honestly.
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        setNotice(`Not moved: ${body.detail || body.error || `HTTP ${r.status}`}`);
        return;
      }
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
  // Re-run the oracle INSIDE the daemon and mint a fresh EvidenceReceipt
  // (POST /api/conductor/gate/mint) — the gate panel's evidence action.
  const mintEvidence = async () => {
    setNotice("re-running the oracle inside the daemon…");
    setMintResult("running the oracle inside the daemon…");
    let msg = "";
    try {
      const r = await api.post<{ ok: boolean; receipt_ok: boolean; receipt_refusal?: string }>(
        `/api/conductor/gate/mint?project=${project}`, { task_id: id });
      // Do NOT claim "Approve will pass" from the MINT's own result: on a red
      // gate the mint writes a PASSING oracle receipt, which can never satisfy
      // the red tooth (that wants the pinned tests observed FAILING at the red
      // anchor). Saying it passed sent the owner to a button that could not
      // help (owner 2026-07-21). Re-pull READINESS and let it be the verdict.
      msg = r.receipt_ok
        ? "oracle re-ran and a fresh receipt was minted — checking the gate…"
        : `oracle ran but evidence still not ready: ${r.receipt_refusal || "no receipt minted"}`;
      setNotice(msg);
      setMintResult(msg);
    } catch (e) {
      msg = `oracle re-run failed: ${(e as Error).message}`;
      setNotice(msg);
      setMintResult(msg);
    }
    // The readiness fetch is one-shot per task (heavy git-walk, kept out of
    // the 5s poll) — so a mint MUST re-pull it or the result chip stays
    // "missing" forever and the button reads as broken.
    const gr = await refreshReadiness();
    if (gr) {
      // Readiness is the authority on whether Approve will actually pass.
      msg = gr.receipt_ok
        ? "fresh evidence receipt minted — Approve will pass the evidence check"
        : `re-run did NOT satisfy this gate: ${gr.receipt_refusal || "evidence still missing"}`;
      setNotice(msg);
      setMintResult(msg);
    } /* else: keep the last known readiness */
    load();
  };

  const gateDecide = async (action: "approve" | "reject") => {
    // A plain Approve is ONE CLICK (owner 2026-07-16: demanding a why here is
    // friction) — the audit trail records a truthful default note. A reject or
    // an override release still demands the owner's why: those are the levers
    // that need justification.
    const needsReason = action === "reject" || gateOverride;
    if (needsReason && !gateReason.trim()) {
      setGateResult({
        kind: "refused",
        text: action === "reject"
          ? "A reason is required to reject the gate."
          : "A reason is required for an override release.",
      });
      return;
    }
    const decisionReason = gateReason.trim() || "approved by owner (one-click from the task page)";
    setBusy(true);
    // FR-4 (task 377b00a8): a wedged POST used to leave CHECKING indistinguishable
    // from a dead page forever. An AbortController + a generous timeout (longer
    // than a healthy mint) guarantees control returns to the owner, and an
    // elapsed-seconds ticker (real Date.now() reads, not a static label) proves
    // the page is still alive while it waits.
    const GATE_DECIDE_TIMEOUT_MS = 180_000;
    const startedAt = Date.now();
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), GATE_DECIDE_TIMEOUT_MS);
    const checkingText = () => {
      const elapsed = Math.round((Date.now() - startedAt) / 1000);
      return (gateOverride
        ? "recording your audited manual release…"
        : "running the machine check — this takes up to a few minutes; stay on this page…")
        + ` (${elapsed}s elapsed)`;
    };
    // A recursive setTimeout, never a fixed-interval timer — this file is
    // pinned (test_no_fixed_interval_readiness_poll_and_one_choke_point)
    // to carry no such timer at all, so a local UI tick still has to
    // reschedule itself one setTimeout at a time.
    let elapsedTimer: ReturnType<typeof setTimeout> | undefined;
    const tick = () => {
      setGateResult({ kind: "checking", text: checkingText() });
      elapsedTimer = setTimeout(tick, 1000);
    };
    tick();
    try {
      // FR-6 (task 791602a9): a plain plan_gate approve must also record
      // the design-packet ledger's own approval, or the packet stays
      // unapproved forever after the gate releases. Runs BEFORE the gate
      // POST, inside the SAME try{} - a failed design-packet approve
      // throws and the gate POST below never fires. Gated off by
      // !gateOverride (task 73f13267): an override release is an audited
      // manual bypass, never an explicit owner_explicit sign-off on the
      // packet - recording an approval receipt from an override click would
      // forge that sign-off. Approver is the resolved identity (task 98d38111).
      if (isAwaitingDesignApproval && action === "approve" && !gateOverride) {
        await approveDesignPacket(id ?? "", approverIdentity, project);
      }
      const r = await fetch(`/api/conductor/gate?project=${project}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: id,
          action,
          reason: decisionReason,
          override: gateOverride,
          actor: approverIdentity,
        }),
        signal: controller.signal,
      });
      clearTimeout(timeoutId);
      clearTimeout(elapsedTimer);
      const body = await r.json().catch(() => ({}));
      if (body.ok === false) {
        setGateResult({
          kind: "refused",
          text: `${action} refused: ${body.reason ?? "unknown"}`,
        });
      } else {
        // Only the terminal green_gate actually releases the task
        // (models/workflow.py WORKFLOW_STEPS - green_gate is the one
        // step nothing follows). A plan_gate/story_gate/red_gate advance
        // must name the next step, never claim release (clause E).
        setGateResult({
          kind: "ok",
          text: `Gate ${action}d${body.to_step ? ` → ${body.to_step}` : ""}. ${action === "approve" && body.gate_step === "green_gate" ? "This task is released." : ""}`,
        });
        setGateReason("");
        setGateOverride(false);
        // Refetch the Design tab's own card too, so a successful design
        // approve confirms in place without a full page reload (never
        // bumped before this point - a refused/failed approve must not
        // show a false approved state).
        setDesignPacketRefreshToken((n) => n + 1);
      }
      load();
    } catch (e) {
      clearTimeout(timeoutId);
      clearTimeout(elapsedTimer);
      if ((e as Error).name === "AbortError") {
        // Client-side timeout, distinct from a real server refusal: the
        // server check may still be finishing, so give the owner a next
        // action instead of an opaque failure (mx-d6c1df — CHECKING must
        // never look identical to dead).
        const elapsed = Math.round((Date.now() - startedAt) / 1000);
        setGateResult({
          kind: "refused",
          text: `Gate ${action} timed out after ${elapsed}s — the server check may still be running. Reload to check readiness, or try again.`,
        });
      } else {
        setGateResult({ kind: "refused", text: `Gate ${action} failed: ${(e as Error).message ?? e}` });
      }
    } finally {
      setBusy(false);
    }
  };

  // Step this task back exactly ONE workflow step and reopen that step's
  // gate for a fresh decision — the audited recovery lever
  // (ConductorService.rewind_task) for a gate that was decided in error.
  // The backend itself refuses a blank reason and refuses to rewind off the
  // first step, so this stays simple: send it, show whatever it says.
  const doRewind = async () => {
    if (!rewindReason.trim()) {
      setRewindResult({ kind: "refused", text: "A reason is required to rewind (audited lever)." });
      return;
    }
    setRewindBusy(true);
    setRewindResult(null);
    try {
      const r = await fetch(`/api/conductor/rewind?project=${project}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: id,
          reason: rewindReason.trim(),
          actor: approverIdentity || "owner",
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (r.ok && body.ok) {
        setRewindResult({
          kind: "ok",
          text: `Rewound ${body.from_step ?? "?"} → ${body.to_step ?? "?"}. That step's gate is open for a fresh decision.`,
        });
        setRewindReason("");
        load();
      } else {
        setRewindResult({ kind: "refused", text: body.reason || `Rewind failed (${r.status}).` });
      }
    } catch (e) {
      setRewindResult({ kind: "refused", text: `Rewind failed: ${(e as Error).message ?? e}` });
    } finally {
      setRewindBusy(false);
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
  const conductorOn = (task.workflow_step ?? "") !== "" || (task.gate_state ?? "none") !== "none";
  const shortId = String(task.id ?? id).slice(0, 8);
  const gateActive = (task.gate_state ?? "none") !== "none";
  const gateStep = /_gate$/.test(task.workflow_step ?? "");
  // ─ Gate-panel receipt reconciliation (task 5a6837a0) ─────────────────────
  // The panel renders TWO independent receipt lookups: the DecisionPacket row
  // is oracle_spec.latest_receipt() — newest receipt on the task, ANY gate, ANY
  // tree (services/decision_packet.py:86-98) — while the check row is
  // GET /api/conductor/gate/readiness, pinned to THIS gate's spec + anchor
  // commit (api/conductor.py:102-163). Both can be true at once and read as one
  // contradictory fact. Remedy (mx-352a3d, v7.1.24): NAME the tree each was
  // measured at, and say in one line why they differ — never delete a row.
  const gateAnchorSha = (/\b[0-9a-f]{12,40}\b/.exec(gateReadiness?.receipt_refusal || "") || [""])[0];
  const receiptsDiverge = !!testReceipt && !gateReadiness?.receipt;
  // The gate panel OWNS the oracle while a gate is up (stop_if #2: it must not
  // render twice on one page); PlanView keeps it for every other state.
  const gatePanelOwnsOracle = conductorOn && (task.gate_state === "pending" || task.gate_state === "failed") &&
    task.status !== "cancelled" && task.status !== "archived" && task.status !== "deleted";
  // Repo-relative code paths capped at 8 rows with a "N more" expander.
  const CODE_CAP = 8;
  const codeShown = codeExpanded ? codePaths : codePaths.slice(0, CODE_CAP);
  const codeHidden = codePaths.length - codeShown.length;
  const connectionCount =
    realSessions.length + codePaths.length + knowledge.length + (gateActive || gateStep ? 1 : 0) + children.length;
  const hasConnections = connectionCount > 0;

  return (
    <Page>
      {/* Breadcrumb — "Tasks / <short-id>"; the crumb root carries the
          context-aware back navigation the old ← button did. When the task
          HAS a real parent_id, the crumb always goes there and says
          "Parent" -- previously this only happened when `from` (browser
          navigation state) pointed at a task, so a child opened via direct
          URL/bookmark/refresh showed "Tasks" here with no visible way up,
          and the ONLY real parent link lived in a Card far down the page,
          past the whole SDLC trace (owner: "i see no way to see the parent
          to navigate to the parent"). */}
      <div className="flex items-center justify-between gap-4">
        <div className="flex items-center gap-1.5 text-xs text-[color:var(--text-muted)]">
          <button
            onClick={() =>
              task?.parent_id
                ? navigate(`/tasks/${task.parent_id}`, { state: { from: "/tasks" } })
                : navigate(from)
            }
            className="hover:text-[color:var(--text-secondary)]"
            title={task?.parent_id ? "back to parent" : backLabel}
          >
            {task?.parent_id
              ? "Parent"
              : from.startsWith("/tasks/") ? "Parent" : from === "/conductor" ? "Conductor" : "Tasks"}
          </button>
          <span className="opacity-50">/</span>
          <span className="font-mono text-[color:var(--text-secondary)]">{shortId}</span>
        </div>
        {conductorOn && (
          <Link
            to={`/workflows?task=${task.id}`}
            aria-label="Open this task's conductor flow"
            className="text-xs uppercase tracking-wider text-[color:var(--text-secondary)] hover:text-[color:var(--text-primary)]"
          >
            ↗ Flow
          </Link>
        )}
      </div>

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
              className="text-3xl font-[650] tracking-[-0.01em] bg-transparent border-b border-current outline-none w-full"
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
              className="text-3xl font-[650] tracking-[-0.01em] cursor-text hover:opacity-70"
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
              className="inline-block"
            >
              <Lozenge tone={statusLoz(taskStatus)}>{taskStatus.replace(/_/g, " ")}</Lozenge>
            </motion.span>
            {/* DONE alone is a lie until the work is shipped (owner
                2026-07-16: "done is done, eg shipped and validated on
                main") — qualify it RIGHT HERE, where the claim is made,
                not below the fold. Click scrolls to the Delivery card. */}
            {taskStatus === "done" && delivery && !delivery.delivered && (
              <button
                type="button"
                onClick={() => document.getElementById("delivery-card")?.scrollIntoView({ behavior: "smooth", block: "center" })}
                className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-2xs uppercase tracking-wider font-semibold"
                style={{ color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)", background: "var(--accent-amber-bg)" }}
                title="verified, but the work has not shipped — click to see the delivery pipeline"
              >
                ● not delivered yet · {delivery.stages.find((s) => s.state === "next")?.detail || "unshipped"}
              </button>
            )}
            {taskStatus === "done" && delivery?.delivered && (
              <span
                className="inline-flex items-center rounded-full px-2.5 py-0.5 text-2xs uppercase tracking-wider font-semibold"
                style={{ color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)", background: "var(--accent-sage-bg)" }}
              >
                ✓ delivered{delivery.commits[0]?.released_in ? ` · ${delivery.commits[0].released_in}` : ""}
              </span>
            )}
            {typeof task.priority !== "undefined" && (
              <Lozenge tone={priorityLoz(task.priority)}>priority {task.priority}</Lozenge>
            )}
            {task.assigned_agent && <Lozenge tone="new">{task.assigned_agent}</Lozenge>}
            {(task.tags ?? []).map((tag) => (
              <Lozenge key={tag} tone="neutral">#{tag}</Lozenge>
            ))}
          </div>
          {(task.mirrors ?? []).length > 0 && (
            // Every ACTIVE counterpart, not just the first — a task linked
            // to both github and jira shows both (task 6fbbec35, supersedes
            // the singular task.mirror; that field survives as a read-only
            // mirrors[0] alias on the wire).
            <div data-external-context className="mt-1.5 text-2xs flex items-center gap-3 flex-wrap">
              {task.restricted ? (
                <span data-restricted className="italic" style={{ color: "var(--text-disabled)" }}>
                  Restricted — you don't have access to this item's linked provider context.
                </span>
              ) : (
                (task.mirrors ?? []).map((mirror) => (
                  <span key={mirror.provider + mirror.url} className="flex items-center gap-1.5">
                    <a href={mirror.url} target="_blank" rel="noreferrer" style={{ color: "var(--accent-teal-fg)" }}>
                      {mirror.issue || "linked issue"} ↗
                    </a>
                    <span style={{ color: "var(--text-muted)" }}>
                      synced {relativeTime(mirror.last_synced_at)} ago
                    </span>
                  </span>
                ))
              )}
            </div>
          )}
        </div>
        <div className="flex flex-wrap gap-2 shrink-0">
          {transitions.map((target) => (
            <button
              id={`status-transition-${target}`}
              key={target}
              disabled={busy}
              onClick={() => setStatus(target)}
              className="text-xs font-medium px-3 py-1.5 rounded-md border border-[color:var(--border-default)] bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] hover:border-[color:var(--border-strong)] hover:text-[color:var(--text-primary)] disabled:opacity-40"
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

      {/* Two-column issue layout (artifact .issue: 1fr / 300px, stacks
          below 900px). LEFT = the document column (plan/oracle/contract/…
          intact); RIGHT = the Details + Connections rail. */}
      <div className="grid grid-cols-1 gap-6 items-start">
      <div className="space-y-6 min-w-0">

      {/* Doc-column tab strip (artifact .itabs): Overview / Trace / Evidence.
          The Trace label carries the drive-total once fetched; Evidence only
          appears while there's a live gate decision to review (same
          condition as gatePanelOwnsOracle) — nothing to show otherwise.
          Evidence carries the ONLY gate-review signal on this page now
          (owner live, 2026-08-24: "it looks like you left the evidence
          stuff on the overview tab" — the notification banner that used to
          also live on Overview is gone; this dot is what replaces it). */}
      <div className="flex gap-0.5 border-b border-[color:var(--border-default)]" role="tablist">
        {([
          ["overview", "Overview"],
          ["trace", `Trace${trace ? ` · ${fmtTokens(trace.totals.tokens)} tok` : ""}`],
          ...(gatePanelOwnsOracle ? [["evidence", "Evidence"]] : []),
        ] as [typeof docTab, string][]).map(([val, label]) => (
          <button
            key={val}
            role="tab"
            aria-selected={docTab === val}
            onClick={() => setDocTab(val)}
            className={`-mb-px border-b-2 px-3.5 py-2 text-[13px] font-medium inline-flex items-center gap-1.5 ${
              docTab === val
                ? "border-[color:var(--accent-teal-fg)] text-[color:var(--accent-teal-fg)]"
                : "border-transparent text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]"
            }`}
          >
            {val === "evidence" && (
              <span
                className="w-1.5 h-1.5 rounded-full shrink-0"
                style={{ background: `var(--accent-${bannerTone}-fg)` }}
                aria-label="needs your review"
              />
            )}
            {label}
          </button>
        ))}
      </div>

      {docTab === "trace" && <TraceView trace={trace} loading={traceLoading} spend={task.spend} />}

      {docTab === "evidence" && conductorOn && (
        <div id="gate-recovery" className="rounded-md border overflow-hidden mb-6" style={{ borderColor: "var(--border-default)" }}>
          <Disclosure
            className="text-[12.5px]"
            summaryClassName="w-full px-4 py-3 text-left text-2xs uppercase tracking-wider"
            summaryStyle={{ color: "var(--text-muted)", background: "var(--surface-2)" }}
            summary="troubleshooting — recover from a gate decided in error (unrelated to the current decision below)"
          >
            <div className="p-4 space-y-2.5" style={{ borderTop: "1px solid var(--border-default)" }}>
              <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--text-secondary)" }}>
                Moves this task back exactly one workflow step (currently{" "}
                <code className="font-mono text-2xs px-1 rounded" style={{ background: "var(--surface-1)" }}>{task.workflow_step || "—"}</code>
                ) and reopens that step's gate — pending, ready for a fresh decision. Use this if a gate was
                approved when it shouldn't have been (e.g. a human-only gate approved by someone other than the
                project owner), or a drive advanced further than it should have. Audited: every rewind is
                recorded on the task's history with your reason.
              </div>
              <textarea
                value={rewindReason}
                onChange={(e) => setRewindReason(e.target.value)}
                placeholder="Required — why you're stepping this back (recorded on the audit trail)"
                rows={2}
                className="w-full text-[13px] rounded-md bg-[color:var(--background-base)]/40 border border-[color:var(--midground-base)]/20 p-2 leading-relaxed resize-y"
              />
              {rewindResult && (
                <div
                  className="rounded-md p-2.5 text-[12.5px] leading-relaxed"
                  style={rewindResult.kind === "ok"
                    ? { background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)", boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)" }
                    : { background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}
                  role="status"
                >
                  {rewindResult.text}
                </div>
              )}
              <button
                type="button"
                disabled={rewindBusy || !rewindReason.trim()}
                onClick={doRewind}
                className="text-2xs uppercase tracking-wider px-3.5 py-1.5 rounded disabled:opacity-40"
                style={{ background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }}
              >
                {rewindBusy ? "rewinding…" : "Rewind one step"}
              </button>
            </div>
          </Disclosure>
        </div>
      )}

      {docTab === "evidence" && gatePanelOwnsOracle && (<>

      {/* SECTION LABEL (owner live, 2026-08-25: "i dont understand how
          recovery and ready are sharing a space in the evidence panel") —
          the Recovery disclosure right above and this banner are two
          unrelated, independently-rendered blocks (different gating
          conditions: conductorOn vs. gatePanelOwnsOracle) that read as one
          compound "collapsed header + expanded body" element with nothing
          marking a new section start. This label is that mark. */}
      <div className="text-2xs uppercase tracking-wider mb-1.5" style={{ color: "var(--text-muted)" }}>
        current gate decision
      </div>

      {/* GATE STATUS HEADER — the severity summary that used to be a
          clickable notification banner on Overview (owner 2026-07-14).
          RELOCATED off Overview entirely (owner live, 2026-08-24: "it
          looks like you left the evidence stuff on the overview tab") —
          it's a plain status line here, not a button: there's nothing to
          navigate to, you're already on the one place this content lives. */}
      <div
        className="rounded-md border px-4 py-3 text-[13px] leading-relaxed flex items-center gap-3 flex-wrap"
        style={{
          borderColor: `var(--accent-${bannerTone}-ring)`,
          background: `var(--accent-${bannerTone}-bg)`,
          color: `var(--accent-${bannerTone}-fg)`,
        }}
      >
        <span className="font-semibold">
          ● {(() => {
              // ONE shared severity vocabulary (task 8e5aa63b, lib/gateSeverity)
              // so this banner, StepRail's gate row and the board tile cannot
              // legitimately disagree for the same gate at the same moment.
              const sev = gateSeverity({
                gate_state: task.gate_state,
                readiness: gateReadiness,
                verifier_refused: !!verifierRefusal,
                manual_review: !!gateReadiness?.manual_review,
              });
              return isAwaitingDesignApproval
                ? `AWAITING YOUR APPROVAL · packet ready (${packetParts})`
                // story-rubric (task a646cbd1, mx-a31a3d): quote the live reason.
                : isStoryRubricPending
                ? `PENDING · story rubric: ${gateReadiness?.receipt?.reason || "acceptance criteria not yet complete"}`
                : gateVerdict === "ready"
                ? (isAwaitingReview
                    ? "AWAITING YOUR REVIEW · no machine evidence at this tree"
                    : "READY · evidence passing")
                : (gateReadiness?.receipt?.adapter === "epic-rollup" && (gateReadiness?.blocking_children?.length ?? 0) > 0)
                ? `BLOCKED · waiting on ${gateReadiness!.blocking_children!.length} child task(s)`
                : verifierRefusal ? "BLOCKED · verifier rejected current evidence"
                // The legacy generic literal (mx-a31a3d precedent) survives,
                // reachable ONLY behind the shared function's blocked verdict —
                // never a private re-derived ternary.
                : sev.key === "blocked" ? "BLOCKED · evidence not on file"
                : sev.label;
            })()}
        </span>
        <span className="ml-auto text-[12.5px] opacity-80">
          {stepLabel(task.workflow_step ?? "gate")}
        </span>
      </div>

            <div className="bg-[color:var(--surface-1)] divide-y divide-[color:var(--border-subtle)] border rounded-md" style={{ borderColor: "var(--border-subtle)" }}>
              {/* BLOCKED-ON CHILDREN (task a646cbd1): name + link the
                  specific unfinished child(ren), not just roll-up prose —
                  same EntityChip -> /tasks/${id} pattern as the Children
                  rail group below. */}
              {(gateReadiness?.blocking_children?.length ?? 0) > 0 && (
                <div className="p-4 space-y-1.5">
                  <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>blocked on</div>
                  <div className="flex flex-wrap gap-1.5">
                    {gateReadiness!.blocking_children!.map((c) => (
                      <EntityChip key={c.id} kind="task" label={oneLine(c.title || c.id.slice(0, 8), 26)} title={c.title} to={`/tasks/${c.id}`} />
                    ))}
                  </div>
                </div>
              )}
              {/* 1 · WHAT YOU'RE APPROVING — the contract, human-first:
                  bold title, then the oracle's LEAD clause (URL linkified),
                  the rest collapsed under 'Acceptance criteria'. */}
              <div className="p-4 space-y-1.5">
                <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>what you're approving</div>
                <div className="text-[15px] font-semibold text-[color:var(--text-primary)]">{task.title}</div>
                {task.oracle && (() => {
                  const dot = task.oracle!.indexOf(". ");
                  const lead = dot > 0 ? task.oracle!.slice(0, dot + 1) : task.oracle!;
                  const rest = dot > 0 ? task.oracle!.slice(dot + 1).trim() : "";
                  const m = /(https?:\/\/\S+)/.exec(lead);
                  const leadNode = m
                    ? (<>{lead.slice(0, m.index)}<a href={m[1]} target="_blank" rel="noreferrer" className="font-mono underline decoration-dotted underline-offset-2" style={{ color: "var(--accent)" }}>{m[1].replace(/^https?:\/\//, "")}</a>{lead.slice(m.index + m[1].length)}</>)
                    : lead;
                  return (
                    <>
                      <div className="text-[13px] leading-relaxed text-[color:var(--text-secondary)]">{leadNode}</div>
                      {(rest || pinTests.length > 0) && (
                        <Disclosure
                          className="text-[12.5px]"
                          summaryStyle={{ color: "var(--text-muted)" }}
                          summary={<>Acceptance criteria{pinTests.length > 0 ? ` (${pinTests.length} assertions)` : ""}</>}
                        >
                          <div className="mt-1.5 space-y-1.5 text-[color:var(--text-secondary)] leading-relaxed">
                            {rest && <div>{rest}</div>}
                            {pinTests.length > 0 && (
                              <button type="button" onClick={showTests} className="underline decoration-dotted underline-offset-2" style={{ color: "var(--accent)" }}>
                                each assertion is a pinned test → view them with their latest outcomes
                              </button>
                            )}
                          </div>
                        </Disclosure>
                      )}
                    </>
                  );
                })()}
                {/* The risk that makes a PASS wrong belongs with the contract
                    it qualifies — not only in the Tests tab the judge would
                    have to go hunting for. */}
                {task.likely_misfire && (
                  <Disclosure
                    className="text-[12.5px]"
                    summaryClassName="text-2xs uppercase tracking-wider"
                    summaryStyle={{ color: "var(--accent-amber-fg)" }}
                    summary="how a pass could still be wrong"
                  >
                    <div className="mt-1.5 leading-relaxed text-[color:var(--text-secondary)]">{task.likely_misfire}</div>
                  </Disclosure>
                )}
              </div>

              {/* 2 · EVIDENCE — a table, two proof systems separated */}
              <div className="p-4">
                <div className="text-2xs uppercase tracking-wider mb-2" style={{ color: "var(--text-muted)" }}>evidence</div>
                {/* VISUAL EVIDENCE FIRST — for a UI/demo gate the screenshot/video IS
                    the evidence; surface it at the TOP of the gate, not buried under
                    the machine-receipt table (owner: "where is the visual validation?"). */}
                {/!\[[^\]]*\]\(/.test(task.completion_proof || "") && (
                  <div className="mb-4">
                    <div className="text-2xs uppercase tracking-wider mb-2" style={{ color: "var(--accent-sage-fg)" }}>visual evidence</div>
                    <ProofShots text={task.completion_proof} className="" />
                  </div>
                )}
                {/* THE DESIGN UNDER APPROVAL IS THE EVIDENCE (task b7b71225,
                    owner 2026-08-14: "fix the UI so that the design is in the
                    evidence package... its confusing where to do [the
                    review]"). At a design-approval gate the implementation
                    DecisionPacket is four rows of "none" - nothing has been
                    built yet - while the thing actually being approved sat in
                    PlanView's design tab below the fold. So here the packet
                    the banner points at IS the design packet; its approval
                    footer is hidden because this panel's own decision controls
                    are the single affordance (task 76df7520). */}
                {isAwaitingDesignApproval && (
                  <div className="mb-4">
                    <DesignPacket
                      taskId={id ?? ""}
                      project={project}
                      prototypeSrc={task.has_prototype
                        ? `/api/tasks/${id}/prototype?project=${encodeURIComponent(project)}`
                        : undefined}
                      hideApproval
                    />
                    <div className="mt-2 text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>
                      implementation evidence (diff, commits, oracle receipts, screenshots) does not exist yet
                      at {stepLabel(task.workflow_step ?? "plan gate")} — it is assembled here at the later gates.
                    </div>
                  </div>
                )}
                {/* Server-assembled DECISION PACKET (task a1e4120f): diff vs
                    baseline + commits + oracle receipt + screenshots, all read
                    from real worktree artifacts. Leads the machine-receipt
                    table so the reviewer sees the concrete change first. At a
                    design-approval gate it yields to the DesignPacket above -
                    the all-"none" implementation rows read as "no evidence"
                    while the real evidence is the design (task b7b71225). */}
                {!isAwaitingDesignApproval && (
                <div className="mb-4">
                  <DecisionPacket taskId={id} project={project} state={task.gate_state} step={task.workflow_step}
                                  latestReceipt={testReceipt} />
                  {/* ONE actionable line when the packet's receipt and the
                      gate's tooth are not the same receipt — a silent
                      contradiction is the defect this slice closes. */}
                  {receiptsDiverge && (
                    <div className="mt-2 rounded-md px-2.5 py-1.5 text-2xs leading-relaxed"
                         style={{ background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)" }}>
                      These are two different receipts, not a contradiction: the packet above shows the
                      task's LATEST receipt (job {testReceipt!.job_id.slice(0, 8)} measured at {testReceipt!.tree_sha.slice(0, 7)}, whatever
                      gate it served), while {stepLabel(task.workflow_step ?? "gate")} has no receipt of its own
                      {gateAnchorSha ? ` at anchor ${gateAnchorSha.slice(0, 7)}` : ""}. Act on the check row
                      below — the packet's pass does not decide this gate.
                    </div>
                  )}
                </div>
                )}
                <table className="w-full text-[12.5px]">
                  <thead>
                    <tr className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
                      <th className="text-left font-medium pb-1.5">check</th>
                      <th className="text-left font-medium pb-1.5">result</th>
                      <th className="text-right font-medium pb-1.5">freshness</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr>
                      <td className="py-1.5 pr-3">
                        <span className="font-mono">oracle receipt · {gateReadiness?.receipt?.adapter || "trusted run"}</span>
                        <div className="text-2xs" style={{ color: "var(--text-muted)" }}>the gate's evidence tooth</div>
                        {/* Name the receipt: which gate it serves and the tree it
                            was measured at, so it can't be read as the packet's
                            latest-receipt row (mx-352a3d: name the tree). */}
                        <div className="text-2xs font-mono" style={{ color: "var(--text-muted)" }}>
                          serves {stepLabel(task.workflow_step ?? "gate")} · {gateAnchorSha ? `anchor ${gateAnchorSha.slice(0, 7)}` : "anchor not reported"}
                        </div>
                      </td>
                      <td className="py-1.5 pr-3">
                        {gateReadiness?.receipt_ok
                          ? <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)" }}>passing</span>
                          : gateReadiness?.unshipped
                            // A genuinely PASSING receipt that is merely unshipped is not
                            // stale, not failed, and not missing — re-running the oracle
                            // mints another passing receipt and cannot fix a shipped-ness
                            // problem (task 8a06e121), so no re-run action is offered here.
                            ? <span className="inline-flex items-center gap-2">
                                <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }} title={gateReadiness?.receipt?.reason || ""}>passing · not shipped</span>
                                <span className="text-2xs" style={{ color: "var(--text-muted)" }}>{gateReadiness?.ship_on_approve ? "approving will ship it" : "merge the branch to origin/main"}</span>
                              </span>
                          : isAwaitingDesignApproval
                            // A design-packet receipt awaiting your review is not a failure and
                            // has nothing to re-run — the Approve click below IS the runner
                            // (task 5120c7b2, the CHECK row was task 791602a9's one miss).
                            ? <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }}>awaiting your approval, use Approve below</span>
                          : gateReadiness?.receipt?.status === "manual_evidence_required"
                            // NOT a failure — the oracle (e.g. a browser/screenshot check) has no
                            // machine runner wired, so it AWAITS your manual review. Painting this
                            // "stale / failed" reads as a broken build; it isn't. Re-run is hidden
                            // because there's nothing to re-run — approve with Override below.
                            ? <span className="inline-flex items-center gap-2">
                                <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }} title={gateReadiness?.receipt?.reason || ""}>manual · your review</span>
                                <span className="text-2xs" style={{ color: "var(--text-muted)" }}>no auto-runner for this oracle — approve with Override below</span>
                              </span>
                            : <span className="inline-flex items-center gap-2">
                                <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}>{gateReadiness?.receipt ? "stale / failed" : "missing"}</span>
                                <button type="button" onClick={mintEvidence} className="text-2xs uppercase tracking-wider underline decoration-dotted">↻ re-run</button>
                              </span>}
                        {mintResult && (
                          <div className="text-2xs mt-1 leading-relaxed max-w-[360px]" style={{ color: "var(--text-muted)" }}>
                            {mintResult}
                          </div>
                        )}
                      </td>
                      <td className="py-1.5 text-right font-mono text-2xs" style={{ color: gateReadiness?.receipt_ok ? "var(--accent-sage-fg)" : "var(--text-muted)" }}>
                        {gateReadiness?.receipt?.ended_at ? `fresh · ${String(gateReadiness.receipt.ended_at).slice(11, 19)}` : "—"}
                      </td>
                    </tr>
                    {(gateReadiness?.tests ?? []).map((tr) => (
                      <tr key={tr.id}>
                        <td className="py-1.5 pr-3 pl-4">
                          <button type="button" onClick={() => navigate(tr.href)}
                            className="font-mono underline decoration-dotted underline-offset-2 text-left"
                            title={tr.id}>
                            △ {tr.label}
                          </button>
                          <div className="text-2xs" style={{ color: "var(--text-muted)" }}>decides this gate · receipt-backed</div>
                        </td>
                        <td className="py-1.5 pr-3">
                          {tr.passed === true
                            ? <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)" }}>passed</span>
                            : tr.passed === false
                              ? <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}>failed</span>
                              : <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--text-muted)", boxShadow: "inset 0 0 0 1px var(--border-default)" }}>not run</span>}
                        </td>
                        <td className="py-1.5 text-right font-mono text-2xs" style={{ color: "var(--text-muted)" }}>
                          {tr.ended_at ? `${String(tr.ended_at).slice(11, 19)} · ${tr.receipt_job_id.slice(0, 8)}` : "—"}
                        </td>
                      </tr>
                    ))}
                    <tr>
                      <td className="py-1.5 pr-3">
                        <span className="font-mono">shell verifier · green_full</span>
                        <div className="text-2xs" style={{ color: "var(--text-muted)" }}>the release gate — runs on Approve</div>
                      </td>
                      <td className="py-1.5 pr-3">
                        {verifierRefusal
                          ? <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}>{verifierRefusal.summary}</span>
                          : <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={{ color: "var(--text-muted)", boxShadow: "inset 0 0 0 1px var(--border-default)" }}>not yet run</span>}
                      </td>
                      <td className="py-1.5 text-right font-mono text-2xs" style={{ color: "var(--text-muted)" }}>
                        {verifierRefusal ? "last decision" : "—"}
                      </td>
                    </tr>
                    {pinTests.length > 0 && (() => {
                      // When a gate receipt exists, the authoritative "pinned"
                      // set is the tests the receipt actually decided (the
                      // oracle) — not every file that merely names the task.
                      const gateVerified = pinTests.filter((t) => t.verified_by === "gate-receipt");
                      const scope = (testReceipt && gateVerified.length > 0) ? gateVerified : pinTests;
                      const passing = scope.filter((t) => (t.status || "").toLowerCase() === "passed").length;
                      return (
                        <tr>
                          <td className="py-1.5 pr-3">
                            <button type="button" onClick={showTests} className="font-mono underline decoration-dotted underline-offset-2 text-left">pinned tests</button>
                            {/* Name the tree the count came from and when it was taken, so
                                the number is checkable rather than merely trusted. */}
                            {(pinSource.source || pinTestsAt) && (
                              <div className="text-2xs font-mono" style={{ color: "var(--text-muted)" }}>
                                {pinSource.source ? `${pinSource.source}${pinSource.source_sha ? ` ${pinSource.source_sha.slice(0, 7)}` : ""}` : ""}
                                {pinTestsAt ? ` · as of ${pinTestsAt}` : ""}
                              </div>
                            )}
                            <div className="text-2xs" style={{ color: testReceipt?.stale ? "var(--accent-amber-fg)" : "var(--text-muted)" }}>{!testReceipt ? "task's own suite — not the gate" : testReceipt.stale ? `stale — measured at ${testReceipt.tree_sha.slice(0, 7)}, NOT the tree under review${testReceipt.current_tree_sha ? ` (${testReceipt.current_tree_sha.slice(0, 7)})` : ""} · re-run to judge now` : "reflects the gate's trusted-runner result"}</div>
                          </td>
                          <td className="py-1.5 pr-3">
                            {/* A stale receipt is HISTORY: render it neutral and stamped with the
                                commit it was measured at, never as a live pass/fail verdict — an
                                amber "13 / 14 passing" from an older tree reads as a failure that
                                is happening NOW (owner 2026-07-21: "fake facts in prism"). */}
                            <span className="rounded-full px-2.5 py-0.5 font-mono text-2xs" style={testReceipt?.stale ? { color: "var(--text-muted)", boxShadow: "inset 0 0 0 1px var(--border-default)" } : passing === scope.length ? { color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)" } : { color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }}>{passing} / {scope.length} {testReceipt?.stale ? `at ${testReceipt.tree_sha.slice(0, 7)}` : "passing"}</span>
                          </td>
                          <td className="py-1.5 text-right font-mono text-2xs" style={{ color: testReceipt?.stale ? "var(--accent-amber-fg)" : testReceipt?.passed ? "var(--accent-sage-fg)" : "var(--text-muted)" }} title={testReceipt ? testReceipt.reason : ""}>{testReceipt ? `receipt ${testReceipt.job_id.slice(0, 8)} · ${testReceipt.tree_sha.slice(0, 7)}${testReceipt.stale ? " · STALE" : ""}` : "latest run"}</td>
                        </tr>
                      );
                    })()}
                  </tbody>
                </table>
              </div>

              {/* 3 · VERIFIER DECISION — plain language; machine text collapsed */}
              <div className="p-4 space-y-2">
                <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>verifier decision</div>
                {verifierRefusal ? (
                  <div className="rounded-md p-3 text-[12.5px] leading-relaxed" style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}>
                    <b>Refused.</b> Gate expects <code className="font-mono text-2xs px-1 rounded" style={{ background: "var(--surface-1)", color: "var(--text-primary)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}>verifier.status = pass</code>; current run is <code className="font-mono text-2xs px-1 rounded" style={{ background: "var(--surface-1)", color: "var(--text-primary)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}>fail</code>.
                    <div className="mt-1.5">
                      {verifierRefusal.detail} — a skipped check is refused, not a pass. Pinned tests
                      ({pinTests.length ? `${pinTests.filter((t) => (t.status || "").toLowerCase() === "passed").length}/${pinTests.length}` : "—"}) are the task's own suite
                      and don't satisfy the gate. This verifier is a known engine defect (68e5c699) — override below is the audited recovery.
                    </div>
                  </div>
                ) : (
                  <div className="text-[12.5px] text-[color:var(--text-secondary)]">
                    Not decided yet — the machine check runs when you click Approve (takes up to a few minutes).
                  </div>
                )}
                {/* task a646cbd1: task.gate_reason is a STORED snapshot that
                    can read as inviting ("evidence is ready; Approve") even
                    while the live gate/readiness evaluation below is
                    actively refusing — the collapsed block must consult it
                    too, not just render the stale string unconditionally. */}
                {(task.gate_reason || gateEvidenceLines(history).length > 0) && (
                  <Disclosure
                    className="text-[12px]"
                    summaryClassName="text-2xs uppercase tracking-wider"
                    summaryStyle={{ color: "var(--text-muted)" }}
                    summary="audit detail (machine text)"
                  >
                    <div className="mt-1.5 space-y-1">
                      {task.gate_reason && gateReadiness?.receipt_ok !== false && <div className="font-mono text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>{task.gate_reason}</div>}
                      {gateReadiness?.receipt_ok === false && gateReadiness?.receipt_refusal && (
                        <div className="font-mono text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>live: {gateReadiness.receipt_refusal}</div>
                      )}
                      {gateEvidenceLines(history).slice(0, 4).map((l, i) => (
                        <div key={i} className="font-mono text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>• {l}</div>
                      ))}
                    </div>
                  </Disclosure>
                )}
              </div>

              {/* 4 · YOUR DECISION — action last, controls match the state */}
              <div className="p-4 space-y-2.5">
                <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>your decision</div>
                <textarea
                  value={gateReason}
                  onChange={(e) => setGateReason(e.target.value)}
                  placeholder="Optional note — recorded on the gate's audit trail (required to reject or override)"
                  rows={2}
                  className="w-full text-[13px] rounded-md bg-[color:var(--background-base)]/40 border border-[color:var(--midground-base)]/20 p-2 leading-relaxed resize-y"
                />
                {gateVerdict !== "ready" && (
                  <label className="flex items-center gap-2 text-[12px] cursor-pointer">
                    <input type="checkbox" checked={gateOverride} onChange={(e) => setGateOverride(e.target.checked)} />
                    <span><b style={{ color: "var(--accent-rose-fg)" }}>Override</b> — bypasses the verifier's automated check only. Audited. The oracle evidence receipt is still required: a stale or refused receipt still refuses this Approve even with override ticked. To recover, re-run the oracle for a fresh receipt, then Approve with override unticked.</span>
                  </label>
                )}
                {gateResult && (
                  <div
                    className="rounded-md p-2.5 text-[12.5px] leading-relaxed"
                    style={gateResult.kind === "ok"
                      ? { background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)", boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)" }
                      : gateResult.kind === "checking"
                        ? { background: "var(--surface-2)", color: "var(--text-secondary)", boxShadow: "inset 0 0 0 1px var(--border-default)" }
                        : { background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}
                    role="status"
                  >
                    {gateResult.kind === "checking" && <span className="inline-block h-2 w-2 rounded-full animate-pulse mr-2" style={{ background: "var(--text-muted)" }} />}
                    {gateResult.text}
                  </div>
                )}
                <div className="flex items-center gap-3 flex-wrap">
                  <button
                    id="gate-decide-approve"
                    type="button"
                    disabled={busy || (gateVerdict !== "ready" && !isAwaitingDesignApproval && !gateOverride) || (gateOverride && !gateReason.trim())}
                    onClick={() => gateDecide("approve")}
                    className="text-2xs uppercase tracking-wider px-3.5 py-1.5 rounded disabled:opacity-40"
                    style={gateVerdict === "ready" || gateOverride
                      ? { background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)", boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)" }
                      : { color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}
                  >
                    {busy ? "checking…" : task.gate_state === "failed" ? "Approve (recover)" : "Approve"}
                  </button>
                  {task.gate_state !== "failed" && (
                    <button
                      id="gate-decide-reject"
                      type="button"
                      disabled={busy || !gateReason.trim()}
                      onClick={() => gateDecide("reject")}
                      className="text-2xs uppercase tracking-wider px-3.5 py-1.5 rounded disabled:opacity-40"
                      style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}
                    >
                      Reject
                    </button>
                  )}
                  <span className="text-2xs" style={{ color: "var(--text-muted)" }}>
                    {gateVerdict === "ready"
                      ? "Ready: Approve records your decision; the machine check runs first."
                      : isAwaitingDesignApproval
                        ? "Awaiting your approval: Approve records the design-packet sign-off and releases the gate."
                        : gateOverride
                          ? "Override armed: Approve releases on your judgment (audited)."
                          : "Blocked: tick override to recover, or fix the evidence and re-run."}
                  </span>
                </div>
              </div>
            </div>
      </>)}{/* end Evidence tab */}

      {docTab === "overview" && (<>
      {/* Dead-task gate card (task e948008a): a cancelled/archived/deleted
          task parked at a pending/failed gate renders INERT — the decision
          is moot, so no Approve/Override/re-run — while evidence and gate
          history keep rendering read-only via this branch's own smaller
          projection (never the shared helper above; ordering invariant
          pinned by test_task_oracle_always_visible_ui.py). Appended AFTER
          the live branch closes; never hides or unmounts it. */}
      {conductorOn && (task.gate_state === "pending" || task.gate_state === "failed") && (task.status === "cancelled" || task.status === "archived" || task.status === "deleted") && (
        <div className="rounded-md border overflow-hidden" style={{ borderColor: "var(--border-default)" }}>
          <div className="w-full text-left px-4 py-3 text-[13px] leading-relaxed" style={{ background: "var(--surface-2)", color: "var(--text-secondary)" }}>
            <span className="text-2xs uppercase tracking-wider font-semibold mr-2">{task.status.toUpperCase()}</span>
            decision moot — this task is {task.status} and its parked gate is no longer actionable. Evidence below is read-only history.
          </div>
          <div className="p-4 space-y-3 text-[12.5px]" style={{ borderTop: "1px solid var(--border-default)" }}>
            {/* ProofShots self-guards (returns null with no matches) - no
                wrapping regex test needed here, so this branch never adds a
                second copy of the visual-evidence detector's unbalanced-
                paren regex literal (the live branch's copy at ~line 1619
                already sits inside the file's naive whole-component
                paren-balance budget; a duplicate tips it negative and
                strands test_stale_gate_banner_inspect_vs_override.py's
                _gate_panel_block scan). */}
            <ProofShots text={task.completion_proof} className="" />
            {pinTests.length > 0 && (
              <div className="text-2xs" style={{ color: "var(--text-muted)" }}>
                pinned tests: {pinTests.filter((t) => (t.status || "").toLowerCase() === "passed").length} / {pinTests.length} passing (last observed)
              </div>
            )}
            {(task.gate_reason || gateEvidenceLines(history).length > 0) && (
              <div className="space-y-1">
                {task.gate_reason && <div className="font-mono text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>{task.gate_reason}</div>}
                {gateEvidenceLines(history).slice(0, 4).map((l, i) => (
                  <div key={i} className="font-mono text-2xs leading-relaxed" style={{ color: "var(--text-muted)" }}>• {l}</div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
      {task.status === "blocked" && task.blocked_reason && (
        <div
          className="rounded-md px-4 py-3 text-[13px]"
          style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}
        >
          <span className="text-2xs uppercase tracking-wider font-semibold mr-2">blocked</span>
          {task.blocked_reason}
        </div>
      )}

      {(conductorOn || task.plan_doc || task.plan_diagram || task.has_prototype || pinTests.length > 0) && (
        <Stagger i={0} reduced={reduced}>
        <div ref={planRef}>
        <Card>
          {conductorOn && (
            <div className="flex items-center gap-1.5 px-1 pb-2">
              <span className="text-2xs uppercase tracking-wider opacity-50">Workflow</span>
              <Lozenge tone="neutral">{task.workflow || "implement"}</Lozenge>
            </div>
          )}
          <PlanView
            diagram={task.plan_diagram}
            doc={task.plan_doc}
            prototypeSrc={task.has_prototype
              ? `/api/tasks/${id}/prototype?project=${encodeURIComponent(project)}`
              : undefined}
            reduced={reduced}
            pinTests={pinTests}
            runningTests={runningTests}
            onRunTests={runTests}
            gateReadiness={gateReadiness}
            onMintEvidence={mintEvidence}
            tabRequest={tabRequest}
            taskId={id}
            project={project}
            proofType={task.proof_type}
            oracle={gatePanelOwnsOracle ? undefined : task.oracle}
            completionProof={task.completion_proof}
            likelyMisfire={task.likely_misfire}
            fullOutcomeComplete={task.full_outcome_complete}
            isAwaitingDesignApproval={isAwaitingDesignApproval}
            onApproveDesign={() => gateDecide("approve")}
            designPacketRefreshToken={designPacketRefreshToken}
            conductor={conductorOn ? {
              step: task.workflow_step,
              gateState: task.gate_state,
              gateReason: task.gate_reason,
              phase: task.phase_progress,
              status: task.status,
              activity: task.activity,
              timeline,
              turns: history,
              stepTokens: task.step_tokens,
            } : null}
            gate={null /* the gate DECISION lives in the top-level action
              panel on Overview now (owner: notifications must be top-level
              and actionable in place) — Implementation keeps the rail */}
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

      {/* DELIVERY — where the work IS and what's left before it's truly done
          (owner 2026-07-16: done means SHIPPED, merged + validated on main).
          Only rendered once the task has something to deliver. */}
      {delivery && (delivery.commits.length > 0 || task.status === "done") && (
        <div id="delivery-card">
        <Card>
          <SectionLabel>Delivery — where this work is</SectionLabel>
          <div className="mt-3 flex items-center gap-0 flex-wrap">
            {delivery.stages.map((st, i) => (
              <div key={st.key} className="flex items-center min-w-0">
                {i > 0 && (
                  <span className="h-px w-6 min-[720px]:w-10 mx-1 shrink-0" style={{
                    background: st.state === "done" ? "var(--accent-sage-fg)" : "var(--border-default)",
                    opacity: st.state === "done" ? 0.6 : 1,
                  }} />
                )}
                <span
                  className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-2xs uppercase tracking-wider font-semibold shrink-0"
                  title={st.detail}
                  style={st.state === "done"
                    ? { color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)", background: "var(--accent-sage-bg)" }
                    : st.state === "next"
                      ? { color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)", background: "var(--accent-amber-bg)" }
                      : { color: "var(--text-muted)", boxShadow: "inset 0 0 0 1px var(--border-default)" }}
                >
                  {st.state === "done" ? "✓" : st.state === "next" ? "●" : "○"} {st.label}
                </span>
              </div>
            ))}
            {!delivery.delivered && (
              <span className="ml-3 text-2xs" style={{ color: "var(--accent-amber-fg)" }}>
                not yet delivered — {delivery.stages.find((s) => s.state === "next")?.detail}
              </span>
            )}
            {delivery.delivered && (
              <span className="ml-3 text-2xs" style={{ color: "var(--accent-sage-fg)" }}>
                delivered{delivery.commits[0]?.released_in ? ` in ${delivery.commits[0].released_in}` : ""}
              </span>
            )}
          </div>
          {delivery.commits.length > 0 && (
            <div className="mt-2.5 space-y-1">
              {delivery.commits.map((c) => (
                <div key={c.sha} className="flex items-center gap-2 text-[12.5px] min-w-0">
                  <span className="font-mono text-2xs shrink-0" style={{ color: "var(--text-muted)" }}>{c.sha}</span>
                  <span className="truncate" style={{ color: "var(--text-secondary)" }}>{c.subject}</span>
                  <span className="ml-auto font-mono text-2xs shrink-0" style={{ color: "var(--text-muted)" }}>
                    {c.released_in ? `released · ${c.released_in}` : c.merged_to_main ? "on main" : c.pushed ? "pushed" : `local · ${delivery.branch}`}
                  </span>
                </div>
              ))}
            </div>
          )}
        </Card>
        </div>
      )}

      {/* When the task is in the conductor, the oracle lives in the Tests tab
          and completion proof & risk lives in the gate's evidence area — so
          this standalone card is only the fallback for non-pipeline tasks. */}
      {!conductorOn && (task.oracle || task.proof_type || task.completion_proof || task.likely_misfire || task.full_outcome_complete !== undefined || pinTests.length > 0) && (
        <Card>
          {/* The oracle itself moved to the Tests tab (oracle + tests belong
              together, tied to the proposed change's ACs). This panel keeps the
              completion PROOF and the risk (misfire / owner-outcome). */}
          <SectionLabel>Completion — proof &amp; risk</SectionLabel>
          <div className="mt-2 space-y-3 text-[13px]">
            {task.proof_type && (
              <div className="flex items-center gap-2">
                <span className="opacity-50 text-2xs uppercase tracking-wider">proof type</span>
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
              </div>
            )}
            <div>
              <div className="opacity-50 mb-1 text-2xs uppercase tracking-wider">completion proof</div>
              {task.completion_proof
                ? <>
                    <button
                      onClick={() => navigate(`/tasks/${id}/proof`, { state: { from: `/tasks/${id}` } })}
                      className="flex items-center gap-1.5 text-left w-full group"
                    >
                      <span className="leading-relaxed opacity-80 group-hover:opacity-100 truncate">{oneLine(task.completion_proof)}</span>
                      <span className="opacity-50 group-hover:opacity-100 shrink-0">→</span>
                    </button>
                    <ProofShots text={task.completion_proof} className="mt-2" />
                  </>
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
              // One-line summary from the REAL latest run (never a hardcoded
              // red): "Pinning tests · 14/14 PASS" once the outcomes hold,
              // "N RED" only while they genuinely fail, "not run" honestly
              // when no run happened. Clicking opens the Tests tab.
              const acs = pinTests.map((t) => parseAc(t.doc).badge).filter(Boolean) as string[];
              const failing = pinTests.filter((t) => ["failed", "error"].includes((t.status || "").toLowerCase())).length;
              const passing = pinTests.filter((t) => (t.status || "").toLowerCase() === "passed").length;
              const ran = pinTests.some((t) => t.status);
              const view = failing > 0
                ? { mark: "✗", tone: "var(--accent-rose-fg)", text: `Pinning tests · ${failing} RED of ${pinTests.length}`, label: "pinning tests — what currently proves it's NOT done" }
                : ran && passing === pinTests.length
                  ? { mark: "✓", tone: "var(--accent-sage-fg)", text: `Pinning tests · ${passing}/${pinTests.length} PASS`, label: "pinning tests — the acceptance criteria hold (latest real run)" }
                  : { mark: "○", tone: "var(--text-muted)", text: `Pinning tests · ${pinTests.length} (latest outcomes unavailable)`, label: "pinning tests" };
              return (
                <div className="pt-3" style={{ borderTop: "1px solid var(--surface-3)" }}>
                  <div className="opacity-50 mb-1 text-2xs uppercase tracking-wider">
                    {view.label}
                  </div>
                  <button
                    type="button"
                    onClick={showTests}
                    className="flex items-center gap-2 text-left w-full group"
                    title="view the pinning tests, correlated to their acceptance criteria"
                  >
                    <span className="shrink-0" style={{ color: view.tone }} aria-hidden>{view.mark}</span>
                    <span className="leading-relaxed text-[color:var(--text-primary)]">
                      {view.text}
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
                  <Link
                    to={`/sessions/${s.session_id}`}
                    state={{ from: `/tasks/${id}` }}
                    className="font-mono text-[12px] break-all underline decoration-dotted underline-offset-2 hover:opacity-80"
                  >
                    {s.session_id}
                  </Link>
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

      </>)}{/* end Overview tab */}

      </div>{/* end left doc column */}

      <aside className="grid grid-cols-1 min-[720px]:grid-cols-2 gap-3.5 items-start min-w-0">
        <RailCard title="Details">
          <Field k="Status"><Lozenge tone={statusLoz(taskStatus)}>{taskStatus.replace(/_/g, " ")}</Lozenge></Field>
          {task.workflow_step && <Field k="Step"><Lozenge tone="neutral">{task.workflow_step}</Lozenge></Field>}
          {gateActive && <Field k="Gate"><Lozenge tone={gateLoz(task.gate_state ?? "none")}>{task.gate_state}</Lozenge></Field>}
          {typeof task.priority !== "undefined" && <Field k="Priority">p{task.priority}</Field>}
          {task.proof_type && <Field k="Proof"><span className="font-mono text-xs">{task.proof_type}</span></Field>}
          {task.updated_at && <Field k="Updated"><span className="text-[color:var(--text-secondary)]">{String(task.updated_at).slice(0, 10)}</span></Field>}
        </RailCard>

        <RailCard title="Connections" count={connectionCount || undefined}>
          {!hasConnections && (
            <div className="px-3.5 py-2 text-xs text-[color:var(--text-muted)]">No connections yet.</div>
          )}
          {realSessions.length > 0 && (
            <RelGroup label="Sessions">
              {realSessions.map((s) => (
                <RelRow key={s.session_id}>
                  <EntityChip
                    kind="session"
                    label={`${s.session_id.slice(0, 8)} · ${s.started_at ? String(s.started_at).slice(0, 10) : "—"}`}
                    to={`/sessions/${s.session_id}`}
                  />
                </RelRow>
              ))}
            </RelGroup>
          )}
          {codePaths.length > 0 && (
            <RelGroup label="Code touched">
              {codeShown.map((p) => (
                <RelRow key={p}>
                  <EntityChip kind="code" label={shortPath(p)} title={p} to={`/artifact?focus=${encodeURIComponent(p)}`} />
                </RelRow>
              ))}
              {codeHidden > 0 && (
                <button
                  onClick={() => setCodeExpanded(true)}
                  className="px-3.5 py-1 text-xs text-[color:var(--accent-teal-fg)] hover:underline"
                >
                  {codeHidden} more
                </button>
              )}
            </RelGroup>
          )}
          {knowledge.length > 0 && (
            <RelGroup label="Knowledge · Understand">
              {knowledge.map((k) => (
                <RelRow key={k.id} why={k.type}>
                  <EntityChip
                    kind="memory"
                    label={oneLine(k.title, 26)}
                    title={k.title}
                    to={`/understand?concept=${encodeURIComponent(k.id)}`}
                  />
                </RelRow>
              ))}
            </RelGroup>
          )}
          {(gateActive || gateStep) && (
            <RelGroup label="Gate">
              <RelRow>
                <EntityChip kind="gate" label={`${task.workflow_step ?? "gate"} · ${task.gate_state ?? "none"}`} />
              </RelRow>
            </RelGroup>
          )}
          {children.length > 0 && (
            <RelGroup label="Children">
              {children.map((c) => (
                <RelRow key={c.id} why={c.status}>
                  <EntityChip kind="task" label={oneLine(c.title ?? String(c.id).slice(0, 8), 26)} title={c.title} to={`/tasks/${c.id}`} />
                </RelRow>
              ))}
            </RelGroup>
          )}
        </RailCard>
      </aside>

      </div>{/* end .issue grid */}
    </Page>
  );
}

