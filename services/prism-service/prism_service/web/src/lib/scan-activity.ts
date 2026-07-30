/**
 * useScanActivity — polls /api/jobs for in-flight + pending analyzer work.
 *
 * Backs two surfaces:
 *   * Sidebar Knowledge nav items pulse blue while anything is in flight.
 *   * LiveStatusStrip across the top of the page shows what's running
 *     and elapsed time.
 *
 * Polls fast (2s) while there is work, slow (10s) while idle, so the
 * UI feels live without DDoS'ing the API when nothing's happening.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type ScanJob = {
  id: string;
  project: string;
  analyzer: string;
  target_sha: string;
  state: "pending" | "in_progress" | "completed" | "failed" | "cancelled";
  enqueued_at: number;
  started_at: number;
  completed_at: number;
  attempts: number;
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

// SHARED SINGLETON (task c38ef597): Sidebar.tsx and LiveBar.tsx each call
// useScanActivity() independently, and both are mounted on every route — a
// per-hook useEffect/setInterval meant TWO parallel /api/jobs loops running
// at once, doubling the poll rate everywhere. This module-level state + one
// timer + a subscriber Set means however many components call the hook,
// there is exactly ONE poll loop. It starts when the first subscriber
// mounts and stops when the last one unmounts, so a page that renders
// neither Sidebar nor LiveBar (there is none today, but the pattern holds)
// costs nothing.
let _state: ScanActivity = IDLE;
const _subscribers = new Set<(s: ScanActivity) => void>();
let timer: ReturnType<typeof setTimeout> | null = null;

function _notify() {
  _subscribers.forEach((fn) => fn(_state));
}

// HIDDEN TAB = NO POLL (task c38ef597). This ran /api/jobs?limit=200 every
// 2s forever, including in background tabs nobody was looking at. Skipping
// the FETCH while hidden — rather than not rescheduling — is what keeps the
// loop alive, so becoming visible again resumes instead of freezing (the
// recorded misfire).
async function _load() {
  if (_subscribers.size === 0) return; // last subscriber left; stop for good
  if (typeof document !== "undefined" && document.hidden) {
    timer = setTimeout(_load, POLL_COLD_MS);
    return;
  }
  try {
    const r = await api.get<{ jobs: ScanJob[] }>("/api/jobs?limit=200");
    if (_subscribers.size === 0) return;
    const inProgress = r.jobs.filter((j) => j.state === "in_progress");
    const pending = r.jobs.filter((j) => j.state === "pending").length;
    const failed = r.jobs.filter((j) => j.state === "failed").length;
    const isActive = inProgress.length > 0 || pending > 0;
    _state = { isActive, inProgress, pending, failed };
    _notify();
    timer = setTimeout(_load, isActive ? POLL_HOT_MS : POLL_COLD_MS);
  } catch {
    if (_subscribers.size === 0) return;
    timer = setTimeout(_load, POLL_COLD_MS);
  }
}

// Come back to a fresh number the moment the tab is looked at again.
function _onVisible() {
  if (document.hidden || _subscribers.size === 0) return;
  if (timer !== null) clearTimeout(timer);
  _load();
}
if (typeof document !== "undefined") {
  document.addEventListener("visibilitychange", _onVisible);
}

export function useScanActivity(): ScanActivity {
  const [state, setState] = useState<ScanActivity>(_state);

  useEffect(() => {
    const wasEmpty = _subscribers.size === 0;
    _subscribers.add(setState);
    setState(_state); // pick up anything that changed between render and effect
    if (wasEmpty) _load(); // first subscriber starts the shared loop
    return () => {
      _subscribers.delete(setState);
      if (_subscribers.size === 0 && timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    };
  }, []);

  return state;
}
