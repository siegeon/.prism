/**
 * Shared /api/jobs poller — ONE module-level loop (task e8fc073b), not
 * one per mount. Every surface that used to run its own timer
 * (Sidebar, LiveBar, LiveStatusStrip, SettingsPage's JobsPanel +
 * ScanProgress) now subscribes to this single store, following the
 * subscriber-set pattern already used by `lib/project.ts`.
 *
 * useScanActivity() — derived active/pending/failed summary. Backs:
 *   * Sidebar Knowledge nav items pulse blue while anything is in flight.
 *   * LiveStatusStrip across the top of the page shows what's running
 *     and elapsed time.
 *
 * useJobs() — the raw job rows, for surfaces (Settings) that need to
 * filter/scope them themselves.
 *
 * Polls fast (2s) while there is work, slow (10s) while idle, so the
 * UI feels live without DDoS'ing the API when nothing's happening. An
 * in-flight guard (set before the await, cleared in `finally`) means a
 * scheduled tick never stacks a second request on top of one still
 * outstanding.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type ScanJob = {
  id: string;
  project: string;
  analyzer: string;
  target_sha: string;
  scope_hash: string;
  state: "pending" | "in_progress" | "completed" | "failed" | "cancelled";
  enqueued_at: number;
  started_at: number;
  completed_at: number;
  attempts: number;
  error: string;
  result_path: string;
};

export type ScanActivity = {
  isActive: boolean;
  inProgress: ScanJob[];
  pending: number;
  failed: number;
};

const IDLE: ScanActivity = {
  isActive: false, inProgress: [], pending: 0, failed: 0,
};

const POLL_HOT_MS = 2000;
const POLL_COLD_MS = 10000;

type JobsState = {
  jobs: ScanJob[];
  loaded: boolean;
  lastLoaded: number | null;
};

const EMPTY_STATE: JobsState = { jobs: [], loaded: false, lastLoaded: null };

// Module-level (process-wide, one per browser tab) store. Every
// subscriber shares this one poll loop and one in-flight request.
let jobsState: JobsState = EMPTY_STATE;
const subscribers = new Set<(s: JobsState) => void>();
let timer: ReturnType<typeof setTimeout> | null = null;
let inFlight = false;

function _notify() {
  subscribers.forEach((fn) => fn(jobsState));
}

function _isHot(jobs: ScanJob[]): boolean {
  return jobs.some((j) => j.state === "pending" || j.state === "in_progress");
}

function _scheduleNext(hot: boolean) {
  if (timer !== null) clearTimeout(timer);
  timer = setTimeout(_load, hot ? POLL_HOT_MS : POLL_COLD_MS);
}

// HIDDEN TAB = NO POLL (task c38ef597). Skipping the FETCH while hidden
// — rather than not rescheduling — is what keeps the loop alive, so
// becoming visible again resumes instead of freezing (the recorded
// misfire).
async function _load(): Promise<void> {
  if (subscribers.size === 0) return; // last unsubscribe already stopped us
  if (typeof document !== "undefined" && document.hidden) {
    _scheduleNext(false);
    return;
  }
  if (inFlight) {
    // A tick landed while a request is still outstanding — reschedule
    // WITHOUT issuing a second request (AC-6).
    _scheduleNext(_isHot(jobsState.jobs));
    return;
  }
  inFlight = true;
  try {
    const r = await api.get<{ jobs: ScanJob[] }>("/api/jobs?limit=200");
    jobsState = { jobs: r.jobs, loaded: true, lastLoaded: Date.now() };
    _notify();
    _scheduleNext(_isHot(r.jobs));
  } catch {
    _scheduleNext(false);
  } finally {
    inFlight = false;
  }
}

// Come back to a fresh number the moment the tab is looked at again.
function _onVisible() {
  if (typeof document === "undefined" || document.hidden) return;
  if (timer !== null) clearTimeout(timer);
  _load();
}

function _subscribe(fn: (s: JobsState) => void): () => void {
  const first = subscribers.size === 0;
  subscribers.add(fn);
  if (first) {
    document.addEventListener("visibilitychange", _onVisible);
    _load();
  }
  return () => {
    subscribers.delete(fn);
    if (subscribers.size === 0) {
      document.removeEventListener("visibilitychange", _onVisible);
      if (timer !== null) clearTimeout(timer);
      timer = null;
    }
  };
}

function _deriveActivity(s: JobsState): ScanActivity {
  if (!s.loaded) return IDLE;
  const inProgress = s.jobs.filter((j) => j.state === "in_progress");
  const pending = s.jobs.filter((j) => j.state === "pending").length;
  const failed = s.jobs.filter((j) => j.state === "failed").length;
  return { isActive: inProgress.length > 0 || pending > 0, inProgress, pending, failed };
}

export function useScanActivity(): ScanActivity {
  const [s, setS] = useState<JobsState>(jobsState);
  useEffect(() => _subscribe(setS), []);
  return _deriveActivity(s);
}

/** Raw job rows for surfaces (Settings) that filter/scope them
 * themselves, riding the SAME shared poll loop as useScanActivity(). */
export function useJobs(): {
  jobs: ScanJob[];
  loaded: boolean;
  lastLoaded: number | null;
  refresh: () => void;
} {
  const [s, setS] = useState<JobsState>(jobsState);
  useEffect(() => _subscribe(setS), []);
  return {
    jobs: s.jobs,
    loaded: s.loaded,
    lastLoaded: s.lastLoaded,
    refresh: () => {
      if (timer !== null) clearTimeout(timer);
      _load();
    },
  };
}
