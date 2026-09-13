/**
 * usePolledResource — the generic refetch gate every page-level data query
 * should sit behind, instead of its own private `setInterval` fetch loop
 * OR (the earlier version of this file) a shared change-counter poll.
 *
 * Policy (owner 2026-09-13, two rounds: first "fan out sub agents, fix
 * this" on the idle-tab request storm, then "why are you hammering the
 * server with polling rather than updating with streaming" once the
 * counter-poll fix was itself measured still polling):
 *
 *   - refetch when a matching-kind event arrives over GET /sse/changes
 *     (lib/useChanges.ts's useChanges — a real push, not a poll),
 *   - refetch on window focus or document visibilitychange,
 *   - a 60s floor fires ONLY as a reconnect safety net -- while the SSE
 *     stream itself looks unhealthy (no frames flowing) -- never as a
 *     routine refetch trigger while the stream is fine,
 *   - never fetch at all while the tab is hidden,
 *   - cache the last good payload per URL at MODULE scope, so navigating
 *     back to an already-fetched resource paints instantly from cache
 *     (stale-while-revalidate) instead of a fresh blank-loading flash.
 *
 * `kinds` narrows which /sse/changes event kinds trigger a refetch (e.g.
 * ["task_changed"] for a task list, ["activity"] for the System Activity
 * panel). Omit it to refetch on ANY event kind (the safe default for a
 * caller that has not been mapped to a specific kind yet).
 *
 * This does not replace lib/useConductorState.ts or lib/sharedStream.ts --
 * those already implement the same policy bespoke for their one endpoint.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useChanges } from "@/lib/useChanges";

const FLOOR_MS = 60_000;

type CacheEntry<T> = { data: T; at: number };
const cache = new Map<string, CacheEntry<unknown>>();

export type PolledResource<T> = {
  data: T | null;
  /** True once at least one fetch has resolved (success or failure) --
   * lets a consumer tell "no fetch yet" apart from "fetched and empty". */
  polled: boolean;
  error: boolean;
  /** Force an immediate refetch regardless of the event/floor gate. */
  refresh: () => void;
};

/**
 * Poll `url` under the policy above. `project` selects which
 * GET /sse/changes stream gates this query; `kinds` narrows which event
 * kinds on that stream trigger a refetch (omit for "any kind").
 */
export function usePolledResource<T>(
  url: string | null, project = "", kinds?: string[],
): PolledResource<T> {
  const cached = url ? (cache.get(url) as CacheEntry<T> | undefined) : undefined;
  const [data, setData] = useState<T | null>(cached?.data ?? null);
  const [polled, setPolled] = useState(cached !== undefined);
  const [error, setError] = useState(false);
  const { counter, last, healthy } = useChanges(project);
  const lastCounterRef = useRef<number | null>(null);
  const lastFetchAtRef = useRef(0);

  const load = useCallback(() => {
    if (!url) return;
    lastFetchAtRef.current = performance.now();
    api
      .get<T>(url)
      .then((d) => {
        cache.set(url, { data: d, at: Date.now() });
        setData(d);
        setError(false);
      })
      .catch(() => setError(true))
      .finally(() => setPolled(true));
  }, [url]);

  // Seed from cache instantly on mount / URL change, then always kick a
  // background revalidation -- a cached payload may already be stale by
  // the time this component remounts.
  useEffect(() => {
    if (!url) return;
    const c = cache.get(url) as CacheEntry<T> | undefined;
    if (c) {
      setData(c.data);
      setPolled(true);
    }
    load();
  }, [url, load]);

  // Event-driven refetch: a NEW /sse/changes frame (by counter, so the
  // same event never double-fires this) of a kind this query cares about.
  useEffect(() => {
    if (!url || !last) return;
    if (lastCounterRef.current === counter) return;
    lastCounterRef.current = counter;
    if (kinds && kinds.length > 0 && !kinds.includes(last.kind)) return;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [counter, last, url, load]);

  // Focus/visibility (unconditional refetch trigger) + a 60s floor that
  // fires ONLY while the stream looks unhealthy (reconnect safety net,
  // never a routine poll substitute) -- and never while hidden.
  useEffect(() => {
    if (!url) return;
    const onVisible = () => { if (!document.hidden) load(); };
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    const floor = setInterval(() => {
      if (document.hidden) return;
      if (healthy) return;
      if (performance.now() - lastFetchAtRef.current >= FLOOR_MS) load();
    }, 5000);
    return () => {
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(floor);
    };
  }, [url, load, healthy]);

  return { data, polled, error, refresh: load };
}

/**
 * Same policy as usePolledResource, for a page's own composite `load()`
 * that fetches more than one URL at once (Promise.all(...)) and so
 * cannot be expressed as a single `usePolledResource<T>(url)` call.
 */
export function usePolledEffect(load: () => void, project = "", kinds?: string[]): void {
  const { counter, last, healthy } = useChanges(project);
  const lastCounterRef = useRef<number | null>(null);
  const lastRunAtRef = useRef(0);
  const loadRef = useRef(load);
  loadRef.current = load;

  const run = useCallback(() => {
    lastRunAtRef.current = performance.now();
    loadRef.current();
  }, []);

  useEffect(() => { run(); }, [run]);

  useEffect(() => {
    if (!last) return;
    if (lastCounterRef.current === counter) return;
    lastCounterRef.current = counter;
    if (kinds && kinds.length > 0 && !kinds.includes(last.kind)) return;
    run();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [counter, last, run]);

  useEffect(() => {
    const onVisible = () => { if (!document.hidden) run(); };
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    const floor = setInterval(() => {
      if (document.hidden) return;
      if (healthy) return;
      if (performance.now() - lastRunAtRef.current >= FLOOR_MS) run();
    }, 5000);
    return () => {
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
      clearInterval(floor);
    };
  }, [run, healthy]);
}
