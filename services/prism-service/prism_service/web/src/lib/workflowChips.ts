// Maps WORKFLOW_STEPS ids and gate states to the unified --accent-{tone}
// palette /memory + /graph + /understand share. Conductor's visible
// signal language — only render when conductor is engaged on a task
// (workflow_step !== '' or gate_state !== 'none').

export type WorkflowStep =
  | ""
  | "intake"
  | "review_previous_notes"
  | "draft_story"
  | "story_gate"
  | "verify_plan"
  | "plan_gate"
  | "write_failing_tests"
  | "red_gate"
  | "implement_tasks"
  | "verify_green_state"
  | "green_gate";

export type GateState = "none" | "pending" | "passed" | "failed";

const STEP_TONE: Record<string, string> = {
  intake: "violet",
  review_previous_notes: "teal",
  draft_story: "teal",
  story_gate: "slate",
  verify_plan: "teal",
  plan_gate: "slate",
  write_failing_tests: "violet",
  red_gate: "slate",
  implement_tasks: "amber",
  verify_green_state: "violet",
  green_gate: "slate",
};

const GATE_TONE: Record<GateState, string> = {
  none: "slate",
  pending: "amber",
  passed: "emerald",
  failed: "rose",
};

export function stepLabel(step: string): string {
  if (!step) return "";
  return step.replace(/_/g, " ");
}

export function gateLabel(state: GateState): string {
  return state;
}

export function stepChipClass(step: string): string {
  const tone = STEP_TONE[step] ?? "slate";
  return [
    "bg-[color:var(--accent-" + tone + "-bg)]",
    "text-[color:var(--accent-" + tone + "-fg)]",
    "ring-1 ring-[color:var(--accent-" + tone + "-ring)]",
    "px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-mono",
  ].join(" ");
}

export function gateChipClass(state: GateState): string {
  const tone = GATE_TONE[state];
  return [
    "bg-[color:var(--accent-" + tone + "-bg)]",
    "text-[color:var(--accent-" + tone + "-fg)]",
    "ring-1 ring-[color:var(--accent-" + tone + "-ring)]",
    "px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-mono",
  ].join(" ");
}

export function conductorEngaged(
  step: string | undefined | null,
  gate: string | undefined | null,
): boolean {
  return Boolean((step && step !== "") || (gate && gate !== "none"));
}

export const WORKFLOW_STEPS_ORDERED: { id: string; persona: string; type: "agent" | "gate" | "intake" }[] = [
  { id: "intake", persona: "", type: "intake" },
  { id: "review_previous_notes", persona: "sm", type: "agent" },
  { id: "draft_story", persona: "sm", type: "agent" },
  { id: "story_gate", persona: "", type: "gate" },
  { id: "verify_plan", persona: "sm", type: "agent" },
  { id: "plan_gate", persona: "", type: "gate" },
  { id: "write_failing_tests", persona: "qa", type: "agent" },
  { id: "red_gate", persona: "", type: "gate" },
  { id: "implement_tasks", persona: "dev", type: "agent" },
  { id: "verify_green_state", persona: "qa", type: "agent" },
  { id: "green_gate", persona: "", type: "gate" },
];
