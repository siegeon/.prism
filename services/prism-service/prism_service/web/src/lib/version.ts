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
 *
 * `/api/version`'s default response deliberately OMITS `notes` (task
 * 842248bd: the full changelog is ~262 KB, and this hook's mount fetch plus
 * the 15s poll below only ever read `.version` — shipping the whole
 * changelog on every one of those calls was pure waste). Call
 * `useVersionNotes()` for the one place that genuinely wants the full
 * string (Sidebar's tooltip); it fetches `?notes=true` once, lazily,
 * cached module-scope same as `cached` above.
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
  // D-6 (task 2d480b08): the 15s fallback poll below only fires while this
  // reads false — an always-on poll defeats the whole payload-scope fix.
  let sseHealthy = false;
  const onVersion = (v: string | undefined) => {
    if (!v) return;
    if (initial === null) initial = v;
    else if (v !== initial) window.location.reload();
  };
  // Fast path: SSE reconnect after a backend swap surfaces the new version.
  try {
    const es = new EventSource("/sse/live");
    es.onmessage = (ev) => {
      sseHealthy = true;
      try { onVersion((JSON.parse(ev.data) as { version?: string }).version); }
      catch { /* ignore malformed payloads */ }
    };
    es.onerror = () => { sseHealthy = false; };
  } catch { /* EventSource unavailable — polling still covers it */ }
  // Robust fallback: poll every 15s, but ONLY while the SSE channel above
  // looks unhealthy — /sse/live already pushes the version on connect and
  // reconnect, so this still covers a throttled/suspended tab whose SSE
  // stalled, without running a redundant fetch on every healthy tab. Also
  // re-checks on refocus, when a backgrounded tab wakes up.
  const poll = () => {
    if (sseHealthy) return;
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

let notesCached: string | null = null;
let notesInflight: Promise<string> | null = null;

/** Fetches the full changelog via the explicit `?notes=true` opt-in (task
 * 842248bd) — never ridden on the lean default `useVersion()` path or the
 * 15s poll. Task d5465a25: this used to fire inside a bare mount
 * `useEffect`, so every Sidebar mount downloaded the whole ~262 KB
 * changelog whether or not the user ever hovered the tooltip. There is no
 * auto-fetch now — the caller must invoke the returned `ensureLoaded()`
 * (Sidebar wires it to the footer's hover/focus). Repeated calls after the
 * first are free: `notesCached`/`notesInflight` guard exactly like
 * `useVersion()`'s cache above. */
export function useVersionNotes(): { notes: string; ensureLoaded: () => void } {
  const [n, setN] = useState<string>(notesCached ?? "");
  const ensureLoaded = () => {
    if (notesCached !== null) { setN(notesCached); return; }
    if (!notesInflight) {
      notesInflight = api.get<ServiceVersion>("/api/version?notes=true")
        .then((r) => { notesCached = r.notes ?? ""; return notesCached; })
        .finally(() => { notesInflight = null; });
    }
    notesInflight.then((r) => setN(r)).catch(() => {});
  };
  return { notes: n, ensureLoaded };
}
