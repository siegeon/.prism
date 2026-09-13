/**
 * usePolledResource — the generic refetch gate every page-level data query
 * should sit behind, instead of its own private `setInterval` fetch loop.
 *
 * Policy (owner 2026-09-13, "fan out sub agents, fix this" -- the idle
 * Workflows tab measured 665+ requests in 7 minutes across ~10 endpoints,
 * each an independent fixed-interval poll with no notion of whether
 * anything had changed):
 *
 *   - refetch when the shared change counter (useChanges) moves,
 *   - refetch on window focus (a backgrounded tab someone returns to must
 *     not show minutes-old data),
 *   - otherwise refetch at a 30s FLOOR so a query self-heals even from a
 *     signal this project's wakeups bus never learned about,
 *   - never fetch at all while the tab is hidden,
 *   - cache the last good payload per URL at MODULE scope, so navigating
 *     back to an already-fetched resource paints instantly from cache
 *     (stale-while-revalidate) instead of a fresh blank-loading flash, and
 *     revalidates in the background exactly like any other trigger above.
 *
 * This does not replace lib/useConductorState.ts or lib/sharedStream.ts --
 * those already implement the same policy bespoke for their one endpoint
 * (SSE push + coalescing + staleness sweep). Use THIS for any other GET
 * that would otherwise reach for a bare `setInterval`.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useChanges } from "@/lib/useChanges";

const FLOOR_MS = 30_000;

type CacheEntry<T> = { data: T; at: number };
const cache = new Map<string, CacheEntry<unknown>>();

export type PolledResource<T> = {
  data: T | null;
  /** True once at least one fetch has resolved (success or failure) --
   * lets a consumer tell "no fetch yet" apart from "fetched and empty". */
  polled: boolean;
  error: boolean;
  /** Force an immediate refetch regardless of the counter/floor. */
  refresh: () => void;
};

/**
 * Poll `url` under the policy above. `project` selects which change-counter
 * scope gates this query (pass "" to watch every project's counter, the
 * same default useChanges uses).
 */
export function usePolledResource<T>(url: string | null, project = ""): PolledResource<T> {
  const cached = url ? (cache.get(url) as CacheEntry<T> | undefined) : undefined;
  const [data, setData] = useState<T | null>(cached?.data ?? null);
  const [polled, setPolled] = useState(cached !== undefined);
  const [error, setError] = useState(false);
  const { counter } = useChanges(project);
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

  // Counter-driven refetch.
  useEffect(() => {
    if (!url) return;
    if (lastCounterRef.current === null) {
      lastCounterRef.current = counter;
      return;
    }
    if (counter !== lastCounterRef.current) {
      lastCounterRef.current = counter;
      load();
    }
  }, [counter, url, load]);

  // Focus + 30s floor, both gated on visibility -- never fetch while hidden.
  useEffect(() => {
    if (!url) return;
    const onFocus = () => { if (!document.hidden) load(); };
    window.addEventListener("focus", onFocus);
    const floor = setInterval(() => {
      if (document.hidden) return;
      if (performance.now() - lastFetchAtRef.current >= FLOOR_MS) load();
    }, 5000);
    return () => {
      window.removeEventListener("focus", onFocus);
      clearInterval(floor);
    };
  }, [url, load]);

  return { data, polled, error, refresh: load };
}
