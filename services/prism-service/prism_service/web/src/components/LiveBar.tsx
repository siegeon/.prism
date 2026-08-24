/**
 * LiveBar — a minimal "is the conductor pulse alive" indicator.
 *
 * Mounted ONCE in App.tsx between <PageHeader> and the routed content, but
 * scoped to ACTIVITY-context routes only (owner 2026-07-14: "the task live
 * bar is showing on the understand view, that does not work for me, we are
 * not in that context") — Dashboard, Tasks, Conductor. Knowledge and
 * learning-loop surfaces stay free of task chrome.
 *
 * SIMPLIFIED 2026-08-24 (task d9f082fe follow-up, owner live): this used to
 * render its own full task list (working/gated/inflow rows, one <Link> per
 * managed task, expand-in-place gate evidence) stacked above TasksPage's own
 * Work table — on /tasks that read as a second, competing work queue full of
 * the SAME tasks the table below already lists. Owner: "that live panel is
 * odd to me... I should not need the queue[/]metadata on that panel any
 * longer it shows up elsewhere." Now it is just a status dot + label: green
 * and pulsing while at least one claimed task is genuinely being driven
 * right now, muted otherwise. Every previous row-level detail (per-task
 * chrome, gate evidence, slice counts) lives on TasksPage.tsx and
 * ConductorPage.tsx, which read this exact same shared state.
 */
import { useLocation } from "react-router-dom";
import { useProject } from "@/lib/project";
import { useConductorState } from "@/lib/useConductorState";

// Activity-context routes where the conductor pulse belongs. Everything
// else (Explore/Understand/Sessions/Learning/Settings) renders no bar.
const ACTIVITY_ROUTES = ["/", "/tasks", "/conductor"];

function inActivityContext(pathname: string): boolean {
  return ACTIVITY_ROUTES.some(
    (r) => pathname === r || (r !== "/" && pathname.startsWith(r + "/")),
  ) || pathname.startsWith("/tasks");
}

export default function LiveBar() {
  const [project] = useProject();
  const { pathname } = useLocation();
  // Single resolved source (task 40c29b83): the fetch, the polled/observed
  // guard, the heartbeat clock, the SSE push refresh, and the staleness
  // sweep all live in the shared hook — ConductorPage/TasksPage read the
  // identical state, so this dot can never contradict either surface.
  const { managed, polled } = useConductorState(project);

  // "working" OR "driving" (task e3b7ebf6): a heartbeat-attributed step past
  // both the transition and session-quiet windows is genuinely being driven
  // too. Gated on `claimed` (task 2dfa94bd's fix, LiveBar's own blast
  // radius) — an unclaimed in_progress task must never paint this dot green,
  // matching /conductor's honest "NOT CLAIMED" tile for the same task.
  const isLive = managed.some(
    (m) => m.claimed && (m.activity?.state === "working" || m.activity?.state === "driving"),
  );
  // Honest states: LIVE (something is actually moving) > IDLE (queue quiet).
  // "loading" precedes both — until `polled` flips we have NOT observed the
  // queue, so claiming it is quiet would be a guess, not a fact.
  const state: "loading" | "live" | "idle" = !polled ? "loading" : isLive ? "live" : "idle";
  const tint = {
    loading: { bg: "var(--surface-1)", ring: "var(--border-subtle)", fg: "var(--text-muted)" },
    live: { bg: "var(--accent-sage-bg)", ring: "var(--accent-sage-ring)", fg: "var(--accent-sage-fg)" },
    idle: { bg: "var(--surface-1)", ring: "var(--border-subtle)", fg: "var(--text-muted)" },
  }[state];
  const stateLabel = state === "loading" ? "Connecting…" : state === "live" ? "Live" : "Idle";

  // Task chrome only where tasks are the context.
  if (!inActivityContext(pathname)) return null;

  return (
    <div className="px-6 pt-4 shrink-0">
      <div
        className="inline-flex items-center gap-2 rounded-lg border px-4 py-2.5 text-sm"
        role="status"
        style={{ borderColor: tint.ring, background: tint.bg }}
        title={stateLabel}
      >
        <span
          className={"h-2.5 w-2.5 rounded-full " + (state === "live" ? "animate-pulse" : "")}
          style={{ background: tint.fg }}
        />
        <b className="text-2xs uppercase tracking-wider" style={{ color: tint.fg }}>
          {stateLabel}
        </b>
      </div>
    </div>
  );
}
