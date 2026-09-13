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

type Listener = (snap: ChangesSnapshot) => void;

const listenersByProject = new Map<string, Set<Listener>>();
const latestByProject = new Map<string, ChangesSnapshot>();
let timer: ReturnType<typeof setInterval> | null = null;
let started = false;

function projectsWithSubscribers(): string[] {
  const out: string[] = [];
  for (const [project, subs] of listenersByProject) if (subs.size > 0) out.push(project);
  return out;
}

function pollOnce(): void {
  if (typeof document !== "undefined" && document.hidden) return;
  for (const project of projectsWithSubscribers()) {
    const qs = project ? `?project=${encodeURIComponent(project)}` : "";
    api
      .get<ChangesSnapshot>(`/api/changes${qs}`)
      .then((snap) => {
        latestByProject.set(project, snap);
        for (const l of listenersByProject.get(project) ?? []) l(snap);
      })
      .catch(() => { /* leave last-known snapshot; next tick retries */ });
  }
}

function ensureStarted(): void {
  if (started || typeof window === "undefined") return;
  started = true;
  timer = setInterval(pollOnce, POLL_MS);
  const onVisible = () => { if (!document.hidden) pollOnce(); };
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
  if (timer) clearInterval(timer);
  timer = null;
  started = false;
  listenersByProject.clear();
  latestByProject.clear();
}
