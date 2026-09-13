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
 * Task fix/lasttimers (owner 2026-09-13, "why are you hammering the
 * server with polling rather than updating with streaming"): this used
 * to run its own bare setTimeout loop — fast (2s) while anything was in
 * flight, slow (10s) while idle — so an idle tab still refetched forever.
 * It now rides the SAME event-driven gate every other resource on this
 * page sits behind (lib/usePolledResource.ts's usePolledResource):
 * refetch on a real GET /sse/changes "jobs" frame (backend emits it from
 * the understand-anything queue on enqueue/claim/complete/fail/cancel —
 * see inference/queue.py's wakeups.signal call sites), on window focus/
 * visibility, or as a 60s reconnect safety net while the stream itself
 * looks unhealthy — never a routine poll while it's healthy, and never
 * at all while the tab is hidden.
 */
import { useState, useEffect } from "react";
import { usePolledResource } from "@/lib/usePolledResource";

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

const JOBS_URL = "/api/jobs?limit=200";
const JOBS_KINDS = ["jobs"];

function _deriveActivity(jobs: ScanJob[], loaded: boolean): ScanActivity {
  if (!loaded) return IDLE;
  const inProgress = jobs.filter((j) => j.state === "in_progress");
  const pending = jobs.filter((j) => j.state === "pending").length;
  const failed = jobs.filter((j) => j.state === "failed").length;
  return { isActive: inProgress.length > 0 || pending > 0, inProgress, pending, failed };
}

export function useScanActivity(): ScanActivity {
  const { data, polled } = usePolledResource<{ jobs: ScanJob[] }>(JOBS_URL, "", JOBS_KINDS);
  return _deriveActivity(data?.jobs ?? [], polled);
}

/** Raw job rows for surfaces (Settings) that filter/scope them
 * themselves, riding the SAME shared poll loop as useScanActivity(). */
export function useJobs(): {
  jobs: ScanJob[];
  loaded: boolean;
  lastLoaded: number | null;
  refresh: () => void;
} {
  const { data, polled, refresh } = usePolledResource<{ jobs: ScanJob[] }>(JOBS_URL, "", JOBS_KINDS);
  // usePolledResource doesn't itself expose "when did the payload last
  // change" — track it locally off the data reference so JobsPanel's
  // "Updated Ns ago" label still means something.
  const [lastLoaded, setLastLoaded] = useState<number | null>(null);
  useEffect(() => {
    if (data !== null) setLastLoaded(Date.now());
  }, [data]);
  return {
    jobs: data?.jobs ?? [],
    loaded: polled,
    lastLoaded,
    refresh,
  };
}
