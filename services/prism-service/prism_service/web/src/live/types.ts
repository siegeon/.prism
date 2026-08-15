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
  | { project: string; type: "tokens.turn"; task_id: string; session_id: string; out_tokens: number; dt_s: number; tok_s: number; tokens_total: number; ts: number };
