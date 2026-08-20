/** The conductor workflow definition, sourced from the service.
 *
 * This module is the ONE place the SPA learns the SDLC step ordering.
 * workflowChips.ts used to carry WORKFLOW_STEPS_ORDERED — a hand-copied
 * duplicate of the backend's models/workflow.py WORKFLOW_STEPS that nothing
 * kept in sync, so a step added or reordered on the server silently left the
 * rail rendering yesterday's pipeline. GET /api/workflows serves the real
 * FSM; workflowChips.ts keeps only the label/tone helpers, which key off a
 * step id it is GIVEN and therefore never need the list.
 *
 * Two consumers with genuinely different lifecycles, one fetcher:
 *   - the conductor rail (StepRail/SdlcProgress) needs the ORDERING, which
 *     does not vary by project and never changes at runtime -> fetched once
 *     and cached for the tab (useWorkflowSteps).
 *   - the /workflows canvas needs the whole view INCLUDING live occupancy,
 *     which is per-project and changes constantly -> fetches directly
 *     (fetchWorkflowDef) on its own poll.
 */

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { getProject } from "@/lib/project";

export type WorkflowStepType = "agent" | "gate" | "intake" | "behavior";

/** One conductor FSM step as the service describes it. `persona` resolves
 * through the backend's STEP_ROLES, so a GATE (whose `agent` is null —
 * nobody AUTHORS a gate) still names the Steward who adjudicates it. */
export type WorkflowStepDef = {
  id: string;
  agent: string | null;
  type: WorkflowStepType;
  validation: string | null;
  persona: string;
  persona_label: string;
  purpose: string;
  input: string;
  action: string;
  output: string;
  execution: "connected" | "scripted" | "definition_only";
  linked_workflow_id?: string | null;
  linked_workflow_step_count?: number;
  runner?: string;
  command?: string;
  working_directory?: string;
  timeout_seconds?: number;
  average_duration_seconds?: number | null;
  duration_sample_count?: number;
  depends_on?: string[];
  script_path?: string;
  script_language?: string;
  script_source?: string;
};

/** A bot: a role card that drives the conductor's FSM. */
export type WorkflowBot = { id: string; persona_label: string; card: string };

export type WorkflowDef = {
  steps: WorkflowStepDef[];
  bots: WorkflowBot[];
  /** step id -> count of non-done tasks standing there right now. */
  occupancy: Record<string, number>;
  /** Selectable project workflows. Absent only for an older service. */
  workflows?: WorkflowCatalogEntry[];
};

export type WorkflowCatalogEntry = Omit<WorkflowDef, "workflows"> & {
  id: string;
  name: string;
  description: string;
  /** Directory nesting: absent/null for a top-level entry, otherwise the id
   * of the catalog entry this one is disclosed under (e.g. a bot's own
   * FSM/Behavior entries nest under that bot's top-level entry). */
  parent_id?: string | null;
};

export type WorkflowStepResult = {
  step: string;
  exitCode: number;
  output: string;
  status: "pending" | "passed" | "failed" | "skipped" | "timed_out";
};

export type WorkflowRun = {
  id: string;
  status: string;
  createTime: string;
  completeTime?: string | null;
  runtime?: {
    currentStep: string;
    status: string;
    exitCode?: number | null;
    startedAt?: string | null;
  } | null;
  timeline?: Array<{
    step: string;
    startedAt: string;
    endedAt?: string | null;
    status: string;
  }>;
  data: {
    project: string;
    definition?: {
      steps?: Array<{ id: string }>;
    };
    tests: WorkflowStepResult;
    build: WorkflowStepResult;
    passed: boolean;
  };
};

/** The shape the conductor rail renders. Deliberately the same {id, persona,
 * type} triple the rail already consumed, so moving the SOURCE of the list
 * costs its consumers one line each and changes nothing downstream. */
export type RailStep = { id: string; persona: string; type: WorkflowStepType };

/** `intake` is NOT a conductor step: it is the pre-conductor state a task
 * sits in before the first agent step, which is why models/workflow.py has
 * no row for it and should not grow one. The rail draws it as the leading
 * entry, so it is prepended here — one synthetic entry, not a second copy
 * of the pipeline. */
const INTAKE: RailStep = { id: "intake", persona: "", type: "intake" };

export function fetchWorkflowDef(project: string): Promise<WorkflowDef> {
  return api.get<WorkflowDef>(
    `/api/workflows?project=${encodeURIComponent(project)}`,
  );
}

export function startWorkflowRun(project: string, workflowId: string): Promise<{ instanceId: string; reused?: boolean }> {
  return api.post(
    `/api/workflows/${encodeURIComponent(workflowId)}/runs?project=${encodeURIComponent(project)}`,
    {},
  );
}

export function fetchWorkflowRun(instanceId: string): Promise<WorkflowRun> {
  return api.get(`/api/workflows/runs/${encodeURIComponent(instanceId)}`);
}

export function fetchActiveWorkflowRun(project: string, workflowId: string): Promise<{ instanceId: string }> {
  return api.get(
    `/api/workflows/${encodeURIComponent(workflowId)}/runs/active?project=${encodeURIComponent(project)}`,
  );
}

export function fetchWorkflowRunHistory(project: string, workflowId: string, limit = 72): Promise<{ runs: WorkflowRun[] }> {
  return api.get(
    `/api/workflows/${encodeURIComponent(workflowId)}/runs/history?project=${encodeURIComponent(project)}&limit=${limit}`,
  );
}

export function requestWorkflowFix(
  project: string, workflowId: string, instanceId: string, stepId: string,
): Promise<{ queued: boolean; task_id: string; status: string; next: string }> {
  return api.post(
    `/api/workflows/${encodeURIComponent(workflowId)}/fixes?project=${encodeURIComponent(project)}`,
    { instance_id: instanceId, step_id: stepId },
  );
}

function railFrom(steps: WorkflowStepDef[]): RailStep[] {
  return [INTAKE, ...steps.map((s) => ({ id: s.id, persona: s.persona, type: s.type }))];
}

// Tab-lifetime cache: the ordering is identical for every project and never
// changes while the tab is open, so the rail must not refetch it per mount
// (SdlcProgress renders once per task card on the board).
let pending: Promise<WorkflowDef> | null = null;
let railCache: RailStep[] | null = null;

/** The ordered conductor rail: intake followed by the service's FSM.
 * Returns just the intake row until the first fetch resolves — honest about
 * what it knows, rather than seeding a hardcoded list that would quietly
 * become the duplicate this module exists to delete. */
export function useWorkflowSteps(): RailStep[] {
  const [steps, setSteps] = useState<RailStep[]>(railCache ?? [INTAKE]);

  useEffect(() => {
    if (railCache) return;
    let cancel = false;
    if (!pending) pending = fetchWorkflowDef(getProject());
    pending
      .then((def) => {
        railCache = railFrom(def.steps);
        if (!cancel) setSteps(railCache);
      })
      .catch(() => {
        // Service unreachable — retry on the next mount rather than
        // caching a failure for the life of the tab.
        pending = null;
      });
    return () => { cancel = true; };
  }, []);

  return steps;
}
