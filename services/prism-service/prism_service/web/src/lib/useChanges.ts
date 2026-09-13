/**
 * useChanges — the ONE shared 1s poll of GET /api/changes per tab.
 *
 * THE BUG THIS EXISTS FOR (2026-09-13, live measurement): an idle Workflows
 * tab issued ~10 independent requests every 1-2s -- version, staleness,
 * workflows/live, conductor/state, consolidation/workers, tasks,
 * tasks/stranded, jobs, sse/work -- because every consumer polled its own
 * endpoint on its own fixed interval with no notion of whether the
 * underlying data had actually moved. 665+ requests in 7 minutes on one
 * idle tab, each a full payload.
 *
 * GET /api/changes (api/changes.py) answers a single bumped counter, backed
 * by the daemon's existing services/wakeups.py signal bus -- the same
 * mutation points that already wake standing workers. This hook polls it
 * ONCE per tab (module-scope subscriber set, exactly one setInterval, the
 * same sharing shape sharedStream.ts already uses for SSE) and every other
 * data query gates its own refetch on "did this counter move" through
 * usePolledResource instead of guessing on a private timer.
 *
 * Paused entirely while the tab is hidden, and re-synced immediately on
 * refocus/visibilitychange -- a backgrounded tab must cost nothing, and a
 * tab someone just switched back to must not wait up to a second to notice
 * it might be stale.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type ChangesSnapshot = { counter: number; at: number };

const POLL_MS = 1000;
// Self-healing backoff (measured live, 2026-09-13): an older backend that
// predates GET /api/changes answers 404 forever, and polling a 404 at 1Hz
// costs real requests for zero benefit -- measured adding ~60 req/min on an
// idle tab with nothing to show for it. After FAILURES_BEFORE_BACKOFF
// consecutive failures the shared poll backs off to BACKOFF_MS; the very
// next SUCCESS (the backend catches up, or comes back after a restart)
// resets it to the full 1Hz cadence immediately.
const FAILURES_BEFORE_BACKOFF = 5;
const BACKOFF_MS = 30_000;

type Listener = (snap: ChangesSnapshot) => void;

const listenersByProject = new Map<string, Set<Listener>>();
const latestByProject = new Map<string, ChangesSnapshot>();
let timer: ReturnType<typeof setTimeout> | null = null;
let started = false;
let consecutiveFailures = 0;

function projectsWithSubscribers(): string[] {
  const out: string[] = [];
  for (const [project, subs] of listenersByProject) if (subs.size > 0) out.push(project);
  return out;
}

async function pollOnce(): Promise<void> {
  if (typeof document !== "undefined" && document.hidden) return;
  const projects = projectsWithSubscribers();
  if (projects.length === 0) return;
  const results = await Promise.allSettled(
    projects.map((project) => {
      const qs = project ? `?project=${encodeURIComponent(project)}` : "";
      return api.get<ChangesSnapshot>(`/api/changes${qs}`).then((snap) => {
        latestByProject.set(project, snap);
        for (const l of listenersByProject.get(project) ?? []) l(snap);
      });
    }),
  );
  if (results.some((r) => r.status === "rejected")) {
    consecutiveFailures += 1;
  } else {
    consecutiveFailures = 0;
  }
}

/** The ONE live schedule this module ever runs: a single pending timer,
 * re-armed after each poll settles (never a second one stacked on top).
 * Same shared-timer discipline as sharedStream.ts, just self-rescheduling
 * instead of a fixed setInterval so the backoff above can stretch the gap
 * without a second concurrent timer ever existing. */
function scheduleNext(): void {
  const delay = consecutiveFailures >= FAILURES_BEFORE_BACKOFF ? BACKOFF_MS : POLL_MS;
  timer = setTimeout(runAndReschedule, delay);
}

function runAndReschedule(): void {
  void pollOnce().finally(scheduleNext);
}

function ensureStarted(): void {
  if (started || typeof window === "undefined") return;
  started = true;
  scheduleNext();
  const onVisible = () => {
    if (document.hidden) return;
    // Refocusing/returning to a backed-off tab shouldn't wait out the full
    // backoff window -- try immediately, which also resets it on success.
    if (timer) clearTimeout(timer);
    void pollOnce().finally(scheduleNext);
  };
  document.addEventListener("visibilitychange", onVisible);
  window.addEventListener("focus", onVisible);
}

/** Subscribe to the change counter for `project` ("" = every project).
 * Returns the live counter (0 until the first poll resolves) and `at`
 * (epoch seconds of that poll). */
export function useChanges(project = ""): ChangesSnapshot {
  const [snap, setSnap] = useState<ChangesSnapshot>(
    latestByProject.get(project) ?? { counter: 0, at: 0 },
  );

  useEffect(() => {
    ensureStarted();
    let subs = listenersByProject.get(project);
    if (!subs) {
      subs = new Set();
      listenersByProject.set(project, subs);
    }
    subs.add(setSnap);
    // A brand new project nobody has polled yet -- kick one off immediately
    // rather than waiting up to POLL_MS for the shared tick to notice it.
    if (!latestByProject.has(project)) pollOnce();
    return () => {
      subs?.delete(setSnap);
      if (subs && subs.size === 0) listenersByProject.delete(project);
    };
  }, [project]);

  return snap;
}

/** Test/diagnostic escape hatch -- never call from product code. */
export function _resetForTests(): void {
  if (timer) clearTimeout(timer);
  timer = null;
  started = false;
  consecutiveFailures = 0;
  listenersByProject.clear();
  latestByProject.clear();
}
