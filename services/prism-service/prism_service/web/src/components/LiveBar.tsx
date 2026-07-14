/**
 * LiveBar — the conductor's realtime pulse as a persistent app-shell strip.
 *
 * Mounted ONCE in App.tsx between <PageHeader> and the routed content, so the
 * pulse follows the user across every page (ws5 oracle: "visible on every
 * page"). It reads the SAME honest work-state ConductorPage/TasksPage read —
 * /api/conductor/state activity.state is "working" ONLY when a task is being
 * driven right now — so it can NEVER paint a parked task green: when nothing is
 * moving it shows the quiet daemon-heartbeat state, not a frozen live dot.
 *
 * This is the conductor pulse; it is DISTINCT from <LiveStatusStrip>, which is
 * the analyzer scan-queue strip. Both persist in the shell; they surface
 * different signals and neither replaces the other.
 *
 * Slim single row, collapsible to just the Live/Idle heartbeat line.
 */
import { useEffect, useState, useCallback, useRef } from "react";
import { api } from "@/lib/api";
import { useProject } from "@/lib/project";
import { stepLabel } from "@/lib/workflowChips";
import { useScanActivity } from "@/lib/scan-activity";
import { type Activity } from "@/components/conductor/SdlcProgress";
import { Lozenge } from "@/components/Lozenge";
import { EntityChip } from "@/components/EntityChip";

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

export default function LiveBar() {
  const [project] = useProject();
  const [managed, setManaged] = useState<ManagedTask[]>([]);
  const [version, setVersion] = useState<string>("");
  const [collapsed, setCollapsed] = useState(false);
  const scan = useScanActivity();

  // Heartbeat: the bar must VISIBLY tick or it's indistinguishable from frozen.
  // sinceFetchS counts up every second and RESETS on each 5s poll — the same
  // honest-liveness trick the conductor tiles use.
  const fetchedAt = useRef(0);
  const [sinceFetchS, setSinceFetchS] = useState(0);

  const load = useCallback(() => {
    api.get<{ managed_tasks?: ManagedTask[] }>(`/api/conductor/state?project=${project}`)
      .then((d) => { setManaged(d.managed_tasks ?? []); fetchedAt.current = performance.now(); setSinceFetchS(0); })
      .catch(() => setManaged([]));
  }, [project]);

  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, [load]);
  useEffect(() => {
    const t = setInterval(() => setSinceFetchS(Math.max(0, (performance.now() - fetchedAt.current) / 1000)), 1000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => { api.get<{ version: string }>(`/api/version`).then((d) => setVersion(d.version)).catch(() => {}); }, []);

  const working = managed.filter((m) => m.activity?.state === "working");
  const gated = managed.filter((m) => (m.gate_state === "pending" || m.gate_state === "failed") || m.activity?.state === "awaiting_gate");
  const isLive = working.length > 0;
  // Three honest states, in precedence order (artifact .livebar semantics):
  // LIVE (something is actually moving) > AWAITING REVIEW (work parked at a
  // gate — NOT "idle", a human owes a decision) > IDLE (queue truly quiet).
  const state: "live" | "gated" | "idle" =
    isLive ? "live" : gated.length > 0 ? "gated" : "idle";
  const tint = {
    live: { bg: "var(--accent-sage-bg)", ring: "var(--accent-sage-ring)", fg: "var(--accent-sage-fg)" },
    gated: { bg: "var(--accent-amber-bg)", ring: "var(--accent-amber-ring)", fg: "var(--accent-amber-fg)" },
    idle: { bg: "var(--surface-1)", ring: "var(--border-subtle)", fg: "var(--text-muted)" },
  }[state];
  const stateLabel = state === "live" ? "Live" : state === "gated" ? "Awaiting review" : "Idle";
  const heartbeat = `poll ${Math.floor(sinceFetchS)}s · queue ${scan.pending}${version ? ` · daemon v${version}` : ""}`;

  // Legible chip: the task TITLE (truncated), never a bare uuid — the full
  // id rides in the tooltip (owner: "it's hard to see what 401811b8 is").
  const chipLabel = (m: ManagedTask) => {
    const t = (m.title ?? "").trim();
    return t.length > 34 ? t.slice(0, 33) + "…" : t || shortId(m.id);
  };

  // The artifact's .livebar is a padded rounded CARD inside the content
  // column, not an edge-to-edge strip.
  return (
    <div className="px-6 pt-4 shrink-0">
      <div
        className="flex items-center gap-x-4 gap-y-1.5 flex-wrap rounded-lg border px-4 py-2.5 text-sm"
        role="status"
        style={{ borderColor: tint.ring, background: tint.bg }}
      >
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

        {!collapsed && working.map((m) => (
          <span key={m.id} className="inline-flex items-center gap-2 min-w-0">
            <span className="h-4 w-px shrink-0" style={{ background: tint.ring }} />
            <EntityChip kind="task" label={chipLabel(m)} to={`/tasks/${m.id}`} title={`${m.title ?? ""} · ${m.id}`} />
            {m.workflow_step && <Lozenge tone="info">{stepLabel(m.workflow_step)}</Lozenge>}
            <Lozenge tone="ok">working</Lozenge>
            <span className="text-2xs font-mono tabular-nums" style={{ color: "var(--text-muted)" }}>
              {m.assigned_agent || "claude-code"}
            </span>
          </span>
        ))}

        {!collapsed && gated.map((m) => (
          <span key={m.id} className="inline-flex items-center gap-2 min-w-0">
            <span className="h-4 w-px shrink-0" style={{ background: tint.ring }} />
            <EntityChip kind="task" label={chipLabel(m)} to={`/tasks/${m.id}`} title={`${m.title ?? ""} · ${m.id}`} />
            <Lozenge tone="warn">{`awaiting ${m.gate_state === "failed" ? "gate · failed" : (m.workflow_step || "gate")}${waitFor(m.updated_at)}`}</Lozenge>
          </span>
        ))}

        {!collapsed && state === "idle" && (
          <span className="text-xs" style={{ color: "var(--text-muted)" }}>no task being driven — queue is quiet</span>
        )}

        <span className="ml-auto text-2xs font-mono tabular-nums shrink-0" style={{ color: tint.fg }}>
          {heartbeat}
        </span>
      </div>
    </div>
  );
}
