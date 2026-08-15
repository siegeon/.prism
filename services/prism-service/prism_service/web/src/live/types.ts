/** Shapes shared by /api/work/graph (boot snapshot) and /sse/work
 * (incremental push) — the gamify walking skeleton's wire contract.
 * Mirrors prism_service/api/work.py's response shape 1:1. */

export type NodeKind = "task" | "subtask" | "session";

export type GraphNode = {
  id: string;
  kind: NodeKind;
  label: string;
  status: string;
  workflow_step: string;
  gate_state: string;
  activity_state: string;
  heartbeat_age_s: number | null;
  tok_s: number | null;
  tokens_total: number | null;
  /** Live USD spend, task/subtask nodes only (api/work.py's _task_node
   * data-enrichment slice) — absent on session nodes. */
  spend_usd?: number | null;
  /** Seconds since the CURRENT gate went pending, None when not pending
   * (api/work.py gate_waiting_s) — round 2, piece 3/4: the honest source
   * for a gate card's "waiting Xm" label and for backdating
   * gatePendingSince on boot/reconcile, instead of ever inventing "now". */
  gate_waiting_s?: number | null;
  /** Count of not-yet-started children (api/work.py queue_depth) — the
   * real "backed up" signal, distinct from merely counting parent_of
   * edges (which includes children already in flight). */
  queue_depth?: number;
  href: string;
};

export type EdgeKind = "parent_of" | "driven_in";

export type GraphEdge = {
  source: string;
  target: string;
  kind: EdgeKind;
};

export type GraphSnapshot = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  generated_at: number;
};

/** The four bus event types /sse/work forwards (routes/sse.py
 * _WORK_EVENT_TYPES) — a discriminated union on `type`. */
export type WorkEvent =
  | { project: string; type: "task.changed"; task_id: string; fields?: Record<string, unknown> }
  | { project: string; type: "drive.heartbeat"; task_id: string; step?: string; last_tool?: string; work_units?: number; elapsed_s?: number; ts: number }
  | { project: string; type: "agent.run"; task_id: string; session_id?: string; agent_id?: string; parent_agent_id?: string | null; step?: string; role?: string; model?: string; ok?: boolean; ts: number }
  | { project: string; type: "tokens.turn"; task_id: string; session_id: string; out_tokens: number; dt_s: number; tok_s: number; tokens_total: number; ts: number; usd_total?: number };
