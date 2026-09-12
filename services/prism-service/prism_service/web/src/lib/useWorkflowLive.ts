/** The Workflows canvas's 1-second live channel (owner: "Nodes should be
 * very fast linked to view").
 *
 * GET /api/workflows answers the full catalog (engine JSON, node trend,
 * role bots, tiers) and measured 21-52s under load, so a node's real
 * position lagged the screen by 20-60s. GET /api/workflows/live answers
 * ONLY the tiny live signal underneath it (services/workflow_live.py) --
 * this hook polls that route every second instead, independently of the
 * page's existing slower catalog poll.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export type WorkflowLiveNode = {
  task_id: string;
  driver: string;
  tool: string;
  node: string;
  age_s: number;
  since: string;
  dispatching: boolean;
};

export type WorkflowLiveEntry = {
  occupancy: Record<string, number>;
  live: Record<string, WorkflowLiveNode>;
};

export type WorkflowLivePayload = {
  ts: number;
  entries: Record<string, WorkflowLiveEntry>;
  conductor: WorkflowLiveEntry;
};

const POLL_MS = 1000;
const BACKOFF_MS = 5000;
const FAILURES_BEFORE_BACKOFF = 3;

export function useWorkflowLive(project: string): WorkflowLivePayload | null {
  const [payload, setPayload] = useState<WorkflowLivePayload | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    let failures = 0;

    const tick = () => {
      // Paused while hidden: no fetch is scheduled here at all. The
      // visibilitychange listener below is what resumes it, immediately,
      // the moment the tab is visible again.
      if (document.visibilityState !== "visible") return;
      api
        .get<WorkflowLivePayload>(
          `/api/workflows/live?project=${encodeURIComponent(project)}`,
        )
        .then((data) => {
          if (cancelled) return;
          failures = 0;
          setPayload(data);
          timer = window.setTimeout(tick, POLL_MS);
        })
        .catch(() => {
          if (cancelled) return;
          failures += 1;
          const delay = failures >= FAILURES_BEFORE_BACKOFF ? BACKOFF_MS : POLL_MS;
          timer = window.setTimeout(tick, delay);
        });
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        window.clearTimeout(timer);
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    tick();

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [project]);

  return payload;
}
