/**
 * sharedStream — ONE stream per URL per BROWSER, not one per component per tab.
 *
 * THE BUG THIS EXISTS FOR (task b835f639). Every consumer used to construct its
 * own EventSource. The global chrome alone opened three on EVERY page — the
 * /sse/live version watchdog, useConductorState's /sse/sessions, and a second
 * /sse/sessions that LiveBar opened for the identical URL — plus /sse/work on
 * Live. Browsers cap HTTP/1.1 at ~6 connections per origin, so two tabs reached
 * the cap and a third tab could not fetch even its own HTML document: measured
 * >60s to load /sessions with three tabs open, 62ms with them closed, while the
 * server served that same document in 2.9ms throughout. The server was never
 * slow; PRISM was starving its own connection pool.
 *
 * TWO LAYERS OF SHARING:
 *
 *  1. Per URL, within a tab. `entries` keys every stream by URL and ref-counts
 *     its subscribers, so N components watching one URL share ONE connection
 *     and it is torn down only when the LAST subscriber leaves.
 *
 *  2. Per browser, across tabs. Web Locks elects a single leader tab; only the
 *     leader constructs EventSources, and it republishes every frame over a
 *     BroadcastChannel. Follower tabs hold ZERO connections and stay live off
 *     the channel. Web Locks rather than a hand-rolled heartbeat election
 *     because the browser releases the lock automatically when the leader tab
 *     dies, so failover never has to guess whether a silent leader is dead.
 *
 * Followers announce the URLs they need, so a stream only a follower wants
 * (e.g. /sse/work when just that tab is on Live) is still opened by the leader.
 *
 * DEGRADE PATH: where Web Locks or BroadcastChannel is unavailable, every tab
 * acts as its own leader. That is exactly today's behaviour, still deduped per
 * URL within the tab — no page loses liveness on an older browser.
 */

type Frame = (data: string) => void;
type Health = (healthy: boolean) => void;

type Entry = {
  subs: Set<Frame>;
  healthSubs: Set<Health>;
  es: EventSource | null;
  healthy: boolean;
};

const CHANNEL_NAME = "prism-sse";
const LOCK_NAME = "prism-sse-leader";

const entries = new Map<string, Entry>();

/** URLs other tabs have asked for. Kept even while we are a follower, so a tab
 *  promoted to leader opens what the rest of the browser is already waiting on. */
const remoteWanted = new Set<string>();

let channel: BroadcastChannel | null = null;
let isLeader = false;
let started = false;

type Wire =
  | { t: "frame"; url: string; data: string }
  | { t: "health"; url: string; healthy: boolean }
  | { t: "need"; url: string }
  | { t: "census" };

function post(msg: Wire): void {
  channel?.postMessage(msg);
}

function entryFor(url: string): Entry {
  let e = entries.get(url);
  if (!e) {
    e = { subs: new Set(), healthSubs: new Set(), es: null, healthy: false };
    entries.set(url, e);
  }
  return e;
}

function setHealth(url: string, healthy: boolean): void {
  const e = entries.get(url);
  if (!e || e.healthy === healthy) return;
  e.healthy = healthy;
  for (const h of e.healthSubs) h(healthy);
}

function deliver(url: string, data: string): void {
  const e = entries.get(url);
  if (!e) return;
  for (const s of e.subs) s(data);
}

/** Open the real connection. ONLY the leader ever reaches this. */
function openStream(url: string): void {
  const e = entryFor(url);
  if (e.es) return;
  try {
    const es = new EventSource(url);
    e.es = es;
    es.onmessage = (ev: MessageEvent<string>) => {
      setHealth(url, true);
      post({ t: "health", url, healthy: true });
      deliver(url, ev.data);
      post({ t: "frame", url, data: ev.data });
    };
    es.onerror = () => {
      setHealth(url, false);
      post({ t: "health", url, healthy: false });
    };
  } catch {
    // EventSource unavailable — consumers keep their own fallbacks (version.ts
    // polls /api/version while unhealthy), so this must never throw upward.
    e.es = null;
  }
}

/** Anything this browser wants: our own subscribers plus other tabs' requests. */
function openEverythingWanted(): void {
  for (const [url, e] of entries) if (e.subs.size > 0) openStream(url);
  for (const url of remoteWanted) openStream(url);
}

function onWire(msg: Wire): void {
  if (msg.t === "frame") {
    if (!isLeader) deliver(msg.url, msg.data);
    return;
  }
  if (msg.t === "health") {
    if (!isLeader) setHealth(msg.url, msg.healthy);
    return;
  }
  if (msg.t === "need") {
    remoteWanted.add(msg.url);
    if (isLeader) openStream(msg.url);
    return;
  }
  // A new leader is taking census — re-announce what this tab needs.
  for (const [url, e] of entries) if (e.subs.size > 0) post({ t: "need", url });
}

function start(): void {
  if (started || typeof window === "undefined") return;
  started = true;

  try {
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.onmessage = (ev: MessageEvent<Wire>) => onWire(ev.data);
  } catch {
    channel = null;
  }

  const hasLocks = typeof navigator !== "undefined" && !!navigator.locks;
  if (!channel || !hasLocks) {
    // Degrade: no cross-tab coordination available, so this tab serves itself.
    isLeader = true;
    openEverythingWanted();
    return;
  }

  // The tab holding this lock is THE leader. The promise never resolves, so the
  // lock is held for the tab's lifetime and released by the browser on death —
  // at which point a waiting tab is granted it and takes over.
  void navigator.locks.request(LOCK_NAME, { mode: "exclusive" }, () => {
    isLeader = true;
    post({ t: "census" });
    openEverythingWanted();
    return new Promise<never>(() => {});
  });
}

/**
 * Subscribe to `url`. Returns the unsubscribe.
 *
 * `onHealth` reports whether frames are actually flowing; lib/version.ts gates
 * its /api/version fallback poll on it (D-6) now that it owns no EventSource of
 * its own and cannot read `es.onerror` directly.
 */
export function subscribeStream(
  url: string,
  onMessage: Frame,
  onHealth?: Health,
): () => void {
  start();
  const e = entryFor(url);
  e.subs.add(onMessage);
  if (onHealth) {
    e.healthSubs.add(onHealth);
    onHealth(e.healthy);
  }

  if (isLeader) openStream(url);
  else post({ t: "need", url });

  return () => {
    e.subs.delete(onMessage);
    if (onHealth) e.healthSubs.delete(onHealth);
    // Tear down ONLY when the last subscriber leaves — closing while another
    // component is still attached would break the sharing this module exists
    // for, and reopening it would cost the very connection we just saved.
    if (e.subs.size === 0) {
      e.es?.close();
      e.es = null;
      e.healthy = false;
      entries.delete(url);
    }
  };
}
