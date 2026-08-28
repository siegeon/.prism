/**
 * useConductorState — the ONE resolved source for GET /api/conductor/state
 * (task 40c29b83).
 *
 * Before this hook, LiveBar.tsx and ConductorPage.tsx each ran their OWN
 * independent fetch of the same endpoint, hardened unequally: LiveBar seeded
 * `polled` false and gated its idle copy behind it, while ConductorPage
 * seeded `data` null with no such guard and swallowed fetch failures into
 * `setData(null)`. A fresh load could show the live bar naming a task in
 * flow while the panel said nothing was managed, for the identical payload.
 *
 * This hook is lifted verbatim from LiveBar's already-hardened machinery:
 * the single fetch, the SSE push refresh (task 4d399e0a), the staleness
 * sweep, and the `polled` observed/not-yet-observed guard — plus a new
 * `error` flag so a fetch failure renders as its OWN reachability branch
 * rather than silently collapsing into "empty".
 */
import { useEffect, useState, useCallback, useRef } from "react";
import { subscribeStream } from "@/lib/sharedStream";
import { api } from "@/lib/api";
import { type Activity, type PhaseProgress } from "@/components/conductor/SdlcProgress";

export type ManagedTask = {
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
  // Task 2dfa94bd: True iff conductor_work has genuinely claimed this task
  // (task_workspace.workspace_record exists — ensure_workspace ran at its
  // first PEEK). A task someone flipped in_progress by hand, without ever
  // driving it through conductor_work, must NOT wear SDLC-drive chrome.
  claimed?: boolean;
  // Honest work state (server: conductor_service.activity_for) — the pill
  // and burn graph read THIS, never the raw status.
  activity?: Activity | null;
  subtasks?: { id: string; title: string; status: string }[];
};

export type BoardHealth = {
  consecutive_low_value?: number;
  reorient?: boolean;
  reason?: string;
};

export type ConductorState = {
  managed_tasks?: ManagedTask[];
  step_buckets?: Record<string, number>;
  board_health?: BoardHealth;
};

// Fallback-sweep staleness threshold (task 4d399e0a) — the sweep only
// refetches once a fetch is genuinely this old, never on a bare interval.
// Named + exported so any consumer tuning its OWN clock reads the same
// number LiveBar always has, instead of a second independently-picked one.
export const STALE_AFTER_MS = 15_000;
// Minimum spacing between conductor/state fetch STARTS (loading-perf round
// 2026-08-13, task 4d399e0a) — task.changed fires on EVERY conductor write
// and a busy drive emits several per second; unthrottled, each one refires
// this fetch and the server-side recompute serializes under the storm.
export const MIN_REFRESH_MS = 3_000;

// Cross-instance in-flight coalescer (loading-perf round 2, 2026-08-28):
// MIN_REFRESH_MS above throttles each MOUNTED CONSUMER to one fetch per
// window, but does nothing ACROSS consumers -- Sidebar.tsx (mounted on
// every page) plus whichever page-level consumer is also mounted each run
// their OWN doLoad(), so an SSE push still fires N near-simultaneous
// requests for the identical URL. Confirmed live: navigating to /workflows
// captured 13 concurrent GET /api/conductor/state calls in one burst,
// while the page's OWN one-time GET /api/workflows fetch never completed
// in time to populate the directory -- starved for a connection-pool slot
// by the duplicate traffic (task 04359d5b). Module-scope (not component
// state) so it is genuinely shared by every hook instance in this tab; a
// second consumer that calls in while a fetch is in flight gets the SAME
// promise instead of starting its own. Deliberately NOT a Context/store
// (mx-d412e0 already rejected that as heavier than this seam needs) --
// this is the same burst-coalescing idea the per-instance MIN_REFRESH_MS
// already uses, just shared instead of private to one instance.
const _inFlight = new Map<string, Promise<ConductorState>>();

function fetchConductorState(project: string): Promise<ConductorState> {
  const existing = _inFlight.get(project);
  if (existing) return existing;
  const request = api.get<ConductorState>(`/api/conductor/state?project=${project}`)
    .finally(() => { _inFlight.delete(project); });
  _inFlight.set(project, request);
  return request;
}

export function useConductorState(project: string) {
  const [data, setData] = useState<ConductorState | null>(null);
  // Seeds false so a consumer can tell "no resolved fetch yet" apart from
  // "resolved and genuinely empty" — flashing an honest-sounding-but-
  // premature "queue is quiet" / "no tasks under conductor management"
  // before the FIRST poll even returns was the root of task 40c29b83.
  const [polled, setPolled] = useState(false);
  // Distinct reachability flag (FR-6): a fetch failure is NOT the same
  // fact as "the queue is empty" and must render its own branch.
  const [error, setError] = useState(false);
  const fetchedAt = useRef(0);
  // Heartbeat: a card that isn't being driven still must VISIBLY tick, or
  // it's indistinguishable from frozen. Counts up every second and RESETS
  // on each successful poll (lifted from LiveBar, task 40c29b83) so every
  // consumer reads the SAME clock instead of keeping its own.
  const [sinceFetchS, setSinceFetchS] = useState(0);
  const lastStartRef = useRef(-Infinity);
  const trailingRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const doLoad = useCallback(() => {
    fetchConductorState(project)
      .then((d) => {
        setData(d);
        setError(false);
        fetchedAt.current = performance.now();
        setSinceFetchS(0);
      })
      .catch(() => setError(true))
      .finally(() => setPolled(true));
  }, [project]);

  // Burst coalescing (loading-perf round 2026-08-13, task 4d399e0a): every
  // trigger below (SSE push, staleness sweep, a consumer's manual refresh())
  // funnels through here, so a busy drive's several-per-second event storm
  // collapses to one fetch per MIN_REFRESH_MS window; a single trailing call
  // keeps the final event's state, so nothing observable is ever dropped.
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
  // (same route SessionsPage.tsx uses) — TaskService.update publishes
  // task.changed on every conductor step/gate write, so this reaches load()
  // on real motion.
  useEffect(() => subscribeStream(`/sse/sessions?project=${project}`, () => {
    load();
  }), [project, load]);

  // Fallback sweep only: gates on tab visibility + genuine staleness, never
  // on "is the push connection alive" (onopen fires once and stays true
  // forever, which was the original bug this sweep replaced).
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
    const t = setInterval(
      () => setSinceFetchS(Math.max(0, (performance.now() - fetchedAt.current) / 1000)),
      1000,
    );
    return () => clearInterval(t);
  }, []);

  // "updated Ns ago" — the base heartbeat phrase every consumer reads (task
  // 40c29b83 AC-5, re-anchored from LiveBar). NEVER a poll-interval reading
  // ("poll 5s") — it must read as time-since-last-fetch so a healthy SSE
  // connection (rare refetches) doesn't misread as a dead poller.
  const heartbeat = `updated ${Math.floor(sinceFetchS)}s ago`;

  return {
    data,
    managed: data?.managed_tasks ?? [],
    polled,
    observed: polled,
    error,
    fetchedAt,
    sinceFetchS,
    heartbeat,
    // Exposed so a consumer that ALSO wants its own supplementary push
    // trigger (e.g. LiveBar's D-6 EventSource, task 2d480b08) can nudge the
    // SAME shared load() rather than maintaining a second fetch/parse path.
    refresh: load,
  };
}
