/**
 * useVersion — reads the live service version from /api/version.
 *
 * The string is owned by app/__version__.py.PRISM_VERSION; the
 * Sidebar footer and the Settings page both consume this hook so
 * the version label can't drift between them. Result is cached
 * module-scope after the first fetch — version doesn't change
 * mid-session, so re-fetching on every mount is wasted.
 *
 * On first use we also open an SSE stream to /sse/live. The server
 * emits its current version on connect; EventSource auto-reconnects
 * when the backend restarts (Watchtower swap), so the post-swap
 * reconnect surfaces a new version → we force-reload the page so
 * the user picks up the new bundle without having to hard-refresh.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type ServiceVersion = { version: string; notes: string; dev_mode?: boolean };

let cached: ServiceVersion | null = null;
let inflight: Promise<ServiceVersion> | null = null;
let watchdogStarted = false;

function startLiveWatchdog() {
  if (watchdogStarted || typeof window === "undefined") return;
  watchdogStarted = true;
  let initial: string | null = null;
  const onVersion = (v: string | undefined) => {
    if (!v) return;
    if (initial === null) initial = v;
    else if (v !== initial) window.location.reload();
  };
  // Fast path: SSE reconnect after a backend swap surfaces the new version.
  try {
    const es = new EventSource("/sse/live");
    es.onmessage = (ev) => {
      try { onVersion((JSON.parse(ev.data) as { version?: string }).version); }
      catch { /* ignore malformed payloads */ }
    };
  } catch { /* EventSource unavailable — polling still covers it */ }
  // Robust fallback: poll every 15s so a throttled/suspended tab whose SSE
  // stalled still picks up a new build without a manual hard-refresh. Also
  // re-checks on refocus, when a backgrounded tab wakes up.
  const poll = () => {
    fetch("/api/version", { cache: "no-store" })
      .then((r) => r.json())
      .then((r: { version?: string }) => onVersion(r.version))
      .catch(() => {});
  };
  setInterval(poll, 15000);
  window.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") poll();
  });
}

export function useVersion(): ServiceVersion | null {
  const [v, setV] = useState<ServiceVersion | null>(cached);
  useEffect(() => {
    startLiveWatchdog();
    if (cached) return;
    if (!inflight) {
      inflight = api.get<ServiceVersion>("/api/version")
        .then((r) => { cached = r; return r; })
        .finally(() => { inflight = null; });
    }
    inflight.then((r) => setV(r)).catch(() => {});
  }, []);
  return v;
}
