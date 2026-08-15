// Single source for the "we can't see it" label decision (task 0f090a6c).
//
// Round-2 follow-up on task e9625a4d ("signal loss reads differently"),
// which scoped its honest-silence wording to 'adrift' rows only. A claimed
// task classified 'stalled' whose worker has gone genuinely dark is the
// SAME unobservable-worker condition and must render the SAME neutral,
// non-alarm wording — never the alarm-toned "no active driver"/"idle".
//
// Consumed by ConductorPage.tsx (tile pill), SdlcProgress.tsx (ACTIVITY_META
// stateLabel) and StepRail.tsx (Implementation tab current-step row) so a
// future classification tweak cannot re-diverge into three independently
// maintained copies (mx-d412e0: the same useConductorState single-source-
// hook precedent, applied here to a presentation decision instead of data
// fetching).
//
// Elapsed time reads activity.report_signal_age_s — the REAL
// drive_heartbeat age the server computes (api/conductor.py
// _with_report_signal) — NEVER activity.task_motion_s (step-transition
// recency), which is a different clock that can read low/fresh while the
// real heartbeat is null or ancient. That fallback was the label's
// internal-inconsistency bug this module exists to close.

export type ActivityLike = {
  state?: string;
  report_signal_lost?: boolean;
  report_signal_age_s?: number | null;
} | null | undefined;

export type ActivityLabel = { label: string; tone: string; lost: boolean };

// The two claimed-but-unobservable classifications. Genuinely quiet-but-
// visible states (working/driving/paused/awaiting_gate) never qualify —
// they carry their own honest wording at the call site.
const SIGNAL_LOST_STATES = new Set(["adrift", "stalled"]);

export function fmtSignalAge(s?: number | null): string {
  if (s == null) return "";
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

/** True exactly for the claimed-but-unobservable "we can't see it" case —
 * activity.state is adrift OR stalled AND the server-computed
 * report_signal_lost flag is set. Never a client-invented cutoff. */
export function isSignalLost(activity: ActivityLike): boolean {
  const state = (activity?.state ?? "").toLowerCase();
  return SIGNAL_LOST_STATES.has(state) && activity?.report_signal_lost === true;
}

/** THE single decision every surface calls for the we-can't-see-it label.
 * Returns lost:false (empty label/tone) for every other state so the
 * caller falls through to its own normal-state wording unchanged. */
export function activityLabel(activity: ActivityLike): ActivityLabel {
  if (isSignalLost(activity)) {
    const age = fmtSignalAge(activity?.report_signal_age_s);
    return {
      label: `we can't see it${age ? ` · last report ${age} ago` : ""}`,
      tone: "slate",
      lost: true,
    };
  }
  return { label: "", tone: "", lost: false };
}
