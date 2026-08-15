// Live per-turn token BURN-RATE graph for the conductor tile (the "work
// getting done" panel). One bar per recent assistant turn, height ∝ that
// turn's tok/s; the array is the live tail off the on-disk transcript, so the
// bars FLOW right as new turns land on each 5s poll. Deliberately the ONLY
// place token effort is shown on the tile — the cumulative number lives
// nowhere else on the card, so the graph (rate) and nothing else (no integral
// readout) means zero duplication.
import { motion } from "motion/react";
import type { TokenTurn } from "./SdlcProgress";
import { fmtTokens } from "@/lib/format";

// tok/s rate label. v6.7.23: per-turn tokens now include cache reads, so a
// single cache-heavy turn can burn millions (even billions) per second —
// needs the M and B tiers too (mirrors lib/format.ts fmtTokens tiers).
function fmtRate(n: number): string {
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return `${Math.round(n)}`;
}

function mmss(s: number): string {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

export default function TokenTurns({
  turns,
  total,
  live,
  reduced,
  tokens_source,
  state,
  session_quiet_s,
}: {
  turns?: TokenTurn[];
  total?: number;
  live?: boolean;
  reduced?: boolean | null;
  // 'linked' = authoritative per-task series; 'wallclock' = project-wide
  // approximate fallback; 'unattributed' = a CONTESTED session (linked to
  // 2+ tasks) with no agent_runs drive evidence for THIS task (task
  // a91f1d11) — render dimmed + labelled so the number is never presented
  // as authoritative per-task truth.
  tokens_source?: "linked" | "wallclock" | "unattributed";
  // Honest work state (conductor_service.activity_for). When NOT "working" the
  // burn is real spend but NOT this task's progress — dim it + relabel so it
  // can't masquerade as work getting done here.
  state?: string;
  session_quiet_s?: number | null;
}) {
  const series = turns ?? [];
  const count = total ?? series.length;
  const peak = series.reduce((m, t) => Math.max(m, t.tok_s), 0) || 1;
  // Index of the window-peak bar so it can be visually MARKED (not just a
  // scalar readout) — a teal cap makes the burst legible at a glance.
  const peakIdx = series.length
    ? series.reduce((mi, t, i) => (t.tok_s > series[mi].tok_s ? i : mi), 0)
    : -1;
  const latest = series.length ? series[series.length - 1] : null;
  const approximate = tokens_source === "wallclock";
  // Contested session, no drive evidence for THIS task — never render as an
  // ordinary empty 'linked' state (indistinguishable from "hasn't started").
  const unattributed = tokens_source === "unattributed";
  // The burn belongs to THIS task only while working. EVERY other state
  // (paused/adrift/stalled/awaiting_gate) is real spend but NOT this task's
  // progress ⇒ dim + shrink it and say so honestly, so a borrowed orchestrator
  // burn can't masquerade as work getting done on this tile.
  const st = (state ?? "").toLowerCase();
  const working = st === "working";
  const quietClock = session_quiet_s != null ? mmss(session_quiet_s) : "";
  const heading =
    st === "paused" ? "linked session · not this task"
    // "not this task" was the same lie the pill told: on `adrift` a driver IS
    // mid-step on this task. Stay honest about ATTRIBUTION (we can't prove which
    // step each token bought) without denying the work (owner 2026-07-21).
    : st === "adrift" ? "driver active · linked session burn"
    : st === "stalled" ? `no active driver${quietClock ? ` · quiet ${quietClock}` : ""}`
    : st === "awaiting_gate" ? "awaiting review · paused here"
    : unattributed ? "shared session · not this task's drive"
    : approximate ? "project activity (approximate)"
    : "burn · tok/s by turn";
  // Only a task actively being WORKED gets the full-strength graph.
  const dimmed = approximate || unattributed || (st !== "" && !working);

  return (
    <div
      className="flex flex-col gap-1.5 h-full"
      style={dimmed ? { opacity: 0.4 } : undefined}
      title={heading}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-2xs uppercase tracking-[0.12em] font-mono text-[color:var(--text-muted)] opacity-70">
          {heading}
        </span>
        <span className="flex items-center gap-1 text-2xs font-mono tabular-nums text-[color:var(--text-muted)]">
          {live && (
            <motion.span
              className="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: "var(--accent-amber-fg)" }}
              animate={!reduced ? { opacity: [1, 0.25, 1] } : { opacity: 1 }}
              transition={!reduced ? { duration: 1.1, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
            />
          )}
          <span className="text-[color:var(--text-secondary)]">{count}</span> turns
        </span>
      </div>

      {/* Bar field — flex row, newest on the right. Each bar tweens its height
          between polls so the field flows as the live tail advances. */}
      <div className={`relative flex-1 ${working ? "min-h-[56px]" : "min-h-[30px]"} flex items-end gap-[2px] rounded bg-[color:var(--surface-2)]/40 px-1.5 pt-1.5 pb-1 overflow-hidden`}>
        {series.length === 0 ? (
          <span className="absolute inset-0 grid place-items-center text-2xs font-mono opacity-30 italic">
            awaiting first turn…
          </span>
        ) : (
          series.map((t, i) => {
            const isLast = i === series.length - 1;
            const isPeak = i === peakIdx;
            // Log-scale height: with all four usage fields counted, a
            // cache-heavy turn can run ~1000x a plain generation turn — a
            // linear tok_s/peak normalization pins peak and flattens every
            // other bar to the floor. log1p keeps both cache spikes AND
            // ordinary turns readable; exact values stay in the tooltip.
            const h = Math.max(0.06, Math.log1p(t.tok_s) / Math.log1p(peak));
            return (
              <motion.div
                key={i}
                className="flex-1 min-w-[2px] rounded-t-sm"
                style={{
                  transformOrigin: "bottom",
                  background: isLast ? "var(--accent-amber-fg)" : "var(--accent-amber-bg)",
                  // Peak bar gets a teal cap so the window maximum is MARKED,
                  // not merely printed as a scalar in the readout below.
                  boxShadow: isPeak
                    ? "inset 0 3px 0 var(--accent-teal-fg), inset 0 0 0 1px var(--accent-teal-ring)"
                    : isLast
                    ? "0 0 6px var(--accent-amber-fg)"
                    : "inset 0 0 0 1px var(--accent-amber-ring)",
                  opacity: isLast ? 1 : 0.55 + 0.4 * (i / series.length),
                }}
                title={`${fmtRate(t.tok_s)} tok/s · ${fmtTokens(t.out)} tok / ${t.dt_s}s${isPeak ? " · window peak" : ""}`}
                initial={false}
                animate={{ height: `${h * 100}%` }}
                transition={{ duration: reduced ? 0 : 0.4, ease: [0.16, 1, 0.3, 1] }}
              />
            );
          })
        )}
        {/* live pulse glow over the newest bar while the task is being worked */}
        {!reduced && live && latest && (
          <motion.span
            className="pointer-events-none absolute bottom-1 right-1.5 h-1 w-1 rounded-full"
            style={{ background: "var(--accent-amber-fg)" }}
            animate={{ opacity: [0.9, 0.2, 0.9], scale: [1, 1.6, 1] }}
            transition={{ duration: 1.3, repeat: Infinity, ease: "easeInOut" }}
          />
        )}
      </div>

      {/* Single rate readout — the live (latest-turn) burn + the window peak.
          The ONLY numeric token figures on the tile. */}
      <div className="flex items-baseline justify-between text-2xs font-mono tabular-nums text-[color:var(--text-muted)]">
        <span>
          <span className="text-[color:var(--accent-amber-fg)]">{latest ? fmtRate(latest.tok_s) : "—"}</span> tok/s now
        </span>
        <span className="opacity-60">peak {fmtRate(peak)}</span>
      </div>
    </div>
  );
}
