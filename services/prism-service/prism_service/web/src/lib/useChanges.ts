/**
 * useChanges — the ONE SSE subscription (GET /sse/changes) per tab that
 * replaces the SPA's former shared 1Hz GET /api/changes poll.
 *
 * Owner, live (2026-09-13): "why are you hammering the server with
 * polling rather than updating with streaming". This subscribes instead,
 * through the SAME shared EventSource machinery every other push channel
 * in this app already uses (lib/sharedStream.ts: one connection per URL
 * per tab, a Web Locks leader election across tabs, auto-reconnect with
 * backoff) — so "one EventSource per tab" and "auto-reconnect" come for
 * free rather than being reimplemented here.
 *
 * The backend (routes/sse.py sse_changes, backed by
 * services/wakeups.py's wait()/changed_since()) pushes one frame per
 * real signal: {kind, project, task_id, at}.
 *
 * Interface (coordination point: lib/usePolledResource.ts and any other
 * consumer, including the Workflows/Live canvas rebuild, read the SAME
 * shape from here):
 *   - `useChanges(project)` → { counter, last, healthy }. `counter` is a
 *     monotonic int that bumps on every frame (compare it, don't read
 *     meaning into its value) so a consumer can tell "a new event
 *     arrived" apart from "the same event re-rendered"; `last` is that
 *     event's {kind, project, task_id, at}; `healthy` is true while
 *     frames are actually flowing, for a consumer's own reconnect-safety
 *     floor.
 *   - `subscribeToChangeKind(kind, cb, project?)` → unsubscribe. An
 *     imperative escape hatch for code that isn't a component render
 *     (e.g. driving a canvas's own animation loop) and so can't use the
 *     hook above; fires `cb` only for frames matching `kind`.
 */
import { useEffect, useState } from "react";
import { subscribeStream } from "@/lib/sharedStream";

export type ChangeEvent = { kind: string; project: string; task_id: string | null; at: number };

type RawFrame = { kind?: string; project?: string; task_id?: string | null; at?: number };

function parseFrame(data: string, fallbackProject: string): ChangeEvent | null {
  try {
    const parsed = JSON.parse(data) as RawFrame;
    if (!parsed.kind) return null;
    return {
      kind: parsed.kind,
      project: parsed.project ?? fallbackProject,
      task_id: parsed.task_id ?? null,
      at: parsed.at ?? 0,
    };
  } catch {
    return null; // a malformed frame is dropped -- the next one still arrives
  }
}

export function useChanges(project = ""): { counter: number; last: ChangeEvent | null; healthy: boolean } {
  const [counter, setCounter] = useState(0);
  const [last, setLast] = useState<ChangeEvent | null>(null);
  const [healthy, setHealthy] = useState(false);

  useEffect(() => {
    const qs = project ? `?project=${encodeURIComponent(project)}` : "";
    return subscribeStream(
      `/sse/changes${qs}`,
      (data) => {
        const event = parseFrame(data, project);
        if (!event) return;
        setCounter((n) => n + 1);
        setLast(event);
      },
      setHealthy,
    );
  }, [project]);

  return { counter, last, healthy };
}

/** Imperative subscription to ONE event kind, for a consumer that isn't a
 * component render (e.g. a canvas's own rAF-driven animation loop). Rides
 * the same shared per-URL EventSource as useChanges -- subscribing here
 * costs no extra connection. */
export function subscribeToChangeKind(
  kind: string, cb: (ev: ChangeEvent) => void, project = "",
): () => void {
  const qs = project ? `?project=${encodeURIComponent(project)}` : "";
  return subscribeStream(`/sse/changes${qs}`, (data) => {
    const event = parseFrame(data, project);
    if (event && event.kind === kind) cb(event);
  });
}
