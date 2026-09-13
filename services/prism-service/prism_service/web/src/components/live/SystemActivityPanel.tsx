import { useState } from "react";
import { usePolledResource } from "@/lib/usePolledResource";

/**
 * Task fixer-brief/activity (owner: "this is all about visibility /
 * observability of all processing happening in the system -- lightning
 * fast" and "until we can see the work being done most all things you say
 * are likely not true"). The daemon runs many background passes
 * (task_runner ticks, gate_adjudicator sweeps, resume_actuator, deploy
 * sweep, reap sweep, the language-alignment worker, the drift reindexer,
 * the maintenance-clock's brain-hygiene passes, ship_worker) and none of
 * it was visible AS it happened -- only CPU burn and a dark /live canvas.
 *
 * REDUCED TO "RECENT" ONLY 2026-09-13 (owner, verbatim, with a screenshot
 * of the conductor canvas): "i dont want the panel on the top, the
 * playing is supposed to be IN the graph like in a normal game." A
 * floating overlay read as a second, competing surface instead of
 * something drawn INTO the board it describes. The "running now" half of
 * this panel is GONE -- that is now live/draw.ts's drawWorkerRow, fed by
 * live/graphState.ts's GraphState.workers and driven by LivePage.tsx's own
 * `activity` GET /sse/changes subscription, drawn straight onto the canvas
 * as pulsing chips. This component keeps only the "recent" completed-
 * passes list, in a collapsed-by-default drawer docked along the BOTTOM of
 * the canvas -- out of the way of the gate-decision panel (inset-3) and
 * the reset-layout button (bottom-left, this drawer sits to its right).
 * Still polls GET /api/system/activity via the shared change-counter gate
 * (usePolledResource), independent of the canvas's own /api/work/graph
 * boot and /sse/work stream, so a slow or stalled graph fetch never blanks
 * this drawer.
 */

type ActivityEntry = {
  id: string;
  kind: string;
  project: string;
  detail: string;
  elapsed_ms: number;
  ok?: boolean;
};

type ActivitySnapshot = { recent: ActivityEntry[] };

const EMPTY_SNAPSHOT: ActivitySnapshot = { recent: [] };
// Stable identity (module scope, not an inline literal) so usePolledResource
// never re-subscribes on every render.
const ACTIVITY_KINDS = ["activity"];

function fmtMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  const m = Math.floor(s / 60);
  return `${m}m${Math.round(s - m * 60)}s`;
}

function kindLabel(kind: string): string {
  return kind.replace(/_/g, " ");
}

export default function SystemActivityPanel({ project = "prism" }: { project?: string }) {
  // Collapsed by default (owner 2026-09-13: the panel must not compete with
  // the graph) -- a single click still opens the drawer for the same detail
  // this panel has always shown, it just never imposes itself unasked.
  const [collapsed, setCollapsed] = useState(true);

  // Shared SSE gate (task fix/polling, SSE follow-up): refetch only on a
  // real GET /sse/changes "activity" event (services/system_activity.py's
  // record()/pass_() now call wakeups.signal("activity", project) at the
  // instant a pass starts or completes), on focus, or as a 60s reconnect
  // safety net if the stream itself looks unhealthy -- was a bare 1s
  // setTimeout loop forever.
  const { data } = usePolledResource<ActivitySnapshot>(
    `/api/system/activity?project=${encodeURIComponent(project)}`,
    project,
    ACTIVITY_KINDS,
  );
  const snap = data ?? EMPTY_SNAPSHOT;

  return (
    <div
      className="absolute bottom-3 right-3 z-10 w-72 max-w-[calc(100%-1.5rem)] rounded-lg border text-[11px] overflow-hidden"
      style={{ background: "var(--surface-1)", borderColor: "var(--border-default)" }}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between px-3 py-1.5 uppercase tracking-wider font-semibold"
        style={{ color: "var(--text-primary)" }}
      >
        <span>Recent activity</span>
        <span style={{ color: "var(--text-muted)" }}>
          {snap.recent.length > 0 ? `${snap.recent.length}` : "quiet"}
          {" "}{collapsed ? "▸" : "▾"}
        </span>
      </button>
      {!collapsed && (
        <div className="max-h-72 overflow-y-auto border-t px-3 py-2 space-y-2" style={{ borderColor: "var(--border-default)" }}>
          <div>
            {snap.recent.length === 0 ? (
              <div style={{ color: "var(--text-muted)" }}>nothing yet</div>
            ) : (
              <div className="space-y-1">
                {snap.recent.slice(0, 20).map((e) => (
                  <div key={e.id} className="flex items-center gap-1.5">
                    <span
                      className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
                      style={{
                        background: e.ok === false
                          ? "var(--accent-rose-fg, #f87171)"
                          : "var(--text-muted)",
                      }}
                    />
                    <span className="truncate flex-1" style={{ color: "var(--text-secondary, var(--text-primary))" }}>
                      {kindLabel(e.kind)}
                      {e.project && e.project !== "*" ? ` · ${e.project}` : ""}
                    </span>
                    <span className="shrink-0 font-mono" style={{ color: "var(--text-muted)" }}>
                      {fmtMs(e.elapsed_ms)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
