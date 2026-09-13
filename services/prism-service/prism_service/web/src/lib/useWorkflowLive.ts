/** The Workflows canvas's live occupancy channel (owner: "Nodes should be
 * very fast linked to view").
 *
 * GET /api/workflows answers the full catalog (engine JSON, node trend,
 * role bots, tiers) and measured 21-52s under load, so a node's real
 * position lagged the screen by 20-60s. GET /api/workflows/live answers
 * ONLY the tiny live signal underneath it (services/workflow_live.py).
 *
 * Task fix/polling (SSE coordination round, owner: "why are you hammering
 * the server with polling rather than updating with streaming"): this
 * used to run its own bare 1Hz setTimeout poll with a private backoff.
 * Occupancy moves on the scale of a task transition, which is exactly
 * what a `task_changed` GET /sse/changes event already announces -- so
 * this now rides the shared lib/usePolledResource.ts gate instead of a
 * second, independent polling loop: refetch on a real task_changed event,
 * on focus/visibilitychange, or a 60s reconnect-safety floor if the
 * stream itself looks unhealthy. Same external contract
 * (`useWorkflowLive(project): WorkflowLivePayload | null`), so callers
 * (the Workflows canvas) need no change.
 */

import { usePolledResource } from "@/lib/usePolledResource";

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

// Stable identity (module scope) so usePolledResource never re-subscribes
// on every render.
const TASK_CHANGED_KINDS = ["task_changed"];

export function useWorkflowLive(project: string): WorkflowLivePayload | null {
  const { data } = usePolledResource<WorkflowLivePayload>(
    `/api/workflows/live?project=${encodeURIComponent(project)}`,
    project,
    TASK_CHANGED_KINDS,
  );
  return data;
}
