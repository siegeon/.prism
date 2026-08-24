/**
 * waitForServerThenReload — the ONE safe way to reload this tab after the
 * backend has been unreachable.
 *
 * Owner, live (2026-08-24), watching a dev restart through remote assist:
 * "this was a native app that can exist without the server running it, and
 * it should NEVER go white — it should have the banner letting the
 * customer know it's updating." Before this fix, App.tsx's lazyRoute()
 * caught a failed chunk import (e.g. mid-restart) and called
 * `window.location.reload()` immediately, with no check that the server
 * would actually answer the reload. If the restart was still in progress,
 * that reload navigation itself failed to connect — the browser's own
 * blank/error page for a failed top-level navigation, which no amount of
 * React error-boundary or Suspense-fallback code can intercept, because by
 * that point the SPA has already been torn down.
 *
 * The fix: never call reload() until a real probe succeeds. Poll
 * `/api/version` with a short timeout and linear-ish backoff; only the
 * FIRST successful response triggers the reload. While polling, the
 * global `reconnecting` flag drives a persistent banner (see
 * ReconnectBanner.tsx) so the tab always shows something, never a blank
 * screen — the current page's own JS keeps running the whole time, since
 * no navigation is attempted until we know it will land.
 */

type Listener = (reconnecting: boolean) => void;

let reconnecting = false;
let pollTimer: ReturnType<typeof setTimeout> | null = null;
const listeners = new Set<Listener>();

function setReconnecting(value: boolean) {
  if (reconnecting === value) return;
  reconnecting = value;
  listeners.forEach((l) => l(reconnecting));
}

export function isReconnecting(): boolean {
  return reconnecting;
}

export function subscribeReconnecting(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

// Capped backoff: fast enough to feel responsive once the server is back
// (a dev restart is usually a few seconds), never so tight it hammers a
// server that's still coming up.
const POLL_DELAYS_MS = [500, 500, 1000, 1000, 2000, 2000, 3000];
const POLL_DELAY_MAX_MS = 3000;

function probeOnce(): Promise<boolean> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 4000);
  return fetch("/api/version", { cache: "no-store", signal: controller.signal })
    .then((r) => r.ok)
    .catch(() => false)
    .finally(() => clearTimeout(timeout));
}

/** Start polling for the server to come back, then reload — never before.
 * Safe to call more than once (e.g. two lazy routes failing back to back);
 * only the first call starts the poll loop. */
export function waitForServerThenReload(): void {
  setReconnecting(true);
  if (pollTimer !== null) return; // already polling
  let attempt = 0;
  const tick = () => {
    probeOnce().then((ok) => {
      if (ok) {
        window.location.reload();
        return; // reload navigates away; no further scheduling needed
      }
      const delay = POLL_DELAYS_MS[Math.min(attempt, POLL_DELAYS_MS.length - 1)] ?? POLL_DELAY_MAX_MS;
      attempt += 1;
      pollTimer = setTimeout(tick, delay);
    });
  };
  tick();
}
