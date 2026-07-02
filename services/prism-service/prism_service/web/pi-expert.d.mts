/**
 * Type surface for pi-expert.mjs (task e70cdcda) — lets the SPA
 * (src/lib/piAgent.ts, tsc strict) import the shared expert module
 * without duplicating the prompt or the TypeBox catalog. Keep in sync
 * with the runtime exports in pi-expert.mjs; the catalog value shape is
 * exactly what lib/piAgent.ts's prismTool() consumes.
 */
import type { AgentTool } from "@earendil-works/pi-agent-core";

/** PI's PRISM expertise: stores, gate doctrine, task/memory discipline. */
export declare const EXPERT_SYSTEM_PROMPT: string;

/** The full PRISM tool catalog keyed by real MCP tool name. */
export declare const EXPERT_TOOL_DEFS: Record<
  string,
  {
    label: string;
    description: string;
    parameters: AgentTool["parameters"];
  }
>;
