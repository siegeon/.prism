import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

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
 * A compact overlay, docked top-right so it never collides with the
 * gate-decision panel (inset-3, opens on demand) or the reset-layout
 * button (bottom-left). Polls GET /api/system/activity every 1s -- its
 * own fetch, independent of the canvas's /api/work/graph boot and /sse/
 * work stream, so a slow or stalled graph fetch never blanks this panel.
 */

type ActivityEntry = {
  id: string;
  kind: string;
  project: string;
  detail: string;
  started_at: number;
  elapsed_ms: number;
  ok?: boolean;
};

type ActivitySnapshot = { running: ActivityEntry[]; recent: ActivityEntry[] };

const POLL_MS = 1000;

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
  const [snap, setSnap] = useState<ActivitySnapshot>({ running: [], recent: [] });
  const [collapsed, setCollapsed] = useState(false);
  // Recomputed every tick from `started_at`, independent of when the last
  // fetch happened -- a running pass's elapsed keeps climbing between polls
  // instead of freezing at the last snapshot's value.
  const [, forceTick] = useState(0);
  const cancelledRef = useRef(false);

  useEffect(() => {
    cancelledRef.current = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const poll = () => {
      api.get<ActivitySnapshot>(
        `/api/system/activity?project=${encodeURIComponent(project)}`,
      )
        .then((s) => { if (!cancelledRef.current) setSnap(s); })
        .catch(() => { /* transient fetch failure -- keep showing the last snapshot */ })
        .finally(() => {
          if (!cancelledRef.current) timer = setTimeout(poll, POLL_MS);
        });
    };
    poll();
    return () => { cancelledRef.current = true; if (timer) clearTimeout(timer); };
  }, [project]);

  // A separate, faster ticker so a running pass's elapsed counter moves
  // smoothly rather than jumping once per 1s fetch.
  useEffect(() => {
    const id = setInterval(() => forceTick((n) => n + 1), 250);
    return () => clearInterval(id);
  }, []);

  const now = Date.now();

  return (
    <div
      className="absolute top-3 right-3 z-10 w-72 max-w-[calc(100%-1.5rem)] rounded-lg border text-[11px] overflow-hidden"
      style={{ background: "var(--surface-1)", borderColor: "var(--border-default)" }}
    >
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        className="w-full flex items-center justify-between px-3 py-1.5 uppercase tracking-wider font-semibold"
        style={{ color: "var(--text-primary)" }}
      >
        <span>System activity</span>
        <span style={{ color: "var(--text-muted)" }}>
          {snap.running.length > 0 ? `${snap.running.length} running` : "idle"}
          {" "}{collapsed ? "▸" : "▾"}
        </span>
      </button>
      {!collapsed && (
        <div className="max-h-72 overflow-y-auto border-t px-3 py-2 space-y-2" style={{ borderColor: "var(--border-default)" }}>
          {snap.running.length > 0 && (
            <div className="space-y-1">
              {snap.running.map((e) => {
                const elapsed = e.started_at ? (now - e.started_at * 1000) : e.elapsed_ms;
                return (
                  <div key={e.id} className="flex items-center gap-1.5">
                    <span
                      className="inline-block h-1.5 w-1.5 rounded-full shrink-0 animate-pulse"
                      style={{ background: "var(--accent-sage-fg, #4ade80)" }}
                    />
                    <span className="truncate flex-1" style={{ color: "var(--text-primary)" }}>
                      {kindLabel(e.kind)}
                      {e.project && e.project !== "*" ? ` · ${e.project}` : ""}
                    </span>
                    <span className="shrink-0 font-mono" style={{ color: "var(--text-muted)" }}>
                      {fmtMs(Math.max(0, elapsed))}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
          <div>
            <div className="uppercase tracking-wider text-[10px] mb-1" style={{ color: "var(--text-muted)" }}>
              recent
            </div>
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
