import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useReducedMotion } from "motion/react";
import { useProject } from "@/lib/project";
import { subscribeStream } from "@/lib/sharedStream";
import { api } from "@/lib/api";
import { fetchActiveWorkflowRun, fetchConductorRunFromTask, fetchWorkflowDef, fetchWorkflowRun, fetchWorkflowRunHistory, requestWorkflowFix, startWorkflowRun, type WorkflowCatalogEntry, type WorkflowRun, type WorkflowStepDef } from "@/lib/useWorkflowDef";
import { useConductorState, type ManagedTask } from "@/lib/useConductorState";
import SdlcProgress, { type Activity, type PhaseProgress } from "@/components/conductor/SdlcProgress";
import { WorkflowGraph, drawWorkflows, type ActiveNodeProgress, type NodeVerdict, type RunView } from "@/live/workflowGraph";
import type { SegmentGrab, WireEnd } from "@/live/wireEditing";
import type { Point, WirePort } from "@/live/wires";
import Editor from "@monaco-editor/react";

/** /workflows — the conductor's FSM and the bots that drive it, per project.
 *
 * In PRISM a workflow IS a bot: an FSM that agentically interacts with the
 * conductor's FSM. Both already exist server-side, so this section stores
 * NOTHING of its own — it is a view assembled from GET /api/workflows (the
 * step list off models/workflow.py, the bots off ROLE_CARDS, and occupancy
 * counted from the task rows the board already keeps).
 *
 * Structurally this is LivePage's shape minus the live wire: it owns the DOM
 * canvas, the rAF loop, and pan/zoom/drag; all geometry and drawing live in
 * live/workflowGraph.ts. The FSM changes only on deploy and occupancy moves
 * on the scale of a task transition, so a 10s poll is the honest refresh
 * here — an SSE subscription would be a stream with nothing to say.
 */

/** One pill in the bottom rail, regardless of what it's actually backed by
 * (a WorkflowCore run for a scripted workflow, a live task for conductor).
 * Every workflow's rail renders this SAME shape through ONE component --
 * only the code that PRODUCES the list differs. */
type RailPill = {
  key: string;
  tone: string;
  disabled: boolean;
  ariaLabel?: string;
  title?: string;
  ringHighlighted: boolean;
  onClick?: () => void;
};

function positionsKey(project: string): string {
  return `prism.workflows.positions.${project}`;
}

/** Re-docked wire ends, keyed `<wire>:from` / `<wire>:to` — the same shape
 * /live persists under prism.live.ports.<project>. */
function portsKey(project: string): string {
  return `prism.workflows.ports.${project}`;
}

/** Mid-path bends, per wire. Client state like positions: the canvas is a
 * VIEW, so how the owner arranged it is theirs, not the service's. */
function waypointsKey(project: string): string {
  return `prism.workflows.waypoints.${project}`;
}

/** Directory child order, keyed per project -- which order a bot's own
 * nested FSM/Behavior entries display in. Purely a display preference
 * (how the owner arranged their own folder), never sent to the service. */
function childOrderKey(project: string): string {
  return `prism.workflows.childOrder.${project}`;
}

/** Reads one saved blob, tolerating absent/corrupt/blocked storage — a bad
 * entry means "boot from the deterministic layout", never a broken page. */
function readJson<T>(key: string, apply: (raw: T) => void): void {
  try {
    const raw = localStorage.getItem(key);
    if (raw) apply(JSON.parse(raw) as T);
  } catch {
    // corrupt or unavailable storage — fall through to defaults
  }
}

function writeJson(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // storage full/unavailable — the edit holds for this session only
  }
}

/** Screen-space px a pointer must travel before a press becomes a pan or a
 * node drag — keeps a plain click from being swallowed by a pixel of jitter. */
const DRAG_THRESHOLD_PX = 5;
const POLL_MS = 10_000;
const RECONNECT_MIN_MS = 1_000;
const RECONNECT_MAX_MS = 10_000;
const DIRECTORY_MIN_PX = 180;
const DIRECTORY_MAX_PX = 480;
const DIRECTORY_DEFAULT_PX = 240;
const DIRECTORY_WIDTH_KEY = "prism.workflows.directory.width";
const REPLAY_SPEED = 120;
const REPLAY_MIN_STEP_MS = 1500;
const REPLAY_MAX_STEP_MS = 5000;
const REPLAY_MAX_GAP_MS = 1800;
const RUN_RAIL_PILLS = 72;

type ReplayEvent = NonNullable<WorkflowRun["timeline"]>[number];

function replayStepMs(event: ReplayEvent, speed: number = REPLAY_SPEED): number {
  const elapsed = event.endedAt
    ? Math.max(0, Date.parse(event.endedAt) - Date.parse(event.startedAt))
    : REPLAY_MIN_STEP_MS;
  return Math.min(REPLAY_MAX_STEP_MS, Math.max(REPLAY_MIN_STEP_MS, elapsed / speed));
}

/** One CONCLUDED conductor node, exactly as the recorder stored it: what the
 * node said at decision time, never a live re-check. */
type FlowNodeRun = {
  run_id: string; task_id: string; node_id: string; actor: string;
  outcome: string; reason: string; started_at: string; ended_at: string;
  flow_version: number;
};
/** basis is "teeth" (decided/total for a gate) or "work_units" (the drive
 * heartbeat's own counted units for an agent step). Never elapsed time. */
type FlowRuns = {
  workflow_id: string; task_id: string; flow_version: number;
  finished: boolean; visible: boolean; runs: FlowNodeRun[];
  progress?: { basis: string; done: number; total: number | null } | null;
};

function replayGapMs(event: ReplayEvent, next?: ReplayEvent, speed: number = REPLAY_SPEED): number {
  if (!next?.startedAt || !event.endedAt) return 0;
  const gap = Math.max(0, Date.parse(next.startedAt) - Date.parse(event.endedAt));
  return Math.min(REPLAY_MAX_GAP_MS, gap / speed);
}

function replaySpanMs(event: ReplayEvent, next?: ReplayEvent, speed: number = REPLAY_SPEED): number {
  // Keep the clock moving continuously until the next recorded event. A
  // separate post-fill delay made short steps look complete long before the
  // replay advanced, which falsely read as a stalled workflow.
  return Math.min(REPLAY_MAX_STEP_MS, replayStepMs(event, speed) + replayGapMs(event, next, speed));
}

/** p95 of a step's REAL durations across the last N completed runs -- the
 * exact same dataset the pill rail counts (visibleRunHistory, capped at
 * RUN_RAIL_PILLS), never a separately-fetched sample set. A plain mean
 * (the previous pacing source, server's average_duration_seconds) gets
 * dragged around by one fast or slow outlier and reads as a bar that
 * either rockets to 98% early or crawls long past when the step usually
 * finishes -- p95 is the "this is how long it takes most of the time"
 * number a person actually wants from a progress bar. Null (not 0) below
 * the 3-sample floor, so the caller's existing fallback chain (server
 * average, then the indeterminate wiggle) still applies rather than
 * pacing off one or two noisy runs. Owner 2026-08-26, watching this bar
 * live on the Workflows canvas: "it should be the p95 duration of the
 * last x runs (like the count of the pills or something)". */
function p95StepDurationSeconds(runs: WorkflowRun[], stepId: string): number | null {
  const samples: number[] = [];
  for (const run of runs) {
    const entry = run.timeline?.find((t) => t.step === stepId && t.endedAt);
    if (!entry?.startedAt || !entry.endedAt) continue;
    const secs = (Date.parse(entry.endedAt) - Date.parse(entry.startedAt)) / 1000;
    if (Number.isFinite(secs) && secs > 0) samples.push(secs);
  }
  if (samples.length < 3) return null;
  samples.sort((a, b) => a - b);
  const idx = Math.min(samples.length - 1, Math.ceil(0.95 * samples.length) - 1);
  return samples[idx];
}

function pillIndexTitle(index: number, offset: number, runs: WorkflowRun[]): string | undefined {
  const run = runs[index - offset];
  if (!run) return undefined;
  const outcome = run.runtime?.status === "running"
    ? `running · ${run.runtime.currentStep}`
    : run.data.passed ? "passed" : "failed";
  return `${new Date(run.createTime).toLocaleString()} · ${outcome}`;
}

function formatDuration(seconds?: number | null): string {
  if (seconds == null) return "No runs yet";
  if (seconds < 1) return "<1s";
  const whole = Math.round(seconds);
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

/** Summary text for the top run badge when the instance is a conductor
 * task, not a scripted validation run -- the task's own title/step/gate
 * state, never the build/test phrasing that badge otherwise renders. */
function conductorRunSummary(run: WorkflowRun): string {
  const t = run.data.conductorTask;
  if (!t) return "";
  if (t.status === "done") return `${t.title} · ${t.stranded ? "done · not yet on origin/main" : "shipped"}`;
  // Task 8fbd5cf0 stop_if: "a stored gate refusal reason never reaches the
  // canvas." A correct "gate failed" label still leaves a driver guessing
  // WHAT to fix -- the seat already named the exact reason in
  // task.gate_reason (TaskDetailPage renders the same string via
  // LinkedText); this is the SAME text, carried here instead of a second,
  // silent source of truth.
  const gate = t.gateState === "pending" ? " · awaiting gate"
    : t.gateState === "failed" ? ` · REFUSED: ${t.gateReason?.trim() || "gate failed"}`
    : "";
  return `${t.title} · ${(t.workflowStep ?? "").replace(/_/g, " ") || "in progress"}${gate}`;
}

function conductorRunTone(run: WorkflowRun): string {
  if (run.status === "Terminated") return "border-amber-300/60 text-amber-200";
  if (run.status === "Complete") return run.data.passed ? "border-emerald-500/60 text-emerald-300" : "border-red-500/60 text-red-300";
  // A refused gate on a still-RUNNING task (the seat has decided, the task
  // just hasn't rewound/closed yet) must read as refused immediately, not
  // as the same in-progress teal as a gate that is still deciding -- this
  // is the exact live misfire the task names (fc471aed: "machine deciding"
  // shown for 18 minutes after the seat had already refused).
  if (run.data.conductorTask?.gateState === "failed") return "border-red-500/60 text-red-300";
  return "border-[color:var(--accent-solid)]/60 text-[color:var(--text-secondary)]";
}

type FailureEvidence = { location: string | null; lines: string[] };

function failureEvidence(output?: string): FailureEvidence | null {
  if (!output?.trim()) return null;
  const clean = output.replace(/\x1b\[[0-9;]*m/g, "");
  const lines = clean.split(/\r?\n/);
  const locationPatterns = [
    /(?:File\s+["][^\"]+["]|[^\s:]+\.(?:py|ts|tsx|js|jsx|cs))[:,]?\s*line?\s*\d+/i,
    /[^\s:]+\.(?:py|ts|tsx|js|jsx|cs):\d+(?::\d+)?/i,
  ];
  let index = lines.findIndex((line) => locationPatterns.some((pattern) => pattern.test(line)));
  if (index < 0) index = lines.findIndex((line) => /(?:error|failed|exception|assert)/i.test(line));
  if (index < 0) index = Math.max(0, lines.length - 1);
  const location = locationPatterns
    .map((pattern) => lines[index].match(pattern)?.[0] ?? null)
    .find(Boolean) ?? null;
  return {
    location,
    lines: lines.slice(Math.max(0, index - 2), Math.min(lines.length, index + 4)),
  };
}

function failureMarkerLine(scriptSource: string, scriptPath: string | undefined, evidence: FailureEvidence | null): number | null {
  if (!evidence) return null;
  const lines = scriptSource.split(/\r?\n/);
  const locationMatch = evidence.location?.match(/^(.*?)(?::|,?\s+line\s+)(\d+)(?::\d+)?$/i);
  if (locationMatch && scriptPath) {
    const referencedPath = locationMatch[1].replace(/^File\s+["']|["']$/g, "");
    const referencedName = referencedPath.split(/[\\/]/).at(-1);
    const scriptName = scriptPath.split(/[\\/]/).at(-1);
    if (referencedPath === scriptPath || (referencedName && referencedName === scriptName)) {
      return Math.max(1, Math.min(lines.length, Number(locationMatch[2])));
    }
  }
  const invocation = lines.findLastIndex((line) => /^\s*(?:exec\s+)?(?:uv|npm|pnpm|yarn|pytest|dotnet|python|node)\b/.test(line));
  return invocation >= 0 ? invocation + 1 : null;
}

function workflowForGraph(workflow: WorkflowCatalogEntry): WorkflowCatalogEntry {
  // task 25b2a05c: verify_green_state now carries its own real
  // linked_workflow_id (verify-green-state-loop) straight off the API, so
  // this no longer needs to inject a "validation" fallback here -- the
  // backend never omits it.
  const graphWorkflow = workflow;
  // Validation is a state machine; persona ownership is already disclosed on
  // each state card and must not become a competing second topology.
  return graphWorkflow.id === "validation" ? { ...graphWorkflow, bots: [] } : graphWorkflow;
}

function connectWorkflowCatalog(catalog: WorkflowCatalogEntry[]): WorkflowCatalogEntry[] {
  const byId = new Map(catalog.map((workflow) => [workflow.id, workflow]));
  return catalog.map((workflow) => ({
    ...workflow,
    steps: workflow.steps.map((step) => {
      // task 25b2a05c: no "validation" fallback -- linked_workflow_id is
      // always real for every step that has one now.
      const linkedId = step.linked_workflow_id ?? null;
      const linked = linkedId ? byId.get(linkedId) : null;
      return linked ? {
        ...step, linked_workflow_id: linkedId,
        linked_workflow_step_count: linked.steps.length,
      } : step;
    }),
  }));
}

function historicalWorkflowForRun(workflow: WorkflowCatalogEntry, run: WorkflowRun): WorkflowCatalogEntry {
  const recordedIds = run.data.definition?.steps?.map((step) => step.id) ?? [];
  if (!recordedIds.length) return workflowForGraph(workflow);
  const byId = new Map(workflow.steps.map((step) => [step.id, step]));
  const steps = recordedIds.map((id) => byId.get(id)).filter((step): step is WorkflowStepDef => Boolean(step));
  return steps.length ? workflowForGraph({ ...workflow, steps }) : workflowForGraph(workflow);
}

/** Task 8fbd5cf0 oracle: "REPLAY: any finished run replays from the stored
 * record only, node by node, each showing what it concluded at that time.
 * Recompute nothing, so the same run twice gives the identical answer."
 *
 * Before this, replay's timeline came from conductorTimelineFromHistory --
 * a reverse-map of task_history rows, the exact thing this task's own
 * premises name as the OLD, wrong mechanism (workflows.py's own docstring:
 * "A declarative FSM behaviour has no WorkflowCore run behind it"). This
 * maps the RECORDED flow_node_runs rows (GET /{workflow}/runs, read by
 * flow_run_recorder.runs_for_task -- a plain SELECT, never a re-run of any
 * check) onto the same ReplayEvent shape the player already walks, so one
 * code path drives replay regardless of where the events came from.
 * outcome is only ever "pass"/"fail" at every one of the three recorder
 * call sites (conductor_flow.flow_report, gate_adjudicator.sweep_once,
 * ship_worker.ship_task) -- a refused gate records "fail", not a third
 * value, so the mapping below is exhaustive. */
function replayTimelineFromRecordedRuns(runs: FlowNodeRun[]): NonNullable<WorkflowRun["timeline"]> {
  return runs.map((r) => ({
    step: r.node_id,
    startedAt: r.started_at,
    endedAt: r.ended_at || undefined,
    status: r.outcome === "pass" ? "passed" : "failed",
  }));
}

type DragState = {
  mode: "none" | "pan" | "node" | "port" | "waypoint" | "segment";
  moved: boolean;
  lastX: number;
  lastY: number;
  nodeId: string | null;
  offsetX: number;
  offsetY: number;
  /** mode "port"/"waypoint": which wire is being edited, and which end or
   * which bend of it. */
  wireKey: string | null;
  wireEnd: WireEnd | null;
  waypointIndex: number;
  /** mode "segment": the grabbed run and the anchors bounding it. */
  segment: SegmentGrab | null;
};

const IDLE_DRAG: DragState = {
  mode: "none", moved: false, lastX: 0, lastY: 0, nodeId: null, offsetX: 0, offsetY: 0,
  wireKey: null, wireEnd: null, waypointIndex: -1, segment: null,
};

export default function WorkflowsPage() {
  const [project] = useProject();
  const navigate = useNavigate();
  // ?workflow=<id> deep-links directly to a behavior (e.g. "plan-gate-check",
  // "land", "story-gate-check") without a manual sidebar click -- same
  // convention as Understand's `?concept=`: seed initial state from the URL,
  // keep the URL in sync on every selection via `replace` (a workflow click
  // is a view change, not a new history stop), same tier as `?project=`.
  const [searchParams, setSearchParams] = useSearchParams();
  const reduced = useReducedMotion();
  // The conductor's rail shows tasks the conductor is actually engaged with
  // RIGHT NOW -- not a WorkflowCore run. conductor drives real tasks through
  // Python (advance_task/gate_decide), never through AosWorkflows, so there
  // is no run instance to poll; useConductorState is the SAME live, SSE-
  // pushed source LiveBar.tsx already reads for "who's working now", reused
  // here rather than inventing a second one.
  const { managed: conductorManaged } = useConductorState(project);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const graphRef = useRef<WorkflowGraph>(new WorkflowGraph());
  // prefers-reduced-motion: `reduced` (useReducedMotion() above) already
  // tracks the OS setting live for SdlcProgress's own framer-motion tweens
  // -- the canvas's token/packet system is hand-rolled canvas-2D, not a
  // framer-motion element the hook can reach directly, so it needs the
  // SAME value relayed onto the graph instance the rAF loop actually
  // draws. One source of truth, not a second matchMedia listener.
  useEffect(() => {
    graphRef.current.setReducedMotion(!!reduced);
  }, [reduced]);
  const [flowRuns, setFlowRuns] = useState<FlowRuns | null>(null);
  const lastFlowNodeRef = useRef<string>("");
  const dragRef = useRef<DragState>({ ...IDLE_DRAG });
  const suppressCanvasClickRef = useRef(false);
  const [connectionInterrupted, setConnectionInterrupted] = useState(false);
  const [reconnectAttempt, setReconnectAttempt] = useState(0);
  const [grabbing, setGrabbing] = useState(false);
  const [directoryOpen, setDirectoryOpen] = useState(true);
  const [directoryWidth, setDirectoryWidth] = useState(() => {
    try {
      const saved = Number(localStorage.getItem(DIRECTORY_WIDTH_KEY));
      if (Number.isFinite(saved)) return Math.max(DIRECTORY_MIN_PX, Math.min(DIRECTORY_MAX_PX, saved));
    } catch { /* storage unavailable */ }
    return DIRECTORY_DEFAULT_PX;
  });
  const directoryResizeRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowCatalogEntry[]>([]);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState(
    () => searchParams.get("workflow") || "conductor",
  );
  // Progressive disclosure for the directory: a catalog entry with a
  // parent_id (a bot's own FSM/Behavior entries, nested under that bot)
  // stays collapsed under its parent until expanded -- "conductor" starts
  // open since that's the one bot in the system today.
  const [expandedDirectoryIds, setExpandedDirectoryIds] = useState<Set<string>>(
    () => new Set(["conductor"]),
  );
  // Saved drag order of a parent's nested children, keyed by parent id.
  // Ids not (yet) in a saved order fall back to the order the API sent.
  const [childOrder, setChildOrder] = useState<Record<string, string[]>>({});
  const [draggedChildId, setDraggedChildId] = useState<string | null>(null);
  const [dragOverChildId, setDragOverChildId] = useState<{ id: string; before: boolean } | null>(null);
  const [workflowPath, setWorkflowPath] = useState<Array<{
    workflowId: string;
    stepId: string;
  }>>([]);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  // The real per-node answers of a drilled-in behaviour layer, for the task
  // in context. Null on any layer or context where there is nothing real to
  // say -- an empty map is drawn as "no verdict", never as a pass.
  const [nodeVerdicts, setNodeVerdicts] = useState<Record<string, NodeVerdict> | null>(null);
  const [stateDetailsOpen, setStateDetailsOpen] = useState(false);
  const [stateDetailsOrigin, setStateDetailsOrigin] = useState({ x: 50, y: 50 });
  const [scriptDiagnosticOpen, setScriptDiagnosticOpen] = useState(false);
  const [testStep, setTestStep] = useState<number | null>(null);
  const testWorkflowRef = useRef<WorkflowCatalogEntry | null>(null);
  const testModeRef = useRef<"runtime" | "replay" | null>(null);
  // AC-7: a real, user-adjustable playback speed for a done-task replay --
  // previously REPLAY_SPEED was a hardcoded constant only ever rendered as a
  // label with nothing to click. The ref is what the timing functions below
  // actually read (they're plain functions, not hooks); the state re-renders
  // the control and its label.
  const [replaySpeed, setReplaySpeed] = useState(REPLAY_SPEED);
  const replaySpeedRef = useRef(REPLAY_SPEED);
  useEffect(() => {
    replaySpeedRef.current = replaySpeed;
  }, [replaySpeed]);
  const replayStepStartedRef = useRef(0);
  const replayStepDurationRef = useRef(REPLAY_MIN_STEP_MS);
  const replayTimelineRef = useRef<ReplayEvent[]>([]);
  // The CURRENTLY-ANIMATING event's own status, read by the rAF loop below
  // to tint a mid-replay step that genuinely failed and retried -- distinct
  // from replayStoppedAt, which only ever names the FINAL event.
  const replayEventStatusRef = useRef<string | null>(null);
  // Ref mirror of "an instance overlay is open" -- the def+occupancy poll
  // below is a STABLE closure (effect deps are just [project], so it never
  // sees a fresh `selectedHistoryRunId`/testModeRef read from state) that
  // must still know, on its own next tick, whether to skip re-asserting the
  // board's live occupancy over a deliberately-chosen instance's own.
  const viewingInstanceRef = useRef(false);
  const [replayEventIndex, setReplayEventIndex] = useState<number | null>(null);
  const [replayStoppedAt, setReplayStoppedAt] = useState<ReplayEvent | null>(null);
  const pendingReplayRef = useRef<WorkflowRun | null>(null);
  const [workflowRun, setWorkflowRun] = useState<WorkflowRun | null>(null);
  const [workflowRunHistory, setWorkflowRunHistory] = useState<WorkflowRun[]>([]);
  const [selectedHistoryRunId, setSelectedHistoryRunId] = useState<string | null>(null);
  const [historyOverlayOpen, setHistoryOverlayOpen] = useState(false);
  const [historyOverlayReady, setHistoryOverlayReady] = useState(false);
  const [workflowRunError, setWorkflowRunError] = useState<string | null>(null);
  const [startingWorkflow, setStartingWorkflow] = useState(false);
  const [requestingFix, setRequestingFix] = useState(false);
  const [fixTaskId, setFixTaskId] = useState<string | null>(null);
  const [fixRequestError, setFixRequestError] = useState<string | null>(null);
  const [failureIdsCopied, setFailureIdsCopied] = useState(false);
  const [brainActivity, setBrainActivity] = useState<{
    stale: boolean; running: boolean; queueDepth: number; inFlight: number;
  } | null>(null);
  const selectedWorkflowRef = useRef(searchParams.get("workflow") || "conductor");

  useEffect(() => {
    window.dispatchEvent(new CustomEvent("prism:connection-state", {
      detail: { scope: "workflows", interrupted: connectionInterrupted, attempt: reconnectAttempt },
    }));
    return () => {
      window.dispatchEvent(new CustomEvent("prism:connection-state", {
        detail: { scope: "workflows", interrupted: false, attempt: 0 },
      }));
    };
  }, [connectionInterrupted, reconnectAttempt]);

  useEffect(() => {
    let cancelled = false;
    let timer = 0;
    const load = () => Promise.all([
      api.get<{ brain: boolean; graph: boolean }>(`/api/staleness?project=${encodeURIComponent(project)}`),
      api.get<{ workers: Array<{ id: string; running: boolean; queue_depth?: number; in_flight?: number }> }>(`/api/consolidation/workers?project=${encodeURIComponent(project)}`),
    ]).then(([staleness, workers]) => {
      if (cancelled) return;
      const learning = workers.workers.find((worker) => worker.id === "memory_learning_pipeline");
      setBrainActivity({
        stale: staleness.brain || staleness.graph,
        running: learning?.running ?? false,
        queueDepth: learning?.queue_depth ?? 0,
        inFlight: learning?.in_flight ?? 0,
      });
    }).catch(() => { /* validation remains usable while learning status reconnects */ })
      .finally(() => { if (!cancelled) timer = window.setTimeout(load, 5_000); });
    load();
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [project]);

  // Definition + live occupancy, polled. Re-applying a payload only refreshes
  // counts and which wires read live; node geometry is derived, so a poll
  // never disturbs a layout the owner has dragged.
  useEffect(() => {
    let cancel = false;
    let timer = 0;
    let failures = 0;
    const load = () => {
      fetchWorkflowDef(project)
        .then((def) => {
          if (cancel) return;
          failures = 0;
          setConnectionInterrupted(false);
          setReconnectAttempt(0);
          const catalog = connectWorkflowCatalog(def.workflows ?? [{
            id: "conductor", name: "Conductor", description: "PRISM delivery workflow",
            steps: def.steps, bots: def.bots, occupancy: def.occupancy,
          }]);
          setWorkflows(catalog);
          const selected = catalog.find((workflow) => workflow.id === selectedWorkflowRef.current)
            ?? catalog[0];
          // Live occupancy IS the correct default view of the conductor's
          // own canvas -- but while an instance overlay is open (a replay,
          // or a conductor task's live current-step progress),
          // re-applying the board's real, constantly-changing occupancy
          // here on every POLL_MS tick stomps the deliberately-chosen
          // instance's own synthetic occupancy, which is what read as
          // "random animations" instead of a deliberate step-through
          // (owner, live evidence: conductor's real occupancy right now is
          // {green_gate: 25, plan_gate: 13, story_gate: 5, ...} while every
          // OTHER workflow's -- validation included -- is always {}, which
          // is exactly why "Build and test"'s own replay never showed this
          // and conductor's did).
          if (selected && !viewingInstanceRef.current) {
            graphRef.current.setDef(workflowForGraph(selected));
            // Wire edits rehydrate AFTER setDef: both maps are keyed by wire,
            // and the wire list only exists once a definition has landed.
            // Unknown keys are dropped inside the editor.
            readJson<Record<string, WirePort>>(portsKey(project), (raw) =>
              graphRef.current.wireEdits.hydrate(raw, undefined));
            readJson<Record<string, Point[]>>(waypointsKey(project), (raw) =>
              graphRef.current.wireEdits.hydrate(undefined, raw));
            const canvas = canvasRef.current;
            graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600);
          }
          timer = window.setTimeout(load, POLL_MS);
        })
        .catch(() => {
          if (cancel) return;
          failures += 1;
          setConnectionInterrupted(true);
          setReconnectAttempt(failures);
          const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** (failures - 1));
          timer = window.setTimeout(load, delay);
        });
    };
    // Rehydrate before the first paint so dragged nodes never visibly snap
    // from their default slot to the saved one.
    readJson<Record<string, Point>>(positionsKey(project),
      (raw) => graphRef.current.hydrateOverrides(raw));
    load();
    return () => { cancel = true; window.clearTimeout(timer); };
  }, [project]);

  const selectWorkflow = useCallback((
    workflow: WorkflowCatalogEntry,
    path: Array<{ workflowId: string; stepId: string }> = [],
  ) => {
    selectedWorkflowRef.current = workflow.id;
    setSelectedWorkflowId(workflow.id);
    // Keep the address bar a valid link to THIS behavior -- copying it and
    // loading it fresh must land back here, never on the default conductor
    // view. `replace: true` so clicking through several behaviors doesn't
    // pile up back-button stops for what is really one page's view state.
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev);
      next.set("workflow", workflow.id);
      return next;
    }, { replace: true });
    setWorkflowPath(path);
    setSelectedNodeId(null);
    setTestStep(null);
    setReplayEventIndex(null);
    setSelectedHistoryRunId(null);
    setHistoryOverlayOpen(false);
    setHistoryOverlayReady(false);
    pendingReplayRef.current = null;
    testWorkflowRef.current = null;
    testModeRef.current = null;
    viewingInstanceRef.current = false;
    setWorkflowRun(null);
    graphRef.current.clearOverrides();
    // Re-navigating within the app (directory click, drill into a linked
    // workflow, breadcrumb back) must not silently discard the owner's
    // saved layout. clearOverrides() drops the in-memory maps so a stale
    // drag from a DIFFERENT workflow's node ids can't bleed in, but
    // without refilling them from localStorage here, this workflow's
    // nodes place at their auto-layout defaults, and the very next drag's
    // persist() overwrites localStorage with that now-empty map --
    // permanently losing every position the owner had set. Node positions
    // rehydrate BEFORE setDef (so nodes never visibly snap from default to
    // saved, same as the initial-mount effect above); wire ports/waypoints
    // rehydrate AFTER setDef, because they validate against the freshly
    // built wire list.
    readJson<Record<string, Point>>(positionsKey(project),
      (raw) => graphRef.current.hydrateOverrides(raw));
    graphRef.current.setDef(workflowForGraph(workflow));
    readJson<Record<string, WirePort>>(portsKey(project),
      (raw) => graphRef.current.wireEdits.hydrate(raw, undefined));
    readJson<Record<string, Point[]>>(waypointsKey(project),
      (raw) => graphRef.current.wireEdits.hydrate(undefined, raw));
    const canvas = canvasRef.current;
    graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600, true);
  }, [project, setSearchParams]);

  const selectedWorkflow = workflows.find((workflow) => workflow.id === selectedWorkflowId);
  const selectedStep = selectedWorkflow?.steps.find((step) => step.id === selectedNodeId);
  // AC-7 (task 8fbd5cf0): a CONCLUDED node is read from the stored run, never
  // recomputed -- the same node's live /node-status re-check can answer
  // differently later and must not overwrite what this node said when it
  // actually decided. Only a node with no stored run yet falls back to the
  // live check.
  const selectedNodeRun = selectedNodeId
    ? [...(flowRuns?.runs ?? [])].reverse().find((r) => r.node_id === selectedNodeId) ?? null
    : null;
  const selectedNodeVerdict: NodeVerdict | null = selectedNodeRun
    ? { state: selectedNodeRun.outcome === "pass" ? "passed" : "refused",
        reason: selectedNodeRun.reason }
    : selectedNodeId ? nodeVerdicts?.[selectedNodeId] ?? null : null;
  // Task 8fbd5cf0 oracle: "passed nodes keep a visible completed trail."
  // The drilled-in-layer node-status effect above (nodeVerdicts) only ever
  // populates for a CHILD behaviour (nodeStatusLayerId requires
  // selectedWorkflow.parent_id) -- the top-level conductor canvas, the
  // actual walking skeleton, drew every passed node with no persistent
  // mark at all. Every CONCLUDED node already has a stored verdict
  // (flowRuns.runs); reduce it the same way selectedNodeVerdict above
  // does, latest-wins per node, and merge it under the drilled-in verdicts
  // so a child layer's own node-status answer (a different id space) is
  // never shadowed.
  const flowRunNodeVerdicts = useMemo<Record<string, NodeVerdict> | null>(() => {
    if (!flowRuns?.runs?.length) return null;
    const out: Record<string, NodeVerdict> = {};
    for (const r of flowRuns.runs) {
      out[r.node_id] = { state: r.outcome === "pass" ? "passed" : "refused", reason: r.reason };
    }
    return out;
  }, [flowRuns]);
  const effectiveNodeVerdicts = (flowRunNodeVerdicts || nodeVerdicts)
    ? { ...(flowRunNodeVerdicts ?? {}), ...(nodeVerdicts ?? {}) }
    : null;
  // ONE rail mechanism for the whole state-machine family, not one per page
  // (owner, task 3baadd19, 2026-08-24: "we should only have one, we have
  // it the same in conductor, and in build and test, there should not be
  // unique behavior these are all the same component calling flows" --
  // then, catching a hardcoded "conductor" check: "the conductor family
  // should not be hardcoded like that, these are all hierarchical,
  // conductor is the only one now, but later we may have more state
  // machine top level workflows"). So this is NEVER selectedWorkflowId ===
  // "conductor" specifically -- a workflow is in the state-machine family
  // when it IS a top-level bot canvas (something else nests under it) OR
  // is nested UNDER one (parent_id set), full stop, whichever bot that
  // turns out to be. "validation"/"Build and test" is the one deliberate
  // exception: though nested for documentation (parent_id="conductor",
  // api/workflows.py -- it predates the Bot/Behavior registry), it is a
  // genuinely SCRIPTED workflow with its own real WorkflowCore run
  // history, never an FSM behavior -- it already has a working, real rail
  // (workflowRunHistory/refreshRunHistory below); this only covers every
  // OTHER bot-family entry (green-gate-status, red-gate-status, land, the
  // loop steps, and whatever a future bot adds), which showed an empty
  // rail forever -- /runs/history 404s for anything but "validation" (no
  // WorkflowCore run ever backs a declarative FSM-behavior diagram), so
  // there was structurally nothing for them to fetch.
  const hasChildWorkflows = workflows.some(
    (workflow) => workflow.parent_id === selectedWorkflowId,
  );
  const isStateMachineWorkflow = selectedWorkflowId !== "validation"
    && (hasChildWorkflows || !!selectedWorkflow?.parent_id);
  // The conductor's live instance view (a task still in flight, not a
  // replay) reuses SdlcProgress -- the SAME segmented, legibly-labeled
  // "fill up the panel while it's active" bar TaskDetailPage/PlanView
  // already render for exactly this -- instead of relying on the small
  // canvas node's own tiny progress fill (owner: "it is not clear what is
  // currently running... we have the logic to fill up the panel of the
  // task while that workflow is active"). `phase`/`activity` are
  // synthesized client-side from data this page already has (the current
  // step's average_duration_seconds, the run's own startedAt) rather than
  // a second, slower phase_progress fetch (scope=full, ~30s cold) -- the
  // same tradeoff the canvas's own activeProgress already makes.
  const conductorLivePhase = useMemo<PhaseProgress | null>(() => {
    if (!isStateMachineWorkflow || !workflowRun?.runtime || workflowRun.status !== "Runnable") return null;
    // COUNTED UNITS ONLY. This value once grew off the wall clock and the
    // step's stored average duration -- a bar that filled because time
    // passed, not because work got done. The server counts the real units
    // instead (flow_run_recorder.progress_source): teeth decided / teeth
    // total for a gate node, the drive heartbeat's own work_units for an
    // agent node. No clock reaches this value.
    const counted = flowRuns?.progress;
    if (!counted) return null;
    const total = counted.total ?? undefined;
    return {
      pct: total && total > 0 ? Math.min(1, counted.done / total) : undefined,
      basis: counted.basis,
      children_done: counted.done,
      children_total: total,
    };
  }, [isStateMachineWorkflow, workflowRun, flowRuns]);
  const conductorLiveActivity = useMemo<Activity | null>(() => {
    const t = conductorLivePhase && workflowRun?.data.conductorTask;
    if (!t) return null;
    return {
      state: t.gateState === "pending" ? "awaiting_gate" : t.gateState === "failed" ? "blocked" : "working",
    };
  }, [conductorLivePhase, workflowRun]);
  const selectedStepResult = selectedStep?.id === "test"
    ? workflowRun?.data.tests
    : selectedStep?.id === "build" ? workflowRun?.data.build : undefined;
  const selectedFailureEvidence = selectedStepResult && ["failed", "timed_out"].includes(selectedStepResult.status)
    ? failureEvidence(selectedStepResult.output)
    : null;
  const selectedScriptFrame = selectedStepResult?.status === "passed"
    ? "border-emerald-500/70"
    : selectedStepResult && ["failed", "timed_out"].includes(selectedStepResult.status)
      ? "border-red-500/70"
      : "border-[color:var(--accent-sage-ring)]";
  const selectedFailureMarkerLine = selectedStep?.script_source
    ? failureMarkerLine(selectedStep.script_source, selectedStep.script_path, selectedFailureEvidence)
    : null;
  const copyFailureIds = async () => {
    if (!workflowRun || !selectedStep) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify({
        instance_id: workflowRun.id,
        step_id: selectedStep.id,
      }));
      setFailureIdsCopied(true);
      window.setTimeout(() => setFailureIdsCopied(false), 1500);
    } catch {
      // Both values remain visible below if clipboard access is unavailable.
    }
  };
  const visibleRunHistory = workflowRunHistory
    .filter((run) => ["Complete", "Terminated"].includes(run.status))
    .slice(0, RUN_RAIL_PILLS)
    .reverse();
  // Grow from the left; reversing the engine's newest-first response leaves
  // the most recent run at the right edge of the filled span.
  const historyOffset = 0;
  // Conductor keeps no separate run-history array (workflowRunHistory stays
  // validation-only, see refreshRunHistory below) -- the synthesized run
  // openConductorInstance just fetched IS the selected instance, sitting in
  // workflowRun itself rather than a list this page polls.
  const selectedHistoryRun = isStateMachineWorkflow
    ? (workflowRun && workflowRun.id === selectedHistoryRunId ? workflowRun : null)
    : workflowRunHistory.find((run) => run.id === selectedHistoryRunId) ?? null;
  const selectedHistoryFrameTone = selectedHistoryRun?.status === "Terminated"
    ? "border-amber-300"
    : selectedHistoryRun?.status === "Runnable"
      ? "border-[color:var(--accent-solid)]"
      : selectedHistoryRun?.data.passed ? "border-emerald-400" : "border-red-400";
  const runPillTone = (pillIndex: number): string => {
    const run = visibleRunHistory[pillIndex - historyOffset];
    if (!run) return "bg-[color:var(--border-default)]/20";
    if (run.runtime?.status === "running" || !["Complete", "Terminated"].includes(run.status)) {
      return "bg-[color:var(--accent-solid)] animate-pulse";
    }
    if (run.status === "Terminated") return "bg-amber-300/60";
    if (run.data.passed) return "bg-emerald-400";
    return "bg-red-400";
  };
  // The conductor's own rail: real tasks it is engaged with RIGHT NOW, per
  // useConductorState (the same live, SSE-pushed source LiveBar.tsx already
  // reads) -- never a WorkflowCore run, since conductor drives tasks through
  // Python, not through AosWorkflows. Filtered to genuine FSM occupancy (a
  // real step id on THIS canvas) so a legacy/orphaned workflow_step can't
  // invent a pill. Oldest-updated first: the rail GROWS FROM THE LEFT, same
  // as validation's (visibleRunHistory[index] above, no offset subtracted)
  // -- filled pills start at index 0, empty capacity trails on the right.
  //
  // A CHILD behavior's own diagram uses SYNTHETIC step ids local to that
  // diagram (green-gate-status: candidate_controls/reachability/.../status
  // -- none of them ever equal a real task.workflow_step). So for a child,
  // resolve the REAL WORKFLOW_STEPS id it services via the conductor
  // canvas's own linked_workflow_id (the SAME field a click on the
  // conductor canvas already reverses the other direction with, see
  // handleCanvasClick below) -- "land" is the one child with no
  // linked_workflow_id of its own (it nests via _CONDUCTOR_LINKED_
  // BEHAVIOR_IDS directly, api/workflows.py, because green_gate is the
  // FSM's structurally-terminal step), so it falls back to "green_gate"
  // explicitly, same reasoning as that decision.
  const conductorStepIds = useMemo(() => {
    if (hasChildWorkflows) {
      // A top-level bot canvas (this workflow, whichever bot it turns out
      // to be) -- occupy every one of ITS OWN real step ids.
      return new Set((selectedWorkflow?.steps ?? []).map((step) => step.id));
    }
    const parentId = selectedWorkflow?.parent_id;
    if (!parentId || parentId === "validation") return new Set<string>();
    // A child behavior: resolve the ONE real step id on ITS PARENT canvas
    // that links to it, via the same linked_workflow_id field a click on
    // the parent canvas already reverses the other direction with (see
    // handleCanvasClick below) -- never assume the parent is "conductor".
    const parentCanvas = workflows.find((workflow) => workflow.id === parentId);
    const linkedStep = parentCanvas?.steps.find(
      (step) => step.linked_workflow_id === selectedWorkflowId,
    );
    if (linkedStep) return new Set([linkedStep.id]);
    // "land" is the one documented exception with no linked_workflow_id of
    // its own (api/workflows.py: green_gate is the FSM's structurally-
    // terminal step, so there is nowhere on the canvas to hang the link) --
    // mirrors that exact same hardcoded exception on the backend, not a
    // new one invented here.
    // "reap" (task f97c196d) is the step AFTER land and carries the same
    // exception for the same reason: green_gate is the FSM's structurally-
    // terminal WORKFLOW_STEPS entry, so neither terminal behavior has a step
    // to hang a linked_workflow_id on.
    if (selectedWorkflowId === "land" || selectedWorkflowId === "reap") {
      return new Set(["green_gate"]);
    }
    return new Set<string>();
  }, [selectedWorkflow, selectedWorkflowId, workflows, hasChildWorkflows]);
  // managed_tasks() (useConductorState's own source) deliberately EXCLUDES
  // status=="done" -- the same doctrine that drops a finished task off the
  // /conductor board. That's correct for "who's currently engaged", but it
  // meant a task that finished its ENTIRE drive had no pill here at all: no
  // green, no trace, nothing -- indistinguishable from never having run
  // (owner 2026-08-21, real task 4f74dafc). Validation's own rail shows
  // exactly this history (completed runs, colored by outcome); the
  // conductor rail needs the same, sourced from real completed tasks since
  // there is no WorkflowCore run to read it from.
  const [doneConductorTasks, setDoneConductorTasks] = useState<ManagedTask[]>([]);
  // "done" (green_gate passed, status flipped) is a claim about the SDLC,
  // never about whether the code actually reached anyone -- GET /api/tasks/
  // stranded is the real, already-existing, git-backed signal for that
  // (does a [task:<id8>] trailer resolve on origin/main). Without this a
  // done-but-uncommitted-or-unmerged task painted the SAME solid green as a
  // genuinely shipped one (owner 2026-08-21, real task 4f74dafc: drove green
  // clean, the actual button removal never left an uncommitted diff in its
  // own workspace) -- indistinguishable from success. Cross-referenced here
  // so the pill can tell the two apart instead of asserting only the nicer
  // half of the truth.
  const [strandedTaskIds, setStrandedTaskIds] = useState<Set<string>>(new Set());
  useEffect(() => {
    if (!isStateMachineWorkflow || conductorStepIds.size === 0) {
      setDoneConductorTasks([]);
      setStrandedTaskIds(new Set());
      return;
    }
    let cancelled = false;
    type DoneTaskRow = ManagedTask & { parent_id?: string };
    const load = () => Promise.all([
      api.get<{ tasks: DoneTaskRow[] }>(
        `/api/tasks?project=${encodeURIComponent(project)}&fields=id,title,status,workflow_step,gate_state,updated_at,parent_id`,
      ),
      api.get<{ stranded: Array<{ task_id: string }> }>(
        `/api/tasks/stranded?project=${encodeURIComponent(project)}`,
      ),
    ]).then(([{ tasks }, { stranded }]) => {
      if (cancelled) return;
      setDoneConductorTasks(tasks.filter((t) =>
        t.status === "done" && !t.parent_id && conductorStepIds.has(t.workflow_step ?? "")));
      setStrandedTaskIds(new Set(stranded.map((s) => s.task_id)));
    }).catch(() => { /* transient fetch failure; next poll retries */ });
    load();
    const id = window.setInterval(load, 10_000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [project, isStateMachineWorkflow, conductorStepIds]);
  // BELT: one unit per real step advance. `sendTransition` has always been
  // able to put a visible item on an FSM edge, and nothing ever called it
  // from real work -- so the board could show WHERE tasks were standing
  // (occupancy counts) but never that anything MOVED. Owner 2026-08-28:
  // "tokens and processing feeding the workflow ... Factorio". This is the
  // seam: watch each managed task's own workflow_step and, when one
  // changes, send exactly one unit down that transition. Motion therefore
  // MEANS a task advanced -- a still board is honestly still, which is the
  // same contract the ambient occupancy motion already keeps.
  const prevStepsRef = useRef<Map<string, string>>(new Map());
  useEffect(() => {
    if (!isStateMachineWorkflow) return;
    // While an instance overlay is open the canvas is replaying THAT task,
    // so board-wide traffic would mix two stories on one screen.
    if (viewingInstanceRef.current) return;
    const seen = prevStepsRef.current;
    for (const task of conductorManaged) {
      const step = task.workflow_step ?? "";
      if (!step) continue;
      const prev = seen.get(task.id);
      seen.set(task.id, step);
      // First sighting: record only. Every task looks "new" on first paint
      // and firing here would spray units for work that never moved.
      if (prev === undefined || prev === step) continue;
      // A rewind (green_gate -> implement_tasks) has no forward token wire;
      // sendTransition returns false and nothing is drawn, which is correct.
      graphRef.current.sendTransition(prev, step);
    }
    // Forget tasks that left the board so the map cannot grow forever.
    const liveIds = new Set(conductorManaged.map((t) => t.id));
    for (const id of [...seen.keys()]) if (!liveIds.has(id)) seen.delete(id);
  }, [isStateMachineWorkflow, conductorManaged]);

  const conductorRailTasks = isStateMachineWorkflow
    ? [...doneConductorTasks, ...conductorManaged.filter(
        (task) => conductorStepIds.has(task.workflow_step ?? ""))]
        .sort((a, b) => (a.updated_at ?? "").localeCompare(b.updated_at ?? ""))
        .slice(-RUN_RAIL_PILLS)
    : [];
  const conductorPillTone = (task: ManagedTask): string => {
    if (task.status === "done") {
      // Stranded: the SDLC passed but the code never reached origin/main --
      // amber, the SAME tone validation's own rail already uses for
      // "Terminated" (a run that finished without a clean pass/fail verdict).
      return strandedTaskIds.has(task.id) ? "bg-amber-300/60" : "bg-emerald-400";
    }
    if (task.gate_state === "pending" || task.gate_state === "failed") return "bg-fuchsia-400/70 animate-pulse";
    if (task.activity?.state === "working" || task.activity?.state === "driving") return "bg-[color:var(--accent-solid)] animate-pulse";
    return "bg-sky-400/50";
  };
  // ONE rail component for every workflow -- validation and conductor each
  // just produce the same {tone, title, onClick} shape from whatever their
  // real source of truth is (a WorkflowCore run vs. a live task), instead of
  // each rendering their own copy of the pill strip.
  const railPills: RailPill[] = isStateMachineWorkflow
    ? Array.from({ length: RUN_RAIL_PILLS }, (_, index) => {
        const task = conductorRailTasks[index];
        return {
          key: String(index),
          disabled: !task,
          tone: task ? conductorPillTone(task) : "bg-[color:var(--border-default)]/20",
          ariaLabel: task ? `Open task ${task.title}` : undefined,
          title: task
            ? task.status === "done"
              ? `${task.title} · done`
              : `${task.title} · ${selectedWorkflow?.steps.find((step) => step.id === task.workflow_step)?.purpose ?? task.workflow_step}${task.gate_state === "pending" ? " · awaiting gate" : ""}`
            : undefined,
          ringHighlighted: task?.id === selectedHistoryRunId,
          onClick: task ? () => openConductorInstance(task) : undefined,
        };
      })
    : Array.from({ length: RUN_RAIL_PILLS }, (_, index) => {
        const run = visibleRunHistory[index - historyOffset];
        return {
          key: String(index),
          disabled: !run,
          tone: runPillTone(index),
          ariaLabel: run ? `Replay workflow run from ${new Date(run.createTime).toLocaleString()}` : undefined,
          title: pillIndexTitle(index, historyOffset, visibleRunHistory),
          ringHighlighted: run?.id === selectedHistoryRunId,
          onClick: run ? () => replayHistoricalRun(run) : undefined,
        };
      });

  const refreshRunHistory = useCallback(() => {
    if (!selectedWorkflow || selectedWorkflow.id !== "validation") {
      setWorkflowRunHistory([]);
      return Promise.resolve();
    }
    return fetchWorkflowRunHistory(project, selectedWorkflow.id, RUN_RAIL_PILLS)
      .then(({ runs }) => setWorkflowRunHistory(runs));
  }, [project, selectedWorkflow]);

  useEffect(() => {
    refreshRunHistory().catch(() => { /* definition and active-run recovery own connection state */ });
  }, [refreshRunHistory]);

  // Runtime truth belongs to the engine, not to the lifetime of this tab.
  // Reattach after reload/navigation so an already-running scripted step
  // keeps its fill and clock visible.
  useEffect(() => {
    if (!selectedWorkflow || selectedWorkflow.id !== "validation" || workflowRun) return;
    let cancelled = false;
    fetchActiveWorkflowRun(project, selectedWorkflow.id)
      .then(({ instanceId }) => fetchWorkflowRun(instanceId))
      .then((run) => {
        if (cancelled) return;
        testWorkflowRef.current = selectedWorkflow;
        testModeRef.current = "runtime";
        setWorkflowRun(run);
        const index = selectedWorkflow.steps.findIndex((step) =>
          step.id === run.runtime?.currentStep);
        setTestStep(run.runtime?.status === "running" && index >= 0 ? index : null);
      })
      .catch(() => { /* 404 means this project has no active run. */ });
    return () => { cancelled = true; };
  }, [project, selectedWorkflow, workflowRun]);

  useEffect(() => {
    setScriptDiagnosticOpen(false);
    setFixTaskId(null);
    setFixRequestError(null);
  }, [selectedNodeId]);

  const queueAgentFix = useCallback(async () => {
    if (!selectedWorkflow || !workflowRun || !selectedStep) return;
    setRequestingFix(true);
    setFixRequestError(null);
    try {
      const result = await requestWorkflowFix(
        project, selectedWorkflow.id, workflowRun.id, selectedStep.id,
      );
      setFixTaskId(result.task_id);
    } catch (error) {
      setFixRequestError(error instanceof Error ? error.message : "Could not queue the fix");
    } finally {
      setRequestingFix(false);
    }
  }, [project, selectedStep, selectedWorkflow, workflowRun]);

  useEffect(() => {
    if (testModeRef.current === "replay") return;
    const workflow = testWorkflowRef.current;
    if (testStep === null || !workflow) return;
    const occupancy: Record<string, number> = Object.fromEntries(workflow.steps.map((step, index) => [
      step.id, index === testStep ? 1 : 0,
    ]));
    occupancy.__start__ = testStep < 0 ? 1 : 0;
    occupancy.__complete__ = testStep >= workflow.steps.length ? 1 : 0;
    graphRef.current.setDef(workflowForGraph({ ...workflow, occupancy }));
    if (testStep >= workflow.steps.length) return;
    const from = testStep < 0 ? "__start__" : workflow.steps[testStep].id;
    const to = testStep + 1 < workflow.steps.length
      ? workflow.steps[testStep + 1].id
      : "__complete__";
    graphRef.current.sendTransition(from, to);
  }, [testStep]);

  useEffect(() => {
    if (testModeRef.current !== "replay" || replayEventIndex === null) return;
    const workflow = testWorkflowRef.current;
    const timeline = replayTimelineRef.current;
    if (!workflow) return;

    if (replayEventIndex < 0) {
      const occupancy = Object.fromEntries(workflow.steps.map((step) => [step.id, 0]));
      graphRef.current.setDef({ ...workflow, occupancy: { ...occupancy, __start__: 1, __complete__: 0 } });
      setTestStep(-1);
      replayStepStartedRef.current = performance.now();
      replayStepDurationRef.current = 350;
      replayEventStatusRef.current = null;
      const timer = window.setTimeout(() => setReplayEventIndex(0), 350);
      return () => window.clearTimeout(timer);
    }

    if (replayEventIndex >= timeline.length) {
      const last = timeline.at(-1);
      if (!last) return;
      if (last.status !== "passed") {
        const failedStepIndex = workflow.steps.findIndex((step) => step.id === last.step);
        const occupancy = Object.fromEntries(workflow.steps.map((step) => [step.id, step.id === last.step ? 1 : 0]));
        graphRef.current.setDef({ ...workflow, occupancy: { ...occupancy, __start__: 0, __complete__: 0 } });
        setReplayStoppedAt(last);
        setTestStep(failedStepIndex >= 0 ? failedStepIndex : null);
        return;
      }
      const occupancy = Object.fromEntries(workflow.steps.map((step) => [step.id, 0]));
      graphRef.current.setDef({ ...workflow, occupancy: { ...occupancy, __start__: 0, __complete__: 1 } });
      graphRef.current.sendTransition(last.step, "__complete__");
      setReplayStoppedAt(null);
      setTestStep(workflow.steps.length);
      replayStepStartedRef.current = performance.now();
      replayStepDurationRef.current = 500;
      replayEventStatusRef.current = null;
      return;
    }

    const event = timeline[replayEventIndex];
    const stepIndex = workflow.steps.findIndex((step) => step.id === event.step);
    if (stepIndex < 0) {
      setReplayEventIndex((index) => (index ?? 0) + 1);
      return;
    }
    const occupancy = Object.fromEntries(workflow.steps.map((step) => [step.id, step.id === event.step ? 1 : 0]));
    graphRef.current.setDef({ ...workflow, occupancy: { ...occupancy, __start__: 0, __complete__: 0 } });
    graphRef.current.sendTransition(replayEventIndex === 0 ? "__start__" : timeline[replayEventIndex - 1].step, event.step);
    setTestStep(stepIndex);
    const duration = replaySpanMs(event, timeline[replayEventIndex + 1], replaySpeedRef.current);
    replayStepStartedRef.current = performance.now();
    replayStepDurationRef.current = duration;
    // A retried step (flow_report_failure/advance_refused/a rejected gate
    // decision, see conductorTimelineFromHistory) shows up here as its own
    // event with status "failed" -- tint it while it animates, not just the
    // run's terminal outcome, so a task that genuinely struggled doesn't
    // replay as one smooth all-green climb (owner: "the animation on that
    // playback appears to be made up").
    replayEventStatusRef.current = event.status;
    const timer = window.setTimeout(() => setReplayEventIndex((index) => (index ?? 0) + 1), duration);
    return () => window.clearTimeout(timer);
  }, [replayEventIndex]);

  const runScriptedWorkflow = useCallback(async () => {
    if (!selectedWorkflow) return;
    setStartingWorkflow(true);
    setSelectedHistoryRunId(null);
    setReplayEventIndex(null);
    setReplayStoppedAt(null);
    setWorkflowRun(null);
    setWorkflowRunError(null);
    testWorkflowRef.current = selectedWorkflow;
    testModeRef.current = "runtime";
    setTestStep(-1);
    try {
      const started = await startWorkflowRun(project, selectedWorkflow.id);
      const first = await fetchWorkflowRun(started.instanceId);
      setWorkflowRun(first);
      setWorkflowRunError(null);
    } catch (e) {
      testModeRef.current = null;
      setTestStep(null);
      setWorkflowRunError("Connection interrupted");
    } finally {
      setStartingWorkflow(false);
    }
  }, [project, selectedWorkflow]);

  const beginHistoricalReplay = useCallback((run: WorkflowRun) => {
    if (!selectedWorkflow) return;
    setWorkflowRun(run);
    const historicalWorkflow = historicalWorkflowForRun(selectedWorkflow, run);
    testWorkflowRef.current = historicalWorkflow;
    graphRef.current.setDef(historicalWorkflow);
    if (run.runtime?.status === "running") {
      // A conductor task still in flight has no closed timeline to replay --
      // show where it stands right now, the same way a live scripted run's
      // current step gets a progress bar (the rAF loop below reads
      // workflowRun.runtime for exactly this, off the SAME `run` this just
      // set into state).
      testModeRef.current = null;
      replayTimelineRef.current = [];
      setReplayStoppedAt(null);
      setReplayEventIndex(null);
      const index = historicalWorkflow.steps.findIndex((step) => step.id === run.runtime?.currentStep);
      setTestStep(index >= 0 ? index : null);
      return;
    }
    const historicalStepIds = new Set(historicalWorkflow.steps.map((step) => step.id));
    const fallbackTimeline = (run.timeline ?? []).filter((event) =>
      event.status !== "skipped" && historicalStepIds.has(event.step));
    const beginWith = (timeline: NonNullable<WorkflowRun["timeline"]>) => {
      replayTimelineRef.current = timeline;
      testModeRef.current = "replay";
      setReplayStoppedAt(null);
      replayStepStartedRef.current = performance.now();
      setReplayEventIndex(-1);
      setTestStep(-1);
    };
    const flowId = selectedWorkflow.parent_id ? selectedWorkflow.parent_id : selectedWorkflowId;
    if (!flowId) { beginWith(fallbackTimeline); return; }
    // Read the STORED record fresh, at replay time -- never the possibly-
    // stale `flowRuns` poll snapshot the live view happens to be holding.
    // "the same run twice gives the identical answer" means the source is
    // a plain SELECT against flow_node_runs, not client state.
    api.get<FlowRuns>(`/api/workflows/${encodeURIComponent(flowId)}/runs?task_id=${encodeURIComponent(run.id)}&project=${encodeURIComponent(project)}`)
      .then((recorded) => {
        const fromRecord = recorded.runs?.length
          ? replayTimelineFromRecordedRuns(recorded.runs).filter((event) => historicalStepIds.has(event.step))
          : null;
        // Recorded rows exist only for flows the recorder actually covers
        // (conductor's own pipeline) and for tasks driven since it shipped
        // -- an older task or a non-conductor scripted workflow falls back
        // to the task_history reconstruction rather than replaying nothing.
        beginWith(fromRecord?.length ? fromRecord : fallbackTimeline);
      })
      .catch(() => beginWith(fallbackTimeline));
  }, [selectedWorkflow, selectedWorkflowId, project]);

  const replayHistoricalRun = useCallback((run: WorkflowRun) => {
    if (!selectedWorkflow) return;
    pendingReplayRef.current = run;
    setSelectedHistoryRunId(run.id);
    setWorkflowRun(run);
    setWorkflowRunError(null);
    setSelectedNodeId(null);
    setTestStep(null);
    setReplayEventIndex(null);
    setReplayStoppedAt(null);
    testModeRef.current = null;
    // Read by the def+occupancy poll's own next tick (a stable [project]-only
    // closure that can't see this render's state) so it stops re-asserting
    // the board's live occupancy over this instance's synthetic occupancy.
    viewingInstanceRef.current = true;
    setHistoryOverlayReady(false);
    setHistoryOverlayOpen(false);
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => setHistoryOverlayOpen(true));
    });
  }, [selectedWorkflow]);

  // The conductor rail's own version of clicking a validation pill: open
  // the SAME animated instance view (replayHistoricalRun/beginHistoricalReplay
  // above), fed from a WorkflowRun synthesized off the task's own history
  // instead of a WorkflowCore run (owner 2026-08-21: the conductor rail
  // must use the same click-to-open logic "Build and test"'s rail already
  // has, not a plain navigate-away). Falls back to the task page if the
  // synth fetch fails, so a pill is never a dead click.
  const openConductorInstance = useCallback((task: ManagedTask) => {
    fetchConductorRunFromTask(project, {
      id: task.id, title: task.title, status: task.status,
      workflow_step: task.workflow_step, gate_state: task.gate_state,
      gate_reason: task.gate_reason,
      stranded: strandedTaskIds.has(task.id),
    })
      .then((run) => replayHistoricalRun(run))
      .catch(() => navigate(`/tasks/${task.id}`));
  }, [project, strandedTaskIds, replayHistoricalRun, navigate]);

  const leaveHistoricalReplay = useCallback(() => {
    setSelectedHistoryRunId(null);
    setWorkflowRun(null);
    setTestStep(null);
    testModeRef.current = null;
    viewingInstanceRef.current = false;
    pendingReplayRef.current = null;
    setHistoryOverlayOpen(false);
    setHistoryOverlayReady(false);
    if (selectedWorkflow) graphRef.current.setDef(workflowForGraph(selectedWorkflow));
  }, [selectedWorkflow]);

  useEffect(() => {
    // AC-2: Handle task query param to open a specific task's run
    const taskParam = searchParams.get("task");
    if (!taskParam) return;

    // Find the task in the conductor's managed tasks
    const task = conductorManaged.find((t: ManagedTask) => t.id === taskParam);
    if (task) {
      openConductorInstance(task);
    }
  }, [searchParams, conductorManaged, openConductorInstance]);

  // A drilled-in BEHAVIOUR layer (plan-gate-check, green-gate-status, ...)
  // has no WorkflowCore run behind it, so `workflowRun.runtime` is null and
  // every node used to draw with no state at all -- owner 2026-08-29, on the
  // plan-gate layer his task was parked on: "there is no indication anywhere
  // what the hell is going on."
  //
  // The verdicts were never missing, only unexposed. Each node of such a
  // layer IS a check with an answer, and GET /api/workflows/{id}/node-status
  // reports it per node for ONE task. So this needs a task in context: the
  // ?task= param the drill-in carries, else the conductor instance the
  // canvas is attached to. With no task there is no question to answer, and
  // the canvas correctly says nothing rather than inventing a state.
  // ONE driven task foregrounded on a catalog-wide board (task ce471e06).
  // The path comes from the run's OWN advance_task rows -- the same history
  // that IS the conductor's instance record -- never a guess off the step
  // order, and the attempt count from the setback rows written during a
  // dwell (flow_report_failure / advance_refused).
  const [runTrace, setRunTrace] = useState<{ traversedPath: string[]; attempts: number } | null>(null);
  const [catalogStatsOpen, setCatalogStatsOpen] = useState(false);
  const runTraceTaskId = searchParams.get("task") ?? workflowRun?.data.conductorTask?.id ?? null;
  useEffect(() => {
    if (!runTraceTaskId) { setRunTrace(null); return; }
    let cancelled = false;
    api.get<{ history?: Array<{ action?: string; details?: string }> }>(
      `/api/tasks/${encodeURIComponent(runTraceTaskId)}?project=${encodeURIComponent(project)}&scope=core`,
    ).then(({ history }) => {
      if (cancelled) return;
      const traversedPath: string[] = [];
      let attempts = 1;
      for (const row of history ?? []) {
        if (row.action === "advance_task") {
          const from = /from=([^;]+)/.exec(row.details ?? "")?.[1]?.trim();
          const to = /to=([^;]+)/.exec(row.details ?? "")?.[1]?.trim();
          if (!traversedPath.length && from) traversedPath.push(from);
          if (to) traversedPath.push(to);
          attempts = 1;
        } else if (row.action === "flow_report_failure" || row.action === "advance_refused") {
          attempts += 1;
        }
      }
      setRunTrace({ traversedPath, attempts });
    }).catch(() => setRunTrace(null));
    return () => { cancelled = true; };
  }, [runTraceTaskId, project]);
  // Run mode dims the rest of the catalog; the stats toggle folds it back on
  // WITHOUT leaving the run.
  const runView = useMemo<RunView | null>(() => (
    runTrace ? { runMode: !catalogStatsOpen, traversedPath: runTrace.traversedPath } : null
  ), [runTrace, catalogStatsOpen]);
  // Seconds since THIS task's last conductor transition. Never the drive
  // heartbeat's own signal age, which read as "RUN 0s / -4s" against 25
  // minutes of real motion.
  const runMotionSeconds = conductorManaged.find((t) => t.id === runTraceTaskId)?.activity?.task_motion_s ?? null;

  const nodeStatusTaskId = searchParams.get("task")
    ?? workflowRun?.data.conductorTask?.id
    ?? null;
  const nodeStatusLayerId = selectedWorkflow?.parent_id && selectedWorkflowId !== "validation"
    ? selectedWorkflowId
    : null;
  useEffect(() => {
    if (!nodeStatusLayerId || !nodeStatusTaskId) {
      setNodeVerdicts(null);
      return;
    }
    let cancelled = false;
    const load = () => {
      api.get<{ nodes: Array<{ id: string; state: string; reason?: string }> }>(
        `/api/workflows/${encodeURIComponent(nodeStatusLayerId)}/node-status`
        + `?project=${encodeURIComponent(project)}`
        + `&task_id=${encodeURIComponent(nodeStatusTaskId)}`,
      )
        .then((res) => {
          if (cancelled) return;
          const next: Record<string, NodeVerdict> = {};
          for (const node of res.nodes ?? []) {
            next[node.id] = {
              state: node.state as NodeVerdict["state"],
              reason: node.reason ?? "",
            };
          }
          setNodeVerdicts(Object.keys(next).length > 0 ? next : null);
        })
        .catch(() => {
          // A failed read is NOT a verdict. Clearing back to null draws the
          // layer plain rather than freezing a stale answer on the canvas.
          if (!cancelled) setNodeVerdicts(null);
        });
    };
    load();
    // A gate check answers on the same timescale as a task transition -- the
    // same 10s cadence this page already polls its definition on.
    const timer = window.setInterval(load, 10000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [project, nodeStatusLayerId, nodeStatusTaskId]);

  // The whole state-machine family's own version of validation's "reattach
  // after reload/navigation" effect below: land on ANY bot-family canvas
  // (not just the top "conductor" one) with a task genuinely in flight on
  // one of its steps right now, and its fill/clock should already be
  // playing -- no click needed -- the same way "Build and test" has always
  // auto-attached to its own active run (owner: "each step here should
  // have a playback mode... look at how we did it with build and test").
  // Skips when ?task= is already being handled above, so the two effects
  // never race onto different pills for the same canvas.
  useEffect(() => {
    if (!isStateMachineWorkflow || workflowRun || searchParams.get("task")) return;
    // A task parked at a gate is waiting for a PERSON, not working -- and
    // attaching to one sets viewingInstanceRef, which stops the definition
    // poll from re-applying live occupancy to the whole board. Measured on
    // 7.13.150 (task a928f3d5): opening /workflows pinned the canvas to a
    // task that had been awaiting review for 32 hours while 8 tasks were
    // driving through implement_tasks, none of them visible. Attach only to
    // work that is actually moving; with nothing moving, the board keeps
    // live occupancy, which is the honest whole-board view.
    const live = [...conductorRailTasks].reverse().find((task) =>
      task.status !== "done"
      && (task.activity?.state === "working" || task.activity?.state === "driving"));
    if (live) openConductorInstance(live);
  }, [isStateMachineWorkflow, workflowRun, searchParams, conductorRailTasks, openConductorInstance]);

  useEffect(() => {
    // Validation-only: this polls GET /api/workflows/runs/:id, a WorkflowCore
    // instance route that does not exist for a conductor task id, nor for
    // any conductor-linked child (they have no WorkflowCore run either).
    // The conductor's live state already comes from useConductorState's own
    // SSE push (conductorManaged above) -- reusing that here would be the
    // duplicate-source mistake this hook exists to prevent.
    if (isStateMachineWorkflow) return;
    if (!workflowRun || ["Complete", "Terminated"].includes(workflowRun.status)) return;
    let cancelled = false;
    const poll = window.setInterval(() => {
      fetchWorkflowRun(workflowRun.id).then((next) => {
        if (cancelled) return;
        const attach = next.runtime || next.status !== "Runnable"
          ? Promise.resolve(next)
          : fetchActiveWorkflowRun(project, selectedWorkflowId)
              .then(({ instanceId }) => instanceId.toLowerCase() === next.id.toLowerCase()
                ? next
                : fetchWorkflowRun(instanceId))
              .catch(() => next);
        return attach.then((truth) => {
          if (cancelled) return;
          setWorkflowRun(truth);
          setWorkflowRunError(null);
          const workflow = testWorkflowRef.current;
          const index = workflow?.steps.findIndex((step) =>
            step.id === truth.runtime?.currentStep) ?? -1;
          setTestStep(truth.runtime?.status === "running" && index >= 0 ? index : null);
          if (["Complete", "Terminated"].includes(truth.status)) void refreshRunHistory();
        });
      }).catch(() => {
        if (!cancelled) setWorkflowRunError("Connection interrupted");
      });
    }, 1000);
    return () => { cancelled = true; window.clearInterval(poll); };
  }, [project, selectedWorkflowId, workflowRun?.id, workflowRun?.status, refreshRunHistory]);

  const toggleDirectoryExpanded = useCallback((id: string) => {
    setExpandedDirectoryIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }, []);

  const setAndRememberDirectoryWidth = useCallback((width: number) => {
    const next = Math.max(DIRECTORY_MIN_PX, Math.min(DIRECTORY_MAX_PX, width));
    setDirectoryWidth(next);
    try { localStorage.setItem(DIRECTORY_WIDTH_KEY, String(next)); } catch { /* storage unavailable */ }
  }, []);

  const onDirectoryResizeDown = useCallback((ev: React.PointerEvent<HTMLDivElement>) => {
    if (!directoryOpen) return;
    ev.currentTarget.setPointerCapture(ev.pointerId);
    directoryResizeRef.current = { startX: ev.clientX, startWidth: directoryWidth };
  }, [directoryOpen, directoryWidth]);

  const onDirectoryResizeMove = useCallback((ev: React.PointerEvent<HTMLDivElement>) => {
    const drag = directoryResizeRef.current;
    if (!drag) return;
    setAndRememberDirectoryWidth(drag.startWidth + ev.clientX - drag.startX);
  }, [setAndRememberDirectoryWidth]);

  const onDirectoryResizeEnd = useCallback((ev: React.PointerEvent<HTMLDivElement>) => {
    directoryResizeRef.current = null;
    if (ev.currentTarget.hasPointerCapture(ev.pointerId)) ev.currentTarget.releasePointerCapture(ev.pointerId);
  }, []);

  const onDirectoryResizeKey = useCallback((ev: React.KeyboardEvent<HTMLDivElement>) => {
    if (!directoryOpen) return;
    let next = directoryWidth;
    if (ev.key === "ArrowLeft") next -= 16;
    else if (ev.key === "ArrowRight") next += 16;
    else if (ev.key === "Home") next = DIRECTORY_MIN_PX;
    else if (ev.key === "End") next = DIRECTORY_MAX_PX;
    else return;
    ev.preventDefault();
    setAndRememberDirectoryWidth(next);
  }, [directoryOpen, directoryWidth, setAndRememberDirectoryWidth]);

  // ---- the recorded flow: stored node runs + live movement (task 8fbd5cf0)
  // The canvas used to reverse-map task_history rows and guess progress from
  // a clock. It now READS what each node said at decision time
  // (GET /api/workflows/{id}/runs) and moves on the flow.node event the
  // recorder publishes -- no reload, and a concluded node is never recomputed.
  const refreshFlowRuns = useCallback(() => {
    const taskId = nodeStatusTaskId;
    const flowId = selectedWorkflow?.parent_id ? selectedWorkflow.parent_id : selectedWorkflowId;
    if (!taskId || !flowId) { setFlowRuns(null); return; }
    api.get<FlowRuns>(`/api/workflows/${encodeURIComponent(flowId)}/runs?task_id=${encodeURIComponent(taskId)}&project=${encodeURIComponent(project)}`)
      .then(setFlowRuns)
      .catch(() => setFlowRuns(null));
  }, [project, nodeStatusTaskId, selectedWorkflow, selectedWorkflowId]);

  useEffect(() => { refreshFlowRuns(); }, [refreshFlowRuns]);

  /** Play the token ALONG THE DRAWN WIRE from one node to the next. The
   * graph's own packet router already walks the routed polyline, so the
   * token travels the wire a person can see; re-rendering at the new node
   * would be the teleport this task forbids. */
  const animateTokenAlong = useCallback((from: string, to: string) => {
    if (!from || !to || from === to) return;
    graphRef.current.sendTransition(from, to);
  }, []);

  useEffect(() => {
    return subscribeStream(`/sse/work?project=${encodeURIComponent(project)}`, (frame) => {
      const ev = frame as { type?: string; task_id?: string; node_id?: string };
      if (ev?.type !== "flow.node") return;
      if (nodeStatusTaskId && ev.task_id && ev.task_id !== nodeStatusTaskId) return;
      animateTokenAlong(lastFlowNodeRef.current, String(ev.node_id || ""));
      lastFlowNodeRef.current = String(ev.node_id || "");
      refreshFlowRuns();
    });
  }, [project, nodeStatusTaskId, animateTokenAlong, refreshFlowRuns]);

  // Crisp at devicePixelRatio: backing store scaled, CSS size unscaled.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver(() => {
      const dpr = window.devicePixelRatio || 1;
      const w = canvas.clientWidth, h = canvas.clientHeight;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      canvas.getContext("2d")?.setTransform(dpr, 0, 0, dpr, 0, 0);
      graphRef.current.fit(w, h);
    });
    ro.observe(canvas);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    const ctx = canvas?.getContext("2d");
    if (!canvas || !ctx) return;
    let raf = 0;
    let last = performance.now();
    const frame = (now: number) => {
      const dt = now - last;
      last = now;
      graphRef.current.step(dt, now);
      let activeProgress: ActiveNodeProgress | null = null;
      const runtime = workflowRun?.runtime;
      if (isStateMachineWorkflow && runtime?.status === "running" && flowRuns?.progress) {
        // Task 8fbd5cf0 oracle: "the occupied node carries its OWN progress
        // fill... progress fills against TRUE WALL TIME over that node's
        // OWN historical duration... never a fabricated percentage." The
        // conductor/bot-family canvas has a REAL recorded source for this
        // now (flow_run_recorder.progress_source, the same one
        // conductorLivePhase already reads for the bottom rail) -- so the
        // NODE'S OWN fill reads it too, instead of the p95/average-duration
        // clock math below (which stays for the scripted "validation"
        // workflow's Build-and-test node, the one thing it was ever tuned
        // for; conductor never went through that path's own run history).
        const p = flowRuns.progress;
        const total = p.total ?? null;
        // done > total means the step is WEDGED past its own unit count.
        // The old Math.min(0.98, ...) cap painted that as a near-full bar,
        // which reads as "nearly finished"; the real ratio rides along so
        // the node can say OVERRUN instead.
        const ratio = total && total > 0 ? p.done / total : 0;
        activeProgress = {
          nodeId: runtime.currentStep,
          progress: Math.min(1, ratio),
          indeterminate: !(total && total > 0),
          elapsedSeconds: runMotionSeconds ?? (p.basis === "wall_time" ? p.done : 0),
          averageSeconds: total && total > 0 ? total : null,
          overrunRatio: ratio > 1 ? ratio : null,
          attempts: runTrace?.attempts ?? 1,
        };
      } else if (runtime?.status === "running" && runtime.startedAt && selectedWorkflow) {
        // A linked CHILD node (e.g. verify_green_state's "Build and test",
        // whose own steps are "build"/"test" from the external AOS engine)
        // never appears in selectedWorkflow.steps (the CONDUCTOR's own 10
        // steps) -- search the full connected catalog as a fallback so its
        // real average_duration_seconds is found instead of silently
        // falling through to the indeterminate wiggle every time (owner
        // 2026-08-26, screenshot showing "Build and test" stuck cycling at
        // ~23% after 4m51s of real elapsed time).
        const step = selectedWorkflow.steps.find((candidate) => candidate.id === runtime.currentStep)
          ?? workflows.flatMap((wf) => wf.steps).find((candidate) => candidate.id === runtime.currentStep);
        const elapsedSeconds = Math.max(0, (Date.now() - Date.parse(runtime.startedAt)) / 1000);
        // p95 of this step's real recent durations paces the bar when
        // there is enough history; the server's plain mean is the
        // fallback for a step still short on same-step samples, and the
        // indeterminate wiggle is the last resort with neither. p95 stays
        // scoped to the conductor's OWN run history (visibleRunHistory) --
        // a linked child step's timeline never appears there, so it
        // correctly returns null and pacing falls through to `step`
        // (now catalog-wide) above.
        const p95 = p95StepDurationSeconds(visibleRunHistory, runtime.currentStep);
        const pacing = p95 ?? step?.average_duration_seconds;
        activeProgress = {
          nodeId: runtime.currentStep,
          // NO SAWTOOTH. This used to be
          //     0.12 + ((elapsedSeconds % 18) / 18) * 0.68
          // for a step with no duration history: a pure wall-clock loop that
          // swept 12% -> 80% and snapped back every 18 SECONDS, forever,
          // while the step did nothing. Owner watching a task sit at
          // plan_gate, 2026-08-29: "the progress on plan gate has filled to
          // full like 15 times" and "it's not a progress, it's just an
          // animation loop ... it should never fill up and then cycle to
          // start again, unless the sub tasks in that flow step are actually
          // getting done." (The same wiggle drew the earlier "Build and test
          // stuck cycling at ~23% after 4m51s" report noted above.)
          //
          // With real pacing the bar still maths to full ONCE and stops at
          // 0.98. Without it there is no honest number to draw, so we draw
          // none: `indeterminate` tells the renderer to leave the body
          // unfilled, and the card's own "RUN 6m 49s" elapsed clock carries
          // the "this is running" signal, which is true.
          progress: Math.min(1, pacing && pacing > 0 ? elapsedSeconds / pacing : 0),
          indeterminate: !(pacing && pacing > 0),
          elapsedSeconds: runMotionSeconds ?? elapsedSeconds,
          averageSeconds: pacing && pacing > 0 ? pacing : null,
          overrunRatio: pacing && pacing > 0 && elapsedSeconds > pacing ? elapsedSeconds / pacing : null,
          attempts: runTrace?.attempts ?? 1,
        };
      } else if (testModeRef.current === "replay" && testStep !== null && selectedWorkflow) {
        const nodeId = testStep < 0
          ? "__start__"
          : testStep < selectedWorkflow.steps.length
            ? selectedWorkflow.steps[testStep].id
            : "__complete__";
        const elapsedMs = Math.max(0, now - replayStepStartedRef.current);
        const result = nodeId === "build" ? workflowRun?.data.build
          : nodeId === "test" ? workflowRun?.data.tests : null;
        const replayFinishedAtFailure = replayStoppedAt?.step === nodeId;
        // A step that failed and retried mid-replay (not the run's final
        // outcome) gets the SAME "failure" tone/label as a terminal failure
        // -- reusing the existing vocabulary rather than inventing a
        // separate "retry" visual, per the owner's ask that this stop
        // looking like a made-up, always-smooth climb.
        const midWalkFailure = !replayFinishedAtFailure && nodeId !== "__complete__"
          && replayEventStatusRef.current === "failed";
        const tone = replayFinishedAtFailure || midWalkFailure
          ? "failure"
          : nodeId === "__complete__"
          ? workflowRun?.data.passed ? "success" : "failure"
          : "active";
        const replayLabel = replayFinishedAtFailure
          ? result ? result.status.replace("_", " ").toUpperCase() : "FAILED"
          : midWalkFailure
          ? "FAILED"
          : nodeId === "__complete__"
          ? workflowRun?.data.passed ? "PASSED" : "FAILED"
          : "RUN";
        activeProgress = {
          nodeId,
          progress: replayFinishedAtFailure ? 1 : Math.min(0.98, elapsedMs / replayStepDurationRef.current),
          indeterminate: false,
          elapsedSeconds: elapsedMs / 1000,
          averageSeconds: null,
          label: replayLabel,
          tone,
        };
      }
      drawWorkflows(ctx, graphRef.current, canvas.clientWidth, canvas.clientHeight, now, selectedNodeId, activeProgress, effectiveNodeVerdicts, runView);
      raf = requestAnimationFrame(frame);
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [selectedNodeId, selectedWorkflow, workflowRun, testStep, replayStoppedAt, workflowRunHistory, workflows, effectiveNodeVerdicts, runView, runTrace, runMotionSeconds]);

  // Rehydrate the directory's own saved child order whenever the project
  // changes -- a client-side arrangement preference, same tier as node
  // positions and wire edits above.
  useEffect(() => {
    readJson<Record<string, string[]>>(childOrderKey(project), setChildOrder);
  }, [project]);

  /** A parent's children in the owner's saved drag order; anything not
   * (yet) ordered keeps its place from the API response, appended after
   * whatever IS ordered. An id from a stale saved order that no longer
   * exists in the live catalog is simply dropped, never rendered as a gap. */
  const orderedChildren = useCallback((parentId: string, children: WorkflowCatalogEntry[]) => {
    const saved = childOrder[parentId];
    if (!saved?.length) return children;
    const byId = new Map(children.map((child) => [child.id, child]));
    const ordered: WorkflowCatalogEntry[] = [];
    for (const id of saved) {
      const child = byId.get(id);
      if (child) { ordered.push(child); byId.delete(id); }
    }
    // Anything unordered (new since the save, or never dragged) keeps its
    // original relative order from the still-live `children` array.
    for (const child of children) if (byId.has(child.id)) ordered.push(child);
    return ordered;
  }, [childOrder]);

  const reorderChild = useCallback((
    parentId: string, allChildren: WorkflowCatalogEntry[], draggedId: string, targetId: string, before: boolean,
  ) => {
    if (draggedId === targetId) return;
    const current = orderedChildren(parentId, allChildren).map((child) => child.id);
    const from = current.indexOf(draggedId);
    if (from < 0) return;
    current.splice(from, 1);
    const to = current.indexOf(targetId);
    if (to < 0) return;
    current.splice(before ? to : to + 1, 0, draggedId);
    setChildOrder((prev) => {
      const next = { ...prev, [parentId]: current };
      writeJson(childOrderKey(project), next);
      return next;
    });
  }, [orderedChildren, project]);

  const persist = useCallback(() => {
    writeJson(positionsKey(project), graphRef.current.serializeOverrides());
  }, [project]);

  const persistWires = useCallback(() => {
    const saved = graphRef.current.wireEdits.serialize();
    writeJson(portsKey(project), saved.ports);
    writeJson(waypointsKey(project), saved.waypoints);
  }, [project]);

  const handleReset = useCallback(() => {
    graphRef.current.clearOverrides();
    const canvas = canvasRef.current;
    graphRef.current.fit(canvas?.clientWidth || 800, canvas?.clientHeight || 600, true);
    try {
      // Positions AND wire edits — a reset that left wires bent would only
      // half-work, which is worse than no escape hatch at all.
      localStorage.removeItem(positionsKey(project));
      localStorage.removeItem(portsKey(project));
      localStorage.removeItem(waypointsKey(project));
    } catch {
      // nothing to clean up if storage isn't available
    }
  }, [project]);

  // Escape lets go of the selected wire — the keyboard half of "click
  // empty space to deselect".
  useEffect(() => {
    const onKey = (ev: KeyboardEvent) => {
      if (ev.key === "Escape") graphRef.current.editor.selected = null;
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const onPointerDown = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) { dragRef.current = { ...IDLE_DRAG }; return; }
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    const base = { moved: false, lastX: ev.clientX, lastY: ev.clientY };

    // Order matters and is the whole disambiguation. The selected wire's
    // own handles win first (a waypoint and a port dot both sit ON top of
    // things that would otherwise claim the press), then nodes, then any
    // wire body, then empty space.
    const waypoint = g.waypointAtWorld(world.x, world.y);
    if (waypoint) {
      dragRef.current = {
        ...IDLE_DRAG, ...base, mode: "waypoint",
        wireKey: waypoint.key, waypointIndex: waypoint.index,
      };
      return;
    }
    const port = g.portAtWorld(world.x, world.y);
    if (port) {
      dragRef.current = {
        ...IDLE_DRAG, ...base, mode: "port",
        wireKey: port.key, wireEnd: port.end, nodeId: port.nodeId,
      };
      return;
    }
    // Body drag beats the node hit, but only on the wire the owner has
    // already selected — so it is always a deliberate second gesture, never
    // a wire stealing a press meant for the card underneath it.
    const selected = g.editor.selected;
    if (selected) {
      const grab = g.beginSegmentDrag(selected, world.x, world.y);
      if (grab) {
        dragRef.current = {
          ...IDLE_DRAG, ...base, mode: "segment", wireKey: selected, segment: grab,
        };
        return;
      }
    }
    const node = g.nodeAtWorld(world.x, world.y);
    if (node) {
      dragRef.current = {
        ...IDLE_DRAG, ...base, mode: "node", nodeId: node.id,
        offsetX: world.x - node.slot.x, offsetY: world.y - node.slot.y,
      };
      return;
    }
    // A press on a wire selects it outright — no drag threshold, because
    // selecting is what makes its handles appear to aim at next.
    const wire = g.wireAtWorld(world.x, world.y);
    g.editor.selected = wire ? wire.key : null;
    dragRef.current = { ...IDLE_DRAG, ...base, mode: wire ? "none" : "pan" };
  }, []);

  const onPointerMove = useCallback((ev: React.PointerEvent<HTMLCanvasElement>) => {
    const d = dragRef.current;
    if (d.mode === "none") return;
    const dx = ev.clientX - d.lastX, dy = ev.clientY - d.lastY;
    if (!d.moved && (Math.abs(dx) > DRAG_THRESHOLD_PX || Math.abs(dy) > DRAG_THRESHOLD_PX)) {
      d.moved = true;
      setGrabbing(true);
    }
    if (!d.moved) return;

    const g = graphRef.current;
    if (d.mode === "pan") {
      g.pan.x -= dx / g.zoom;
      g.pan.y -= dy / g.zoom;
      d.lastX = ev.clientX;
      d.lastY = ev.clientY;
      return;
    }

    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);

    if (d.mode === "node" && d.nodeId) {
      g.setOverride(d.nodeId, world.x - d.offsetX, world.y - d.offsetY);
    } else if (d.mode === "port" && d.wireKey && d.wireEnd) {
      // The wire re-routes live under the cursor: setPortFromWorld writes
      // the override every move and legs() reads it straight back.
      const wire = g.wire(d.wireKey);
      const slot = wire && g.slotForEnd(wire, d.wireEnd);
      if (slot) g.editor.setPortFromWorld(d.wireKey, d.wireEnd, slot, world.x, world.y);
    } else if (d.mode === "waypoint" && d.wireKey) {
      g.editor.moveWaypoint(d.wireKey, d.waypointIndex, world.x, world.y);
    } else if (d.mode === "segment" && d.segment) {
      // Perpendicular only — the run keeps its own axis, which is what
      // makes the path stay orthogonal while it moves.
      g.editor.moveSegment(d.segment, d.segment.axis === "x" ? world.x : world.y);
    }
    d.lastX = ev.clientX;
    d.lastY = ev.clientY;
  }, []);

  const onPointerUp = useCallback(() => {
    const d = dragRef.current;
    dragRef.current = { ...IDLE_DRAG };
    suppressCanvasClickRef.current = d.moved;
    if (grabbing) setGrabbing(false);
    if (!d.moved) return;
    if (d.mode === "node") { persist(); return; }
    if (d.mode === "waypoint" && d.wireKey) {
      // Dropped back onto the straight run between its neighbours, the
      // bend isn't bending anything — it removes itself rather than
      // lingering as an invisible handle. That IS the remove gesture.
      const g = graphRef.current;
      const wire = g.wire(d.wireKey);
      const pts = wire ? g.route(wire) : [];
      if (pts.length >= 2) {
        g.editor.pruneIfStraightened(d.wireKey, d.waypointIndex, pts[0], pts[pts.length - 1]);
      }
      // Settle the surviving bends onto clean rails before they are saved,
      // so a refresh reloads the path as drawn rather than the raw drag
      // coordinates behind it.
      g.settleWire(d.wireKey);
    }
    // A segment drag settles the same way: anchors it made redundant retire
    // themselves instead of lingering for a manual double-click.
    if (d.mode === "segment" && d.wireKey) graphRef.current.settleWire(d.wireKey);
    if (d.mode === "port" || d.mode === "waypoint" || d.mode === "segment") persistWires();
  }, [grabbing, persist, persistWires]);

  const onCanvasClick = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    if (suppressCanvasClickRef.current) {
      suppressCanvasClickRef.current = false;
      return;
    }
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const world = graphRef.current.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    const node = graphRef.current.nodeAtWorld(world.x, world.y);
    if (!node) {
      setStateDetailsOpen(false);
      return;
    }
    const linkedStep = selectedWorkflow?.steps.find(
      (step) => step.id === node.id,
    );
    // task 25b2a05c: no "validation" fallback -- verify_green_state's own
    // linked_workflow_id (verify-green-state-loop) is always real now.
    const linkedWorkflowId = linkedStep?.linked_workflow_id ?? null;
    if (linkedWorkflowId) {
      const linkedWorkflow = workflows.find(
        (workflow) => workflow.id === linkedWorkflowId,
      );
      if (linkedWorkflow) {
        setStateDetailsOpen(false);
        selectWorkflow(linkedWorkflow, [...workflowPath, {
          workflowId: selectedWorkflow?.id ?? "conductor",
          stepId: node.id,
        }]);
        return;
      }
    }
    setStateDetailsOrigin({
      x: Math.max(0, Math.min(100, ((ev.clientX - rect.left) / rect.width) * 100)),
      y: Math.max(0, Math.min(100, ((ev.clientY - rect.top) / rect.height) * 100)),
    });
    setSelectedNodeId(node.id);
    setStateDetailsOpen(false);
    window.requestAnimationFrame(() => window.requestAnimationFrame(() => setStateDetailsOpen(true)));
  }, [selectedWorkflow, selectWorkflow, workflowPath, workflows]);

  const closeStateDetails = useCallback(() => {
    setStateDetailsOpen(false);
  }, []);

  const returnToWorkflowOrigin = useCallback((pathIndex: number) => {
    const workflowOrigin = workflowPath[pathIndex];
    if (!workflowOrigin) return;
    const origin = workflows.find(
      (workflow) => workflow.id === workflowOrigin.workflowId,
    );
    if (!origin) return;
    const originStepId = workflowOrigin.stepId;
    selectWorkflow(origin, workflowPath.slice(0, pathIndex));
    setSelectedNodeId(originStepId);
  }, [selectWorkflow, workflowPath, workflows]);

  /** Double-click is the bend gesture: on a placed waypoint it removes it,
   * anywhere else along the selected wire it inserts one at the hop that
   * was clicked. */
  const onDoubleClick = useCallback((ev: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const world = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);

    const waypoint = g.waypointAtWorld(world.x, world.y);
    if (waypoint) {
      g.editor.removeWaypoint(waypoint.key, waypoint.index);
      persistWires();
      return;
    }
    const wire = g.wireAtWorld(world.x, world.y);
    if (!wire) return;
    g.editor.selected = wire.key;
    const leg = g.legAtWorld(wire, world.x, world.y);
    if (!leg) return;
    g.editor.insertWaypoint(wire.key, leg.leg, leg.point);
    g.settleWire(wire.key);
    persistWires();
  }, [persistWires]);

  const onWheel = useCallback((ev: React.WheelEvent<HTMLCanvasElement>) => {
    ev.preventDefault();
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const g = graphRef.current;
    const before = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    g.zoom = Math.max(0.35, Math.min(2.2, g.zoom * Math.exp(-ev.deltaY * 0.001)));
    const after = g.toWorld(ev.clientX - rect.left, ev.clientY - rect.top);
    g.pan.x += before.x - after.x;
    g.pan.y += before.y - after.y;
  }, []);

  return (
      <div className="relative flex h-full min-h-[420px] w-full overflow-hidden bg-[color:var(--surface-1)]">
        <aside
          aria-label="Workflow directory"
          style={{ width: directoryOpen ? directoryWidth : 44 }}
          className="shrink-0 bg-[color:var(--nav-bg)] transition-[width] overflow-hidden"
        >
          <div className="h-12 flex items-center justify-between border-b border-[color:var(--nav-line)] px-5">
            {directoryOpen && <span className="text-2xs uppercase tracking-[0.18em] text-[color:var(--nav-text)]">Project workflows</span>}
            <button
              type="button"
              aria-label={directoryOpen ? "Collapse workflow directory" : "Expand workflow directory"}
              aria-expanded={directoryOpen}
              onClick={() => setDirectoryOpen((open) => !open)}
              className="ml-auto h-7 w-7 text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)] hover:bg-[color:var(--nav-hover)]"
            >
              {directoryOpen ? "‹" : "›"}
            </button>
          </div>
          {directoryOpen && (
            <nav className="py-3" aria-label="Available workflows">
              {workflows.filter((workflow) => !workflow.parent_id).map((workflow) => {
                const children = workflows.filter((candidate) => candidate.parent_id === workflow.id);
                const selected = workflow.id === selectedWorkflowId;
                const childSelected = children.some((child) => child.id === selectedWorkflowId);
                const expanded = expandedDirectoryIds.has(workflow.id) || childSelected;
                return (
                  <div key={workflow.id}>
                    <button
                      data-workflow-id={workflow.id}
                      type="button"
                      aria-current={selected ? "page" : undefined}
                      onClick={() => selectWorkflow(workflow)}
                      className={`w-full flex items-center gap-2 pl-2 pr-5 py-2 text-left text-[13px] uppercase tracking-wider transition-colors ${selected ? "text-[color:var(--nav-active-text)] bg-[color:var(--nav-active-bg)] font-semibold" : "text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)] hover:bg-[color:var(--nav-hover)]"}`}
                    >
                      {children.length > 0 ? (
                        <span
                          role="button"
                          tabIndex={0}
                          aria-label={expanded ? `Collapse ${workflow.name}` : `Expand ${workflow.name}`}
                          onClick={(ev) => { ev.stopPropagation(); toggleDirectoryExpanded(workflow.id); }}
                          onKeyDown={(ev) => {
                            if (ev.key !== "Enter" && ev.key !== " ") return;
                            ev.preventDefault();
                            ev.stopPropagation();
                            toggleDirectoryExpanded(workflow.id);
                          }}
                          className="w-4 shrink-0 text-center text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)]"
                        >
                          {expanded ? "⌄" : "›"}
                        </span>
                      ) : (
                        <span className="w-4 shrink-0" aria-hidden="true" />
                      )}
                      <span className="flex-1">{workflow.name}</span>
                      <span className="text-2xs font-mono opacity-70">{workflow.steps.length}</span>
                    </button>
                    {expanded && orderedChildren(workflow.id, children).map((child) => {
                      const childSel = child.id === selectedWorkflowId;
                      const dragOver = dragOverChildId?.id === child.id ? dragOverChildId : null;
                      return (
                        <button
                          type="button"
                          key={child.id}
                          // A STABLE HOOK. These rail entries carried no id,
                          // no data attribute and no aria-label, and the
                          // remote-assist bridge resolves selectors with a
                          // plain document.querySelector -- CSS only, no text
                          // matching. So an agent could SEE this entry in a
                          // screenshot and had no way to click it (owner
                          // 2026-08-29: "CLICK ON IT AS A USER WOULD thats
                          // why you have remote assist").
                          data-workflow-id={child.id}
                          draggable
                          aria-current={childSel ? "page" : undefined}
                          aria-grabbed={draggedChildId === child.id}
                          onClick={() => selectWorkflow(child)}
                          onDragStart={(ev) => {
                            ev.dataTransfer.effectAllowed = "move";
                            setDraggedChildId(child.id);
                          }}
                          onDragEnd={() => { setDraggedChildId(null); setDragOverChildId(null); }}
                          onDragOver={(ev) => {
                            if (!draggedChildId || draggedChildId === child.id) return;
                            ev.preventDefault();
                            const rect = ev.currentTarget.getBoundingClientRect();
                            const before = ev.clientY - rect.top < rect.height / 2;
                            setDragOverChildId((prev) =>
                              prev?.id === child.id && prev.before === before ? prev : { id: child.id, before });
                          }}
                          onDragLeave={() => setDragOverChildId((prev) => (prev?.id === child.id ? null : prev))}
                          onDrop={(ev) => {
                            ev.preventDefault();
                            if (draggedChildId) {
                              reorderChild(workflow.id, children, draggedChildId,
                                child.id, dragOverChildId?.before ?? true);
                            }
                            setDraggedChildId(null);
                            setDragOverChildId(null);
                          }}
                          title="Drag to reorder"
                          className={`w-full flex cursor-grab items-start gap-2 pl-2 pr-5 py-2 text-left text-2xs uppercase tracking-wider transition-colors active:cursor-grabbing ${childSel ? "text-[color:var(--nav-active-text)] bg-[color:var(--nav-active-bg)] font-semibold" : "text-[color:var(--nav-text)] hover:text-[color:var(--nav-text-hi)] hover:bg-[color:var(--nav-hover)]"} ${dragOver?.before ? "border-t-2 border-[color:var(--accent-solid)]" : ""} ${dragOver && !dragOver.before ? "border-b-2 border-[color:var(--accent-solid)]" : ""} ${draggedChildId === child.id ? "opacity-40" : ""}`}
                        >
                          {/* Same w-4/shrink-0/text-center column as the parent row's own
                              disclosure chevron above -- one shared icon column, not an
                              extra forced indent past it. */}
                          <span aria-hidden="true" className="w-4 shrink-0 pt-px text-center text-[color:var(--nav-text)] opacity-50">⠿</span>
                          <span className="flex-1">{child.name}</span>
                          <span className="shrink-0 pt-px font-mono opacity-70">{child.steps.length}</span>
                        </button>
                      );
                    })}
                    {/* Ingestion paths (task c7edf4e2, epic cc9a44c8): only the
                        align_language catalog entry carries `coverage` — the
                        real write paths services.language_alignment has seen
                        register STE, so "every text writer registers ... and
                        cannot drift" is something a person can SEE, not just
                        a claim in a docstring. Shown only while this entry is
                        selected/expanded, same posture as its children above. */}
                    {selected && workflow.coverage && workflow.coverage.length > 0 && (
                      <div
                        aria-label="Ingestion paths"
                        className="mx-2 mb-2 mt-1 border border-[color:var(--nav-line)] bg-[color:var(--surface-1)] px-2 py-2"
                      >
                        <div className="px-1 pb-1 text-2xs uppercase tracking-wider text-[color:var(--nav-text)] opacity-70">
                          Ingestion paths
                        </div>
                        {workflow.coverage.map((row) => (
                          <div
                            key={row.path}
                            className={`flex items-center gap-2 px-1 py-1 text-2xs ${row.known ? "text-[color:var(--nav-text)]" : "text-amber-400"}`}
                            title={row.known ? "" : "This path has not been named in the coverage registry's label map yet."}
                          >
                            <span className="flex-1 truncate font-mono lowercase">{row.path}</span>
                            <span className="shrink-0 font-mono opacity-80">{row.count}</span>
                            <span className="shrink-0 font-mono opacity-60">
                              {row.last_seen ? new Date(row.last_seen).toLocaleString() : "never"}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                    {/* Knowledge health (task b1971944, epic 61821448): only the
                        knowledge_health catalog entry carries `metrics` — a
                        small table, same posture as align_language's own
                        "Ingestion paths" panel above, shown only while this
                        entry is selected. */}
                    {selected && workflow.metrics && (
                      <div
                        aria-label="Knowledge health metrics"
                        className="mx-2 mb-2 mt-1 border border-[color:var(--nav-line)] bg-[color:var(--surface-1)] px-2 py-2"
                      >
                        <div className="px-1 pb-1 text-2xs uppercase tracking-wider text-[color:var(--nav-text)] opacity-70">
                          Knowledge health
                        </div>
                        <table className="w-full text-2xs">
                          <tbody>
                            {([
                              ["Search feedback rate", workflow.metrics.search_feedback_rate],
                              ["Recall-to-use rate", workflow.metrics.recall_to_use_rate],
                              ["Median memory chars", workflow.metrics.median_memory_chars],
                              ["Evidence ratio", workflow.metrics.evidence_ratio],
                              ["Concepts grounded in code", workflow.metrics.concepts_grounded_in_code],
                              ["Modules with knowledge", workflow.metrics.modules_with_knowledge],
                              ["Rules with provenance", workflow.metrics.rules_with_provenance],
                              ["Open rule decisions", workflow.metrics.open_rule_decisions],
                            ] as const).map(([label, value]) => (
                              <tr key={label}>
                                <td className="px-1 py-0.5 text-[color:var(--nav-text)]">{label}</td>
                                <td className="px-1 py-0.5 text-right font-mono opacity-80">{value}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                );
              })}
            </nav>
          )}
        </aside>
        <div
          role="separator"
          aria-label="Resize workflow directory"
          aria-orientation="vertical"
          aria-valuemin={DIRECTORY_MIN_PX}
          aria-valuemax={DIRECTORY_MAX_PX}
          aria-valuenow={directoryWidth}
          tabIndex={directoryOpen ? 0 : -1}
          onPointerDown={onDirectoryResizeDown}
          onPointerMove={onDirectoryResizeMove}
          onPointerUp={onDirectoryResizeEnd}
          onPointerCancel={onDirectoryResizeEnd}
          onKeyDown={onDirectoryResizeKey}
          className={`relative z-10 w-1.5 shrink-0 border-l border-[color:var(--nav-line)] bg-[color:var(--surface-1)] outline-none ${directoryOpen ? "cursor-col-resize hover:bg-[color:var(--accent-solid)] focus:bg-[color:var(--accent-solid)]" : ""}`}
        />
        <div className="relative min-w-0 flex-1">
        <div className="absolute right-4 top-4 z-20 flex items-center gap-2">
          {workflowPath.length > 0 && (
            <nav
              aria-label="Workflow breadcrumb"
              className="flex items-center gap-2 border border-[color:var(--border-default)] bg-[color:var(--surface-2)] px-3 py-2 text-2xs uppercase tracking-wider"
            >
              {workflowPath.map((entry, index) => (
                <span key={`${entry.workflowId}:${index}`} className="flex items-center gap-2">
                  <button type="button" onClick={() => returnToWorkflowOrigin(index)}
                    className="text-[color:var(--accent-solid)] hover:underline">
                    {workflows.find((workflow) => workflow.id === entry.workflowId)?.name ?? entry.workflowId}
                  </button>
                  <span aria-hidden="true" className="text-[color:var(--text-muted)]">›</span>
                </span>
              ))}
              <span aria-current="page" className="text-[color:var(--text-primary)]">{selectedWorkflow?.name}</span>
            </nav>
          )}
          {selectedWorkflow?.id === "validation" && brainActivity && (
            <a
              href="/consolidation"
              title="Validation emits deterministic evidence; Brain indexing and reflective learning run asynchronously"
              className="border border-[color:var(--border-default)] bg-[color:var(--surface-2)] px-3 py-2 text-2xs uppercase tracking-wider text-[color:var(--text-secondary)] hover:border-[color:var(--accent-solid)] hover:text-[color:var(--text-primary)]"
            >
              After validation · Brain {brainActivity.stale ? "syncing" : "current"} · Learning {brainActivity.inFlight > 0 ? `${brainActivity.inFlight} active` : brainActivity.queueDepth > 0 ? `${brainActivity.queueDepth} queued` : brainActivity.running ? "idle" : "off"} ↗
            </a>
          )}
          {testStep !== null && selectedWorkflow && (
            <span className="rounded bg-[color:var(--surface-2)] px-3 py-2 text-2xs uppercase tracking-wider text-[color:var(--text-secondary)]">
              {replayStoppedAt
                ? `Replay stopped · ${replayStoppedAt.step.replace(/_/g, " ")} ${replayStoppedAt.status.replace(/_/g, " ")}`
                : testStep < 0
                ? testModeRef.current === "replay" ? `Replay ${replaySpeed}× · start` : "Testing · start"
                : testStep < selectedWorkflow.steps.length
                  ? `${testModeRef.current === "replay" ? `Replay ${replaySpeed}×` : "Testing"} · ${selectedWorkflow.steps[testStep].id.replace(/_/g, " ")}`
                : testModeRef.current === "replay" ? `Replay ${replaySpeed}× · complete` : "Flow complete"}
            </span>
          )}
          {selectedHistoryRun ? (
            <>
              <button
                type="button"
                onClick={leaveHistoricalReplay}
                title="Historical run selected · click to return to live workflow"
                className="rounded border border-[color:var(--accent-solid)] bg-[color:var(--surface-2)] px-3 py-2 text-2xs uppercase tracking-wider text-[color:var(--accent-solid)]"
              >
                Ran {new Date(selectedHistoryRun.createTime).toLocaleString()}
              </button>
              {workflowRun?.data.conductorTask?.id && (
                <Link
                  to={`/tasks/${workflowRun?.data.conductorTask?.id}`}
                  aria-label="Open task detail"
                  title="Open this task's own detail page"
                  className="rounded border border-[color:var(--border-strong)] bg-[color:var(--surface-2)] px-3 py-2 text-2xs uppercase tracking-wider text-[color:var(--text-primary)] hover:border-[color:var(--accent-solid)]"
                >
                  ↗ Task
                </Link>
              )}
            </>
          ) : selectedWorkflowId !== "conductor" && (
            <button
              type="button"
              onClick={runScriptedWorkflow}
              disabled={startingWorkflow}
              title="Execute this project's typed scripted workflow"
              className="rounded border border-[color:var(--border-strong)] bg-[color:var(--surface-2)] px-3 py-2 text-2xs uppercase tracking-wider text-[color:var(--text-primary)] hover:border-[color:var(--accent-solid)]"
            >
              {startingWorkflow ? "Starting…" : "Run workflow"}
            </button>
          )}
        </div>
        {(workflowRun || workflowRunError) && (
          <div className={`absolute left-4 top-4 z-20 ${conductorLivePhase ? "w-[420px]" : "max-w-[620px]"} border bg-[color:var(--surface-1)] px-3 py-2 text-xs ${
            workflowRunError ? "border-[color:var(--border-strong)] text-[color:var(--text-secondary)]"
            : isStateMachineWorkflow && workflowRun ? conductorRunTone(workflowRun)
            : workflowRun?.status === "Complete" ? (workflowRun.data.passed ? "border-emerald-500/60 text-emerald-300" : "border-red-500/60 text-red-300")
            : "border-[color:var(--border-strong)] text-[color:var(--text-secondary)]"
          }`}>
            <div>
              {workflowRunError || (isStateMachineWorkflow && workflowRun
                ? conductorRunSummary(workflowRun)
                : workflowRun?.status === "Complete"
                  ? `${selectedWorkflow?.name ?? "Workflow"} ${workflowRun.data.passed ? "passed" : "failed"} · build ${workflowRun.data.build?.status} · test ${workflowRun.data.tests?.status} · select a step for results`
                  : `Run ${workflowRun?.id.slice(0, 8)} · ${workflowRun?.runtime?.status === "running" ? "running" : "queued"} · ${workflowRun?.runtime?.currentStep || "waiting"}`)}
            </div>
          </div>
        )}
        {/* AC-6: the timeline content (SdlcProgress for a live run, the
            speed control for a done-instance replay) lives in its OWN bottom
            bar, separate from the box above which now holds only the
            run's title/identification -- previously both were crammed into
            the same top-left overlay (owner, live, 2026-08-25). Sits just
            above the pill-rail bar (bottom-10, that bar is h-10). */}
        {(conductorLivePhase || replayEventIndex !== null) && (
          <div className="absolute bottom-10 left-0 right-0 z-20 h-9 border-t border-white/10 bg-[#08090b] px-3 flex items-center gap-4">
            {conductorLivePhase && (
              <SdlcProgress
                step={workflowRun?.data.conductorTask?.workflowStep ?? undefined}
                phase={conductorLivePhase}
                status={workflowRun?.data.conductorTask?.status}
                activity={conductorLiveActivity}
                reduced={reduced}
                hideTokens
                workflow={selectedWorkflowId}
              />
            )}
            {replayEventIndex !== null && (
              <div className="flex items-center gap-3 ml-auto text-2xs uppercase tracking-wider text-[color:var(--text-secondary)]">
                <span>Replay {replaySpeed}×</span>
                <div role="group" aria-label="Playback speed" className="flex items-center gap-1">
                  {[30, 120, 240].map((speed) => (
                    <button
                      key={speed}
                      type="button"
                      onClick={() => setReplaySpeed(speed)}
                      aria-pressed={replaySpeed === speed}
                      className={`rounded border px-2 py-1 font-mono ${
                        replaySpeed === speed
                          ? "border-[color:var(--accent-solid)] text-[color:var(--accent-solid)]"
                          : "border-[color:var(--border-default)] text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]"
                      }`}
                    >
                      {speed}×
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
        {selectedHistoryRun && (
          <div
            aria-label="Historical workflow overlay"
            className={`pointer-events-none absolute inset-x-0 bottom-10 top-0 z-[9] border-2 bg-transparent ${historyOverlayReady ? selectedHistoryFrameTone : "border-transparent"}`}
          >
            <div
              aria-hidden="true"
              style={{ bottom: historyOverlayOpen ? "100%" : "32px" }}
              className="absolute inset-x-0 top-0 bg-[#111722]/95 transition-[bottom] duration-[1500ms] ease-in-out"
            />
            <div
              onTransitionEnd={(event) => {
                if (event.propertyName !== "bottom" || !historyOverlayOpen || historyOverlayReady) return;
                setHistoryOverlayReady(true);
                const run = pendingReplayRef.current;
                if (run) beginHistoricalReplay(run);
              }}
              style={{ bottom: historyOverlayOpen ? "calc(100% - 32px)" : "0px" }}
              className={`absolute inset-x-0 flex h-8 items-center gap-3 px-5 transition-[bottom,opacity] duration-[1500ms] ease-in-out ${historyOverlayReady ? "opacity-0" : "opacity-100"}`}
            >
              <span aria-hidden="true" className="h-px flex-1 bg-white" />
              <span className="border border-white/20 bg-black/50 px-4 py-2 font-mono text-xs uppercase tracking-widest text-white/80">
                Loading run · {new Date(selectedHistoryRun.createTime).toLocaleString()}
              </span>
              <span aria-hidden="true" className="h-px flex-1 bg-white" />
            </div>
          </div>
        )}
        <canvas
          ref={canvasRef}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onClick={onCanvasClick}
          onDoubleClick={onDoubleClick}
          onWheel={onWheel}
          className={`w-full h-full touch-none ${grabbing ? "cursor-grabbing" : "cursor-pointer"}`}
        />
        {runTrace ? (
          <button
            type="button"
            onClick={() => setCatalogStatsOpen((open) => !open)}
            className="absolute right-3 top-3 z-20 rounded border border-white/15 bg-[#08090b]/90 px-2 py-1 text-[10px] uppercase tracking-wide text-[color:var(--text-dim)] hover:text-white"
          >
            {catalogStatsOpen ? "Focus this run" : "Show catalog stats"}
          </button>
        ) : null}
        <div className="absolute bottom-0 left-0 right-0 z-10 h-10 border-t border-white/10 bg-[#08090b]">
          <div
            role="progressbar"
            aria-label="Workflow run history"
            aria-valuemin={0}
            aria-valuemax={RUN_RAIL_PILLS}
            aria-valuenow={railPills.filter((pill) => !pill.disabled).length}
            className="absolute bottom-0 left-0 right-[118px] top-0 flex items-center justify-between gap-[2px]"
          >
            {railPills.map((pill) => (
              <button
                key={pill.key}
                type="button"
                disabled={pill.disabled}
                aria-label={pill.ariaLabel}
                title={pill.title}
                onClick={pill.onClick}
                className={`h-9 min-w-1 max-w-[10px] flex-1 rounded-none transition-all duration-500 ${
                  pill.ringHighlighted
                    ? "ring-[3px] ring-inset ring-white shadow-[0_0_10px_2px_rgba(255,255,255,0.7)] scale-y-125 relative z-10"
                    : ""
                } ${pill.tone}`}
              />
            ))}
          </div>
          <button
            type="button"
            onClick={handleReset}
            className="absolute bottom-2 right-4 text-2xs uppercase tracking-wider font-mono px-2 py-0.5 rounded border border-[color:var(--border-default)] text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)] bg-[color:var(--surface-1)]"
          >
            reset layout
          </button>
        </div>
        {selectedNodeId && (
          <aside
            aria-label="State details"
            aria-hidden={!stateDetailsOpen}
            onTransitionEnd={(event) => {
              if (event.propertyName !== "transform") return;
              if (!stateDetailsOpen) {
                setSelectedNodeId(null);
                return;
              }
            }}
            style={{
              transformOrigin: `${stateDetailsOrigin.x}% ${stateDetailsOrigin.y}%`,
              transform: stateDetailsOpen ? "scale(1)" : "scale(0.04)",
              opacity: stateDetailsOpen ? 1 : 0,
            }}
            className="absolute inset-x-0 bottom-10 top-0 z-30 overflow-y-auto border border-[color:var(--border-strong)] bg-[color:var(--surface-1)] shadow-2xl transition-[transform,opacity] duration-[650ms] ease-[cubic-bezier(0.22,1,0.36,1)] will-change-transform motion-reduce:transition-none"
          >
            <header className="border-b border-[color:var(--border-default)] px-4 py-4">
              <div className="flex items-start justify-between gap-4">
                <div>
                  <div className="text-base font-semibold text-[color:var(--text-primary)]">{selectedStep?.purpose || selectedNodeId.replace(/^__|__$/g, "").replace(/_/g, " ")}</div>
                  <div className="mt-1 text-xs text-[color:var(--text-muted)]">
                    {selectedStepResult && selectedStepResult.status !== "pending"
                      ? `Last run  •  Exit ${selectedStepResult.exitCode}`
                      : selectedNodeId === "__start__" ? "Entry state" : selectedNodeId === "__complete__" ? "Terminal state" : "Not run yet"}
                  </div>
                </div>
                <div className="flex items-center gap-4">
                  {selectedStepResult && selectedStepResult.status !== "pending" && <span className={`border px-3 py-1 font-mono text-xs uppercase ${selectedStepResult.status === "passed" ? "border-emerald-500/60 text-emerald-300" : "border-red-500/60 text-red-300"}`}>{selectedStepResult.status}</span>}
                  <button type="button" aria-label="Close state details" onClick={closeStateDetails} className="text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]">×</button>
                </div>
              </div>
              {selectedNodeVerdict && (
                <div className={`mt-4 border px-3 py-3 ${
                  selectedNodeVerdict.state === "passed" ? "border-emerald-500/50 bg-emerald-950/20"
                    : selectedNodeVerdict.state === "refused" ? "border-red-500/50 bg-red-950/20"
                      : "border-[color:var(--border-default)] bg-[color:var(--surface-2)]"
                }`}>
                  <div className={`font-mono text-2xs uppercase tracking-wider ${
                    selectedNodeVerdict.state === "passed" ? "text-emerald-300"
                      : selectedNodeVerdict.state === "refused" ? "text-red-300"
                        : "text-[color:var(--text-muted)]"
                  }`}>
                    {selectedNodeVerdict.state.replace(/_/g, " ")}
                  </div>
                  {/* The reason is the whole point of the panel for a
                    * REFUSED node: the check already said why, and leaving
                    * that on the server is what made the layer unreadable. */}
                  <p className="mt-1 whitespace-pre-wrap text-sm text-[color:var(--text-secondary)]">
                    {selectedNodeVerdict.reason
                      || (selectedNodeVerdict.state === "passed"
                        ? "This check ran and is satisfied."
                        : selectedNodeVerdict.state === "not_reached"
                          ? "The codified layer already decided, so this state never ran."
                          : "This layer has no per-node check to report here.")}
                  </p>
                </div>
              )}
              {selectedStep && <div className="mt-4 grid grid-cols-3 gap-6">
                <div><div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)]">Timeout</div><div className="mt-1 text-base text-[color:var(--text-primary)]">{formatDuration(selectedStep.timeout_seconds)}</div></div>
                <div><div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)]">Avg duration</div><div className="mt-1 text-base text-[color:var(--text-primary)]">{formatDuration(selectedStep.average_duration_seconds)}</div></div>
                <div><div className="text-2xs uppercase tracking-wider text-[color:var(--text-label)]">Runs</div><div className="mt-1 text-base text-[color:var(--text-primary)]">{selectedStep.duration_sample_count || 0}</div></div>
              </div>}
              {selectedStep && selectedStepResult && ["failed", "timed_out"].includes(selectedStepResult.status) && (
                <div className="mt-4 border border-red-500/40 bg-red-950/20 px-3 py-3">
                  <div className="flex items-center justify-between gap-4">
                    <p className="text-sm text-red-300">{selectedStep.id.replace(/_/g, " ")} execution failed</p>
                    <div className="flex items-center gap-2">
                      <button type="button" onClick={copyFailureIds} className="border border-red-500/60 px-3 py-2 text-xs text-red-200 hover:bg-red-500/10">
                        {failureIdsCopied ? "Copied" : "Copy failure IDs"}
                      </button>
                      {fixTaskId ? (
                        <a href={`/tasks/${fixTaskId}`} className="border border-emerald-500/60 px-3 py-2 text-xs text-emerald-300 hover:bg-emerald-500/10">Fix queued · Open task →</a>
                      ) : (
                        <button type="button" onClick={queueAgentFix} disabled={requestingFix} className="border border-red-500/60 px-3 py-2 text-xs text-red-200 hover:bg-red-500/10 disabled:opacity-50">
                          {requestingFix ? "Queuing…" : "Ask PRISM agent to fix →"}
                        </button>
                      )}
                    </div>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-2xs text-red-200/80">
                    <span>instance_id: {workflowRun?.id}</span>
                    <span>step_id: {selectedStep.id}</span>
                  </div>
                </div>
              )}
              {fixRequestError && <p role="alert" className="mt-2 text-xs text-red-300">{fixRequestError}</p>}
            </header>
            <div className="text-sm">
              {selectedStep && <section>
                {selectedStep.authority && (
                  <div
                    role="note"
                    aria-label="Gate authority"
                    className="mx-4 mt-4 border border-amber-500/40 bg-amber-950/10 px-3 py-3 text-xs leading-relaxed text-amber-200/90"
                  >
                    <span className="mr-1 font-semibold uppercase tracking-wider text-amber-300">Who decides this gate</span>
                    <div className="mt-1">{selectedStep.authority}</div>
                  </div>
                )}
                <section aria-label="Step behavior">
                  {selectedStep.execution !== "scripted" && <div className="border border-[color:var(--border-default)] px-3 py-3 text-xs text-[color:var(--text-muted)]">{selectedStep.execution === "connected" ? "Connected to PRISM" : "Definition only"}</div>}
                  {selectedStep.execution === "scripted" && selectedStep.script_source && (
                    <section aria-label="Step script" className={`overflow-hidden border bg-[#0d0f12] ${selectedScriptFrame}`}>
                      <header className="flex h-10 items-center justify-between border-b border-[color:var(--border-default)] px-3">
                        <span className="truncate text-xs text-[color:var(--text-primary)]">Behavior</span>
                        <span className="text-2xs uppercase tracking-wider text-[color:var(--accent-sage-fg)]">Read only</span>
                      </header>
                      {scriptDiagnosticOpen && selectedFailureEvidence && (
                        <div role="alert" className="border-b border-red-500/60 bg-red-950/30 px-4 py-3">
                          <div className="flex items-center justify-between gap-4">
                            <span className="text-2xs uppercase tracking-wider text-red-300">Recorded failure</span>
                            <button type="button" onClick={() => setScriptDiagnosticOpen(false)} className="text-xs text-red-200 hover:text-white">Close ×</button>
                          </div>
                          <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap font-mono text-xs leading-5 text-red-100">{selectedFailureEvidence.lines.join("\n")}</pre>
                        </div>
                      )}
                      <div className="h-[max(420px,calc(100vh-470px))] min-h-0">
                        <Editor
                          key={`${selectedStep.id}:${selectedStepResult?.status ?? "unrun"}:${selectedFailureMarkerLine ?? 0}`}
                          height="100%"
                          language={selectedStep.script_language || "shell"}
                          value={selectedStep.script_source}
                          theme="vs-dark"
                          onMount={(editor, monaco) => {
                            if (!selectedFailureMarkerLine || !selectedFailureEvidence) return;
                            editor.createDecorationsCollection([{
                              range: new monaco.Range(selectedFailureMarkerLine, 1, selectedFailureMarkerLine, 1),
                              options: {
                                isWholeLine: true,
                                glyphMarginClassName: "workflow-script-error-glyph",
                                overviewRuler: { color: "#f87171", position: monaco.editor.OverviewRulerLane.Left },
                              },
                            }]);
                            editor.onMouseDown((event) => {
                              if (event.target.type !== monaco.editor.MouseTargetType.GUTTER_GLYPH_MARGIN) return;
                              if (event.target.position?.lineNumber !== selectedFailureMarkerLine) return;
                              setScriptDiagnosticOpen((open) => !open);
                            });
                            editor.revealLineInCenterIfOutsideViewport(selectedFailureMarkerLine);
                          }}
                          options={{ readOnly: true, domReadOnly: true, glyphMargin: true, minimap: { enabled: true }, fontSize: 15, lineHeight: 24, wordWrap: "on", scrollBeyondLastLine: false, automaticLayout: true, padding: { top: 16, bottom: 16 } }}
                        />
                      </div>
                    </section>
                  )}
                </section>
                <details className="border-t border-[color:var(--border-default)] px-4 pt-3 text-xs">
                  <summary className="cursor-pointer text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]">Technical metadata</summary>
                  <dl className="mt-3 grid max-h-64 grid-cols-2 gap-px overflow-auto bg-[color:var(--border-default)] xl:grid-cols-3">
                    {[
                      ["Input", selectedStep.input],
                      ["Output", selectedStep.output],
                      ["Success", selectedStep.validation || "Prior state approved"],
                      ["Runner", selectedStep.runner || "PRISM"],
                      ["Invoke", selectedStep.command],
                      ["Script", selectedStep.script_path || "Embedded"],
                      ["Directory", selectedStep.working_directory],
                      ["Depends", selectedStep.depends_on?.join(", ") || "None"],
                      ["Owner", selectedStep.persona_label],
                    ].map(([label, value]) => (
                      <div key={label} className="min-w-0 bg-[color:var(--surface-2)] p-3">
                        <dt className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)]">{label}</dt>
                        <dd className="mt-1 break-words font-mono text-[color:var(--text-secondary)]">{value || "—"}</dd>
                      </div>
                    ))}
                  </dl>
                </details>
              </section>}
            </div>
          </aside>
        )}
        </div>
      </div>
  );
}
