// Conductor's visible signal language — step + gate chips. Tone selection
// for both is owned by the single resolver in lib/domainTone (domainTone
// "step" / "gate"); this module only builds the chip class strings and
// exposes the ordered step list. Only render when conductor is engaged on a
// task (workflow_step !== '' or gate_state !== 'none').

import { domainTone, toneClass } from "./domainTone";

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

export function stepLabel(step: string): string {
  if (!step) return "";
  return step.replace(/_/g, " ");
}

export function gateLabel(state: GateState): string {
  return state;
}

const CHIP_SIZING = "px-2 py-0.5 rounded text-[10px] uppercase tracking-wider font-mono";

export function stepChipClass(step: string): string {
  return toneClass(domainTone("step", step) ?? "slate") + " " + CHIP_SIZING;
}

export function gateChipClass(state: GateState): string {
  return toneClass(domainTone("gate", state) ?? "slate") + " " + CHIP_SIZING;
}

export function conductorEngaged(
  step: string | undefined | null,
  gate: string | undefined | null,
): boolean {
  return Boolean((step && step !== "") || (gate && gate !== "none"));
}

// Canonical role labels for the three roles in the /api/roles registry. Kept
// as a static fallback so the sync StepRail/SdlcProgress can name a persona
// without awaiting the async useRoles fetch; the /learning ledger uses the live
// registry (richer: tier + effort). These three ids never change — the old
// sprawl (lead/architect/general) collapsed to sm/qa/dev.
export const PERSONA_LABELS: Record<string, string> = {
  sm: "Steward",
  qa: "Verifier",
  dev: "Builder",
};

/** Human label for a role/persona id (Steward/Verifier/Builder), else the id. */
export function personaLabel(id: string | undefined | null): string {
  if (!id) return "";
  return PERSONA_LABELS[id] ?? id;
}

// Each step's `persona` is the role that OWNS it, aligned to the backend
// step_roles map: sm-planning + ALL GATES are Steward-reviewed (gates are no
// longer blank — the Steward is the distinct-actor reviewer), qa authors/checks
// tests, dev implements. StepRail reads `persona` to tag WHO owns each row.
export const WORKFLOW_STEPS_ORDERED: { id: string; persona: string; type: "agent" | "gate" | "intake" }[] = [
  { id: "intake", persona: "", type: "intake" },
  { id: "review_previous_notes", persona: "sm", type: "agent" },
  { id: "draft_story", persona: "sm", type: "agent" },
  { id: "story_gate", persona: "sm", type: "gate" },
  { id: "verify_plan", persona: "sm", type: "agent" },
  { id: "plan_gate", persona: "sm", type: "gate" },
  { id: "write_failing_tests", persona: "qa", type: "agent" },
  { id: "red_gate", persona: "sm", type: "gate" },
  { id: "implement_tasks", persona: "dev", type: "agent" },
  { id: "verify_green_state", persona: "qa", type: "agent" },
  { id: "green_gate", persona: "sm", type: "gate" },
];
