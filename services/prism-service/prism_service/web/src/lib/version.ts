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
 * 842248bd: the full changelog is ~262 KB, and the mount fetch plus the
 * refetches below only ever read `.version` — shipping the whole
 * changelog on every one of those calls was pure waste). Call
 * `useVersionNotes()` for the one place that genuinely wants the full
 * string (Sidebar's tooltip); it fetches `?notes=true` once, lazily,
 * cached module-scope same as `cached` above.
 *
 * Task fix/lasttimers (owner 2026-09-13, live idle-tab measurement: 22 of
 * 37 requests in an idle, focused 33s window were GET /api/version): this
 * file used to run two fixed timers of its own — a 2s dev-bundle poll and
 * a 15s SSE-unhealthy fallback poll — so an idle tab hammered /api/version
 * on a clock even though nothing had changed. Neither timer remains.
 * Both watchers now refetch only on a real `deployed` wakeups signal
 * (backend emits it from deploy_worker.confirm_pending_deploy on a
 * confirmed restart, and once from main.py's lifespan boot — see
 * services/wakeups.py's call sites) delivered over the SAME /sse/changes
 * stream every other push-driven refetch in this app rides
 * (lib/useChanges.ts's subscribeToChangeKind), plus a window `focus`
 * recheck for a tab that was asleep through the event entirely.
 */
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { subscribeStream } from "@/lib/sharedStream";
import { subscribeToChangeKind } from "@/lib/useChanges";

export type ServiceVersion = {
  version: string;
  notes: string;
  dev_mode?: boolean;
  web_build?: string;
};

let cached: ServiceVersion | null = null;
let inflight: Promise<ServiceVersion> | null = null;
let watchdogStarted = false;
let devBundleWatchStarted = false;

// Same anti-loop shape as main.tsx's RELOAD_FLAG/COOLDOWN_MS guard around
// recoverFromStaleChunk: a daemon that flaps between versions mid-bounce (the
// old version briefly answering again before the new one settles) must not
// reload-loop the tab. Both call sites below pass the SAME literal key so a
// bounce that trips the SSE watchdog and the dev-mode poll in the same
// window still only reloads once.
function guardedReload(key: string): void {
  const cooldownMs = 30_000;
  try {
    const last = Number(sessionStorage.getItem(key) || 0);
    if (last && Date.now() - last < cooldownMs) return;
    sessionStorage.setItem(key, String(Date.now()));
  } catch { /* private mode: fall through to a single reload attempt */ }
  window.location.reload();
}

function startDevBundleWatch(initialBuild: string | undefined) {
  if (devBundleWatchStarted || !initialBuild || typeof window === "undefined") return;
  devBundleWatchStarted = true;
  const poll = () => {
    fetch("/api/version", { cache: "no-store" })
      .then((r) => r.json())
      .then((r: ServiceVersion) => {
        if (r.web_build && r.web_build !== initialBuild) guardedReload("prism:version-reload");
      })
      .catch(() => {});
  };
  // No fixed-interval reschedule (fix/lasttimers) — a `deployed` wakeups
  // frame IS the "a new build might be live" signal; a window focus also
  // rechecks for a tab that missed the event while backgrounded/asleep.
  subscribeToChangeKind("deployed", poll);
  window.addEventListener("focus", poll);
}

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
    else if (v !== initial) guardedReload("prism:version-reload");
  };
  // Fast path: SSE reconnect after a backend swap surfaces the new version.
  // Subscribes through lib/sharedStream (task b835f639) so this watchdog costs
  // no connection of its own — it used to hold one in EVERY tab, on every page.
  // Health now arrives via onHealth instead of a locally-owned es.onerror.
  subscribeStream(
    "/sse/live",
    (data) => {
      try { onVersion((JSON.parse(data) as { version?: string }).version); }
      catch { /* ignore malformed payloads */ }
    },
    (healthy) => { sseHealthy = healthy; },
  );
  // Robust fallback, but ONLY while the SSE channel above looks
  // unhealthy — /sse/live already pushes the version on connect and
  // reconnect, so this still covers a throttled/suspended tab whose SSE
  // stalled, without running a redundant fetch on every healthy tab.
  // Task fix/lasttimers: no fixed-interval reschedule any more — the
  // trigger is a real `deployed` wakeups frame (a confirmed daemon
  // restart) delivered over /sse/changes, plus a visibilitychange
  // recheck for a tab that wakes up having missed the event entirely.
  const poll = () => {
    if (sseHealthy) return;
    fetch("/api/version", { cache: "no-store" })
      .then((r) => r.json())
      .then((r: { version?: string }) => onVersion(r.version))
      .catch(() => {});
  };
  subscribeToChangeKind("deployed", poll);
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
        .then((r) => {
          cached = r;
          if (r.dev_mode) startDevBundleWatch(r.web_build);
          return r;
        })
        .finally(() => { inflight = null; });
    }
    inflight.then((r) => setV(r)).catch(() => {});
  }, []);
  return v;
}

let notesCached: string | null = null;
let notesInflight: Promise<string> | null = null;

/** Fetches the full changelog via the explicit `?notes=true` opt-in (task
 * 842248bd) — never ridden on the lean default `useVersion()` path or
 * either watcher above. Task d5465a25: this used to fire inside a bare mount
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
