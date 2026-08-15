/**
 * LiveBar — the conductor's realtime pulse as a persistent app-shell card.
 *
 * Mounted ONCE in App.tsx between <PageHeader> and the routed content, but
 * scoped to ACTIVITY-context routes only (owner 2026-07-14: "the task live
 * bar is showing on the understand view, that does not work for me, we are
 * not in that context") — Dashboard, Tasks, Conductor. Knowledge and
 * learning-loop surfaces stay free of task chrome. It reads the SAME honest
 * work-state ConductorPage/TasksPage read — /api/conductor/state
 * activity.state is "working" ONLY when a task is being driven right now —
 * so it can NEVER paint a parked task green.
 *
 * This is the conductor pulse; it is DISTINCT from <LiveStatusStrip>, which
 * is the analyzer scan-queue strip.
 */
import { useEffect, useState, useCallback, useRef } from "react";
import { Link, useLocation } from "react-router-dom";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { stepLabel } from "@/lib/workflowChips";
import { ACTIVITY_META } from "@/components/conductor/SdlcProgress";
import { useScanActivity } from "@/lib/scan-activity";
import { type Activity } from "@/components/conductor/SdlcProgress";
import { Lozenge } from "@/components/Lozenge";
import { EntityChip } from "@/components/EntityChip";
import { GateEvidenceBlock } from "@/components/conductor/TaskActivityGantt";

// Conductor live-state (server: /api/conductor/state → managed_tasks). The bar
// reads activity.state, never the raw task status, so a parked task can't fake
// liveness.
type ManagedTask = {
  id: string;
  title?: string;
  workflow_step?: string;
  gate_state?: string;
  assigned_agent?: string;
  updated_at?: string;
  activity?: Activity | null;
  // Has conductor_work ever touched this task (api/conductor.py:104-117
  // _with_claimed) -- True forever once a workspace exists, never a
  // recency check. Gates drive chrome, distinct from workflow_step/gate_state.
  claimed?: boolean;
};

const shortId = (id?: string) => (id ?? "").slice(0, 8) || "—";

// " · 5h" wait-duration suffix for a gated chip (artifact: AWAITING
// GREEN_GATE · 5H). Empty when the stamp is missing/unparseable.
function waitFor(ts?: string): string {
  if (!ts) return "";
  const ms = Date.now() - Date.parse(ts);
  if (!Number.isFinite(ms) || ms < 60_000) return "";
  const m = Math.floor(ms / 60_000);
  if (m < 60) return ` · ${m}m`;
  const h = Math.floor(m / 60);
  return h < 48 ? ` · ${h}h` : ` · ${Math.floor(h / 24)}d`;
}

// Activity-context routes where the conductor pulse belongs. Everything
// else (Explore/Understand/Sessions/Learning/Settings) renders no bar.
const ACTIVITY_ROUTES = ["/", "/tasks", "/conductor"];

// Fallback-sweep staleness threshold (task 4d399e0a) — the sweep only
// refetches once a fetch is genuinely this old, never on a bare interval.
const STALE_AFTER_MS = 15_000;
// Minimum spacing between conductor/state fetch starts — absorbs the
// task.changed event bursts a busy drive emits (several per second).
const MIN_REFRESH_MS = 3_000;
// Parentage projection cadence — moves on task creation, not on step writes.
const PARENTAGE_REFRESH_MS = 60_000;

function inActivityContext(pathname: string): boolean {
  return ACTIVITY_ROUTES.some(
    (r) => pathname === r || (r !== "/" && pathname.startsWith(r + "/")),
  ) || pathname.startsWith("/tasks");
}

export default function LiveBar() {
  const [project] = useProject();
  const { pathname } = useLocation();
  const [managed, setManaged] = useState<ManagedTask[]>([]);
  // `managed` inits [] which computes state="idle" — so the bar flashed
  // "Idle · no task being driven — queue is quiet" before its FIRST poll had
  // even returned, on every load (task 89e90d1a likely_misfire). Hold a
  // neutral "connecting" state until this flips.
  const [polled, setPolled] = useState(false);
  const [version, setVersion] = useState<string>("");
  const [collapsed, setCollapsed] = useState(false);
  // Task 25a25d84: the "awaiting gate" chip expands (click) into the gate's
  // RECEIPT-captured evidence — one task open at a time keeps the bar compact.
  const [expandedGate, setExpandedGate] = useState<string | null>(null);
  const scan = useScanActivity();

  // Heartbeat: the bar must VISIBLY tick or it's indistinguishable from frozen.
  // sinceFetchS counts up every second and RESETS on each 5s poll — the same
  // honest-liveness trick the conductor tiles use.
  const fetchedAt = useRef(0);
  const [sinceFetchS, setSinceFetchS] = useState(0);
  // child task id -> parent task id, for rolling driven slices up to the epic.
  const [parentOf, setParentOf] = useState<Record<string, string>>({});

  // Burst coalescing (loading-perf round 2026-08-13): task.changed fires on
  // EVERY conductor write and a busy drive emits several per second; each one
  // used to refire the conductor/state fetch from EVERY open page, and the
  // server-side recompute serializes under that storm. One fetch per
  // MIN_REFRESH_MS window absorbs the burst; a single trailing call keeps the
  // final event's state, so nothing observable is ever dropped.
  const lastStartRef = useRef(-Infinity);
  const trailingRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const parentageAt = useRef(-Infinity);

  const doLoad = useCallback(() => {
    api.get<{ managed_tasks?: ManagedTask[] }>(`/api/conductor/state?project=${project}`)
      .then((d) => { setManaged(d.managed_tasks ?? []); fetchedAt.current = performance.now(); setSinceFetchS(0); })
      .catch(() => setManaged([]))
      .finally(() => setPolled(true));
    // PARENTAGE, fetched separately because managed_tasks does not carry it
    // (ConductorService.managed_tasks selects on workflow_step alone and emits
    // no parent_id, so a driven CHILD is indistinguishable from a root epic
    // here). Without this the bar renders a driver's own decomposition as
    // peers of the epic the owner is watching — owner 2026-08-06: "those are
    // sub tasks ... they are your details, you do not have me conduct them
    // for you". Lean projection, same shape TasksPage.tsx already requests.
    // Parentage moves on task creation/re-parenting, not on step writes —
    // once a minute is current enough and keeps the burst path to ONE fetch.
    if (performance.now() - parentageAt.current >= PARENTAGE_REFRESH_MS) {
      parentageAt.current = performance.now();
      api.get<{ tasks?: { id: string; parent_id?: string }[] }>(
        `/api/tasks?project=${project}&fields=id,parent_id`)
        .then((d) => {
          const map: Record<string, string> = {};
          for (const t of d.tasks ?? []) if (t.parent_id) map[t.id] = t.parent_id;
          setParentOf(map);
        })
        .catch(() => { /* leave parentage as-is; never blank a known map */ });
    }
  }, [project]);

  const load = useCallback(() => {
    const since = performance.now() - lastStartRef.current;
    if (since < MIN_REFRESH_MS) {
      if (trailingRef.current == null) {
        trailingRef.current = setTimeout(() => {
          trailingRef.current = null;
          lastStartRef.current = performance.now();
          doLoad();
        }, MIN_REFRESH_MS - since);
      }
      return;
    }
    lastStartRef.current = performance.now();
    doLoad();
  }, [doLoad]);
  useEffect(() => () => {
    if (trailingRef.current != null) clearTimeout(trailingRef.current);
  }, []);

  // Push refresh (task 4d399e0a): subscribe to the project's real event bus
  // (same route SessionsPage.tsx already uses) rather than /sse/live, which
  // only ever emits one connect payload plus ": keepalive" comment lines and
  // can never fire onmessage. TaskService.update publishes task.changed on
  // every conductor step/gate write, so this reaches load() on real motion
  // -- not merely on "the socket is open" like the retired sseHealthy did.
  useEffect(() => {
    const es = new EventSource(`/sse/sessions?project=${project}`);
    es.onmessage = () => { load(); };
    return () => es.close();
  }, [project, load]);

  // Fallback sweep only (task 4d399e0a): the SSE subscribe above is the
  // primary refresh path now, so this never gates on connection health --
  // gating a "is push alive" flag was the exact bug (onopen fires once,
  // stays true forever, load() never runs again). It gates on tab
  // visibility + genuine STALENESS instead, a safety net for the case push
  // itself observes nothing to say (ConductorService.activity_for decays
  // working -> adrift -> stalled purely on elapsed time, no bus event).
  useEffect(() => {
    const sweep = () => {
      if (!document.hidden && performance.now() - fetchedAt.current >= STALE_AFTER_MS) load();
    };
    load();
    const t = setInterval(sweep, 2000);
    document.addEventListener("visibilitychange", sweep);
    return () => { clearInterval(t); document.removeEventListener("visibilitychange", sweep); };
  }, [load]);
  useEffect(() => {
    const t = setInterval(() => setSinceFetchS(Math.max(0, (performance.now() - fetchedAt.current) / 1000)), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => { api.get<{ version: string }>(`/api/version`).then((d) => setVersion(d.version)).catch(() => {}); }, []);

  // ROOTS ONLY on this bar. A driven CHILD is the driver's own decomposition;
  // it rolls up into its epic's row as a slice count instead of claiming a row
  // of its own, and its gate never turns the bar amber — the owner is not the
  // reviewer for a subtask. Falls back to showing everything until the
  // parentage map has loaded, so nothing vanishes on a slow fetch.
  const roots = managed.filter((m) => !parentOf[m.id]);
  const slicesUnder = (rootId: string) =>
    managed.filter((m) => parentOf[m.id] === rootId).length;

  // "working" OR "driving" (task e3b7ebf6): a heartbeat-attributed step past
  // both the transition and session-quiet windows is genuinely being driven
  // too — never a call to action, so it rolls up into the same live row.
  const working = roots.filter((m) =>
    m.claimed && (m.activity?.state === "working" || m.activity?.state === "driving"));
  const gated = roots.filter((m) => (m.gate_state === "pending" || m.gate_state === "failed") || m.activity?.state === "awaiting_gate");
  // Managed, mid-flow, but neither moving nor at a gate (between reports,
  // adrift, stalled). The bar used to DROP these and claim "queue is quiet"
  // while a drive was literally in progress (owner 2026-07-16: "it says no
  // task being driven ??" during a live fleet drive). Never green — honest
  // activity doctrine paints work only on real motion — but never invisible.
  const inflow = roots.filter(
    (m) => (m.workflow_step ?? "") !== ""
      && !working.some((w) => w.id === m.id)
      && !gated.some((g) => g.id === m.id));
  const isLive = working.length > 0;
  // Honest states, in precedence order: LIVE (something is actually moving)
  // > AWAITING REVIEW (parked at a gate — a human/machine owes a decision)
  // > IN FLOW (managed work between reports) > IDLE (queue truly quiet).
  // "loading" precedes every honest state: until `polled` flips we have NOT
  // observed the queue, so claiming it is quiet would be a guess, not a fact.
  const state: "loading" | "live" | "gated" | "inflow" | "idle" =
    !polled ? "loading"
      : isLive ? "live" : gated.length > 0 ? "gated"
        : inflow.length > 0 ? "inflow" : "idle";
  const tint = {
    loading: { bg: "var(--surface-1)", ring: "var(--border-subtle)", fg: "var(--text-muted)" },
    live: { bg: "var(--accent-sage-bg)", ring: "var(--accent-sage-ring)", fg: "var(--accent-sage-fg)" },
    gated: { bg: "var(--accent-amber-bg)", ring: "var(--accent-amber-ring)", fg: "var(--accent-amber-fg)" },
    inflow: { bg: "var(--surface-1)", ring: "var(--border-default)", fg: "var(--text-secondary)" },
    idle: { bg: "var(--surface-1)", ring: "var(--border-subtle)", fg: "var(--text-muted)" },
  }[state];
  const stateLabel = state === "loading" ? "Connecting…"
    : state === "live" ? "Live" : state === "gated" ? "Awaiting review"
      : state === "inflow" ? "In flow" : "Idle";
  const heartbeat = `updated ${Math.floor(sinceFetchS)}s ago · queue ${scan.pending}${version ? ` · daemon v${version}` : ""}`;

  // Legible chip: the task TITLE (truncated), never a bare uuid — the full
  // id rides in the tooltip (owner: "it's hard to see what 401811b8 is").
  const chipLabel = (m: ManagedTask) => {
    const t = (m.title ?? "").trim();
    return t.length > 34 ? t.slice(0, 33) + "…" : t || shortId(m.id);
  };

  // Task chrome only where tasks are the context.
  if (!inActivityContext(pathname)) return null;

  // The artifact's .livebar is a padded rounded CARD inside the content
  // column, not an edge-to-edge strip. Header (state + heartbeat) is one row;
  // each managed task stacks as its OWN row beneath it (owner 2026-07-16).
  return (
    <div className="px-6 pt-4 shrink-0">
      <div
        className="rounded-lg border px-4 py-2.5 text-sm"
        role="status"
        style={{ borderColor: tint.ring, background: tint.bg }}
      >
        <div className="flex items-center gap-4">
          <button
            type="button"
            onClick={() => setCollapsed((c) => !c)}
            className="inline-flex items-center gap-2 shrink-0"
            aria-label={collapsed ? "expand live bar" : "collapse live bar"}
            aria-expanded={!collapsed}
          >
            <span
              className={"h-2.5 w-2.5 rounded-full " + (state === "live" ? "animate-pulse" : "")}
              style={{ background: tint.fg }}
            />
            <b className="text-2xs uppercase tracking-wider" style={{ color: tint.fg }}>
              {stateLabel}
            </b>
          </button>

          <span className="ml-auto text-2xs font-mono tabular-nums shrink-0" style={{ color: tint.fg }}>
            {heartbeat}
          </span>
        </div>

        {/* Each row IS the task (owner 2026-07-16: "each one of the items on
            the line is a task and needs to be able to be clicked through") —
            the WHOLE row is one <Link> to the task page, so the inner chip
            stays inert (no nested anchors). */}
        {!collapsed && working.map((m) => (
          <Link
            key={m.id}
            to={`/tasks/${m.id}`}
            title={`${m.title ?? ""} · ${m.id}`}
            className="mt-1.5 flex items-center gap-2 min-w-0 border-t pt-1.5 group"
            style={{ borderColor: tint.ring }}
          >
            <EntityChip kind="task" label={chipLabel(m)} />
            {m.workflow_step && <Lozenge tone="info">{stepLabel(m.workflow_step)}</Lozenge>}
            {/* Humanized through the SAME shared map every other activity
                surface reads (task e3b7ebf6 AC-4) -- never a raw enum or a
                literal that only ever matched "working". */}
            <Lozenge tone="ok">{ACTIVITY_META[m.activity?.state ?? "working"]?.label ?? "working"}</Lozenge>
            {slicesUnder(m.id) > 0 && (
              <Lozenge tone="info">{`${slicesUnder(m.id)} slice${slicesUnder(m.id) === 1 ? "" : "s"} running`}</Lozenge>
            )}
            <span className="text-2xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
              {m.assigned_agent || "claude-code"}
            </span>
            <span className="ml-auto text-xs opacity-0 group-hover:opacity-100" style={{ color: "var(--text-muted)" }}>
              open ›
            </span>
          </Link>
        ))}

        {!collapsed && gated.map((m) => {
          const isOpen = expandedGate === m.id;
          return (
            <div key={m.id} className="mt-1.5 border-t pt-1.5" style={{ borderColor: tint.ring }}>
              <div className="flex items-center gap-2 min-w-0 group">
                <Link
                  to={`/tasks/${m.id}`}
                  title={`${m.title ?? ""} · ${m.id}`}
                  className="flex items-center gap-2 min-w-0"
                >
                  <EntityChip kind="task" label={chipLabel(m)} />
                </Link>
                {/* The gate chip EXPANDS (click) to its captured-evidence
                    block, in-place — never navigates away to see it. */}
                <button
                  type="button"
                  onClick={() => setExpandedGate(isOpen ? null : m.id)}
                  className="flex items-center gap-1"
                  aria-expanded={isOpen}
                  title="click to view this gate's captured evidence"
                >
                  <Lozenge tone="warn">{`awaiting ${m.gate_state === "failed" ? "gate · failed" : (m.workflow_step || "gate")}${waitFor(m.updated_at)}`}</Lozenge>
                  <span className="text-2xs font-mono" style={{ color: tint.fg }}>{isOpen ? "▾" : "▸"}</span>
                </button>
                <Link
                  to={`/tasks/${m.id}`}
                  className="ml-auto text-xs opacity-0 group-hover:opacity-100"
                  style={{ color: "var(--text-muted)" }}
                >
                  open ›
                </Link>
              </div>
              {isOpen && (
                <div className="mt-2 pl-1">
                  <GateEvidenceBlock taskId={m.id} project={project} />
                </div>
              )}
            </div>
          );
        })}

        {!collapsed && inflow.map((m) => (
          <Link
            key={m.id}
            to={`/tasks/${m.id}`}
            title={`${m.title ?? ""} · ${m.id}`}
            className="mt-1.5 flex items-center gap-2 min-w-0 border-t pt-1.5 group"
            style={{ borderColor: tint.ring }}
          >
            <EntityChip kind="task" label={chipLabel(m)} />
            {m.workflow_step && <Lozenge tone="info">{stepLabel(m.workflow_step)}</Lozenge>}
            {/* Render the state through the SHARED humanized map — never the raw
                enum. This printed a bare "adrift" next to the bar's own "IN FLOW"
                header, so the strip contradicted itself and handed the owner an
                internal token to interpret (owner 2026-07-21: "what am I supposed
                to do in this state?"). */}
            <span className="text-2xs" style={{ color: "var(--text-muted)" }}>
              {ACTIVITY_META[(m.activity?.state ?? "").toLowerCase()]?.label
                ?? "between reports"}
            </span>
            <span className="ml-auto text-xs opacity-0 group-hover:opacity-100" style={{ color: "var(--text-muted)" }}>
              open ›
            </span>
          </Link>
        ))}

        {/* only claim the queue is quiet once `polled` proves we looked */}
        {!collapsed && polled && state === "idle" && (
          <div className="mt-1.5 border-t pt-1.5 text-xs" style={{ borderColor: tint.ring, color: "var(--text-muted)" }}>
            no task being driven — queue is quiet
          </div>
        )}
      </div>
    </div>
  );
}
