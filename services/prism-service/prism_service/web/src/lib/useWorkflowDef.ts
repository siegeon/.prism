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
  /** Gate steps only — who may decide it and how to recover from a wrong
   * decision. Empty for non-gate steps. */
  authority?: string;
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
    // Optional: a run synthesized from a conductor task (see
    // fetchConductorRunFromTask below) has neither -- there is no
    // WorkflowCore build/test result behind a conductor step.
    tests?: WorkflowStepResult;
    build?: WorkflowStepResult;
    passed: boolean;
    /** Present only on a run synthesized from a conductor task. The
     * conductor's own summary rides here instead of forcing task state
     * (workflow_step/gate_state) into the build/test fields above, which
     * mean something else entirely for a scripted validation run. */
    conductorTask?: {
      id: string;
      title: string;
      status: string;
      workflowStep?: string | null;
      gateState?: string | null;
      stranded: boolean;
    };
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

/** The subset of a conductor task's own fields the rail already has on hand
 * (from useConductorState/GET /api/tasks) -- enough to synthesize a run
 * without a second round trip for anything the rail already fetched. */
export type ConductorRunTask = {
  id: string;
  title: string;
  status?: string;
  workflow_step?: string | null;
  gate_state?: string | null;
  stranded?: boolean;
};

type ConductorHistoryRow = { action?: string; details?: string; timestamp?: string };

/** Which step a mid-dwell history row is a genuine SETBACK for, or null if
 * this row isn't one. conductor_service.py writes three distinct shapes
 * DURING a step's dwell (between the advance_task rows that open and close
 * it), and the original version of this function only ever read
 * advance_task -- silently discarding all three, which is why a task that
 * actually struggled (failed a step twice, or got rejected at a gate
 * before eventually passing) replayed as one smooth, unbroken "passed"
 * segment (owner: "the animation on that playback appears to be made up",
 * confirmed live against task 93d6c6f3's real 3 flow_report_failure rows
 * at verify_green_state):
 *   - `action="flow_report_failure"`, `details="step=<id>; outcome=..."` --
 *     a step attempt that failed and had to retry. `outcome` varies shape
 *     (a bare `fail`, or a `{'ok': False, ...}` dict repr) but the action
 *     name alone already says it failed; no need to parse `outcome`.
 *   - `action="advance_refused"`, `details="step=<id>; validation=...; ..."`
 *     -- a review step's own validation refused to let it advance (same
 *     class of setback, same shape).
 *   - `action="gate_decide"`, `details="gate=<id>_gate; ..."` where the
 *     outcome was a rejection or control-plane failure, not an approval --
 *     `_build_timeline` (api/tasks.py) documents this exact ambiguity: a
 *     `verifier=fail` row can still be immediately overridden (`override=
 *     True`) into a genuine pass, so only a fail WITHOUT an override, an
 *     explicit `action=reject`, or a `control-plane=fail` counts. */
function conductorFailedStepFromDetails(action: string | undefined, details: string): string | null {
  if (action === "flow_report_failure" || action === "advance_refused") {
    return /step=([^;]+)/.exec(details)?.[1]?.trim() ?? null;
  }
  if (action === "gate_decide") {
    const gateMatch = /gate=(\w+_gate)/.exec(details);
    if (!gateMatch) return null;
    const rejected = /action=reject/.test(details)
      || /control-plane=fail/.test(details)
      || (/verifier=fail/.test(details) && !/override=True/.test(details));
    return rejected ? gateMatch[1] : null;
  }
  return null;
}

/** Reconstructs a replay timeline from a task's own audit history.
 * conductor_service.advance_task (conductor_service.py:1653) writes one
 * `action="advance_task"` row per transition with `details="from=X; to=Y..."`
 * -- there is no separate run-history endpoint for the conductor because
 * there is no WorkflowCore instance; this history IS the instance record.
 *
 * A real setback (see conductorFailedStepFromDetails) closes the attempt
 * that just failed with `status: "failed"` and reopens a fresh dwell at the
 * SAME step, so a step tried three times before passing produces three
 * "failed" segments followed by the "passed" one the eventual advance_task
 * row closes -- the replay walks through the retries instead of absorbing
 * them into one clean success. */
function conductorTimelineFromHistory(
  history: ConductorHistoryRow[],
): NonNullable<WorkflowRun["timeline"]> {
  const events: NonNullable<WorkflowRun["timeline"]> = [];
  let open: { step: string; startedAt: string } | null = null;
  for (const row of history) {
    if (!row.timestamp) continue;
    if (row.action === "advance_task") {
      const to = /to=([^;]+)/.exec(row.details ?? "")?.[1]?.trim();
      if (!to) continue;
      if (open) {
        events.push({ step: open.step, startedAt: open.startedAt, endedAt: row.timestamp, status: "passed" });
      }
      open = { step: to, startedAt: row.timestamp };
      continue;
    }
    const failedStep = conductorFailedStepFromDetails(row.action, row.details ?? "");
    if (failedStep && open?.step === failedStep) {
      events.push({ step: open.step, startedAt: open.startedAt, endedAt: row.timestamp, status: "failed" });
      open = { step: failedStep, startedAt: row.timestamp };
    }
  }
  if (open) events.push({ step: open.step, startedAt: open.startedAt, status: "passed" });
  return events;
}

/** Builds the SAME WorkflowRun shape validation's historical replay already
 * consumes, so the shared replayHistoricalRun/beginHistoricalReplay machinery
 * needs no conductor-specific branch of its own -- only the DATA a conductor
 * task actually has (step transitions off its own history, gate_state) fills
 * it in, never a build/test result that does not exist for it.
 *
 * A task still in flight gets a `runtime` block (no closed timeline to
 * replay yet) so the canvas shows its live current-step progress the same
 * way a running scripted workflow's step does; a done task gets a full
 * `timeline` so it replays start to finish like a completed validation run. */
/** The fields of a fresh task row the conductor run cares about --
 * intentionally the same names the REST task shape uses (workflow_step,
 * gate_state), so no remapping is needed between this and ConductorRunTask. */
type ConductorFreshTask = {
  id?: string;
  title?: string;
  status?: string;
  workflow_step?: string | null;
  gate_state?: string | null;
  stranded?: boolean;
};

export function fetchConductorRunFromTask(project: string, task: ConductorRunTask): Promise<WorkflowRun> {
  return api.get<{ task?: ConductorFreshTask; history: ConductorHistoryRow[] }>(
    `/api/tasks/${encodeURIComponent(task.id)}?project=${encodeURIComponent(project)}&scope=core`,
  ).then(({ task: fresh, history }) => {
    // The caller's `task` comes off the rail's own last poll/SSE push, which
    // can be a step or two behind a task that is genuinely advancing right
    // now -- this SAME request already returns the current row, so read the
    // step/status/gate off IT instead of the caller's possibly-stale copy.
    // Before this fix, `runtime.currentStep` below (derived from `history`,
    // always fresh) and `conductorTask.workflowStep` (the caller's stale
    // field) could name two DIFFERENT steps on the same screen at once --
    // exactly the "not clear what is currently running" the owner reported,
    // confirmed live against task a205eb7a mid-drive.
    const title = fresh?.title ?? task.title;
    const status = fresh?.status ?? task.status;
    const workflowStep = fresh?.workflow_step ?? task.workflow_step;
    const gateState = fresh?.gate_state ?? task.gate_state;
    const timeline = conductorTimelineFromHistory(history ?? []);
    const last = timeline.at(-1);
    const done = status === "done";
    const stranded = Boolean(task.stranded);
    const runtime = done ? null : {
      currentStep: last?.step ?? workflowStep ?? "",
      status: "running",
      startedAt: last?.startedAt ?? new Date().toISOString(),
    };
    return {
      id: task.id,
      status: done ? (stranded ? "Terminated" : "Complete") : "Runnable",
      createTime: timeline[0]?.startedAt ?? new Date().toISOString(),
      runtime,
      timeline: done ? timeline : undefined,
      data: {
        project,
        passed: done && !stranded,
        conductorTask: {
          id: task.id, title: title ?? task.title, status: status ?? "",
          workflowStep, gateState, stranded,
        },
      },
    };
  });
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
