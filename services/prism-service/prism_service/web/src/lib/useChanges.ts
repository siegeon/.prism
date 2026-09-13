/**
 * useChangeEvents — the ONE SSE subscription (GET /sse/changes) per tab
 * that replaces the SPA's former shared 1Hz GET /api/changes poll.
 *
 * Owner, live (2026-09-13), on the polling-discipline pass that came
 * before this file: "why are you hammering the server with polling
 * rather than updating with streaming". The counter-poll layer was
 * already an improvement over ~10 independent per-page pollers, but it
 * was still a poll. This subscribes instead, through the SAME shared
 * EventSource machinery every other push channel in this app already
 * uses (lib/sharedStream.ts: one connection per URL per tab, a Web
 * Locks leader election across tabs, auto-reconnect with backoff) — so
 * "one EventSource per tab" and "auto-reconnect" come for free rather
 * than being reimplemented here.
 *
 * The backend (routes/sse.py sse_changes, backed by
 * services/wakeups.py's wait()/changed_since()) pushes one frame per
 * real signal: {kind, project, counter}. This hook exposes the latest
 * frame plus a monotonic `seq` so a consumer (usePolledResource) can
 * tell "a new event arrived" apart from "the same event re-rendered",
 * and `healthy` (frames are actually flowing) so a consumer's own
 * reconnect-safety floor only ever fires while the stream looks dead.
 */
import { useEffect, useState } from "react";
import { subscribeStream } from "@/lib/sharedStream";

export type ChangeEvent = { seq: number; kind: string; project: string; counter: number };

let seqCounter = 0;

export function useChangeEvents(project = ""): { event: ChangeEvent | null; healthy: boolean } {
  const [event, setEvent] = useState<ChangeEvent | null>(null);
  const [healthy, setHealthy] = useState(false);

  useEffect(() => {
    const qs = project ? `?project=${encodeURIComponent(project)}` : "";
    return subscribeStream(
      `/sse/changes${qs}`,
      (data) => {
        try {
          const parsed = JSON.parse(data) as { kind?: string; project?: string; counter?: number };
          if (!parsed.kind) return;
          seqCounter += 1;
          setEvent({
            seq: seqCounter,
            kind: parsed.kind,
            project: parsed.project ?? project,
            counter: parsed.counter ?? 0,
          });
        } catch {
          // ignore a malformed frame -- the next one still arrives
        }
      },
      setHealthy,
    );
  }, [project]);

  return { event, healthy };
}
