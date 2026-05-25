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

export type BuildMode = "dev" | "release" | "docker" | string;
export type ServiceVersion = {
  version: string;
  notes: string;
  // v5.3.21 — populated from /api/service-info on first fetch so the
  // footer can badge "dev" / "release" / "docker" alongside the version
  // number. Empty until that secondary call lands.
  build_mode?: BuildMode;
  shell_version?: string;
};

let cached: ServiceVersion | null = null;
let inflight: Promise<ServiceVersion> | null = null;
let watchdogStarted = false;

function startLiveWatchdog() {
  if (watchdogStarted || typeof window === "undefined") return;
  watchdogStarted = true;
  let initial: string | null = null;
  const es = new EventSource("/sse/live");
  es.onmessage = (ev) => {
    try {
      const v = (JSON.parse(ev.data) as { version?: string }).version;
      if (!v) return;
      if (initial === null) {
        initial = v;
      } else if (v !== initial) {
        window.location.reload();
      }
    } catch {
      /* ignore malformed payloads */
    }
  };
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
    // Secondary fetch for build_mode; not on the critical path so
    // a failure is harmless — the footer just won't show the badge.
    api.get<{ build_mode?: BuildMode; shell_version?: string }>("/api/service-info")
      .then((s) => {
        if (cached) {
          cached.build_mode = s.build_mode;
          cached.shell_version = s.shell_version;
        }
        setV((prev) => prev ? { ...prev, build_mode: s.build_mode, shell_version: s.shell_version } : prev);
      })
      .catch(() => {});
  }, []);
  return v;
}
