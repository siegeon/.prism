// Way 1 — task ACTIVITY Gantt (bar-based, graphical, click-to-drill).
// Replaces the flat "sessions" list as the way to see WHAT HAPPENED on a task:
// real work sessions render as wall-time bars on a shared time axis; gate
// decisions render as honesty markers (real-verifier vs override) on their own
// lane. Synthetic gate-actor labels are NEVER drawn as bare session rows — they
// ARE the gate markers. Same bar visual language as the TokenTurns burn graph.
import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { api } from "@/lib/api";
import { EvidenceGallery, type EvidenceItem } from "@/components/EvidenceGallery";

export type GanttLane = {
  session_id: string;
  start: number; // epoch seconds
  end: number;
  duration_s: number;
  skills: number;
  live: boolean;
};
// One RECEIPT-captured artifact (task 25a25d84) — screenshot/video re-pointed
// at the whitelisted /evidence/<file> route, or the verbatim source of a
// pytest-pinned assertion. `path` is a servable URL for screenshot/video,
// or the pytest node id for assertion_source.
export type GateArtifact = { kind: string; path: string; provenance?: Record<string, unknown> };
export type GateAssertion = { id: string; source: string };
// GET /api/tasks/:id/gate_evidence — the newest EvidenceReceipt projected for
// a gate card. Empty artifacts+assertions is an HONEST "nothing captured",
// never inferred from the driving agent's hand-attached proof markdown.
export type GateEvidence = {
  artifacts: GateArtifact[];
  captured_by?: string;
  captured_at?: string;
  build?: string;
  tree_sha?: string;
  assertions: GateAssertion[];
};

export type GanttGate = {
  gate: string; // "red" | "green" | "gate"
  ts: number;
  actor: string;
  override: boolean;
  verified: boolean;
  proof: string;
  reason: string;
  // The RECEIPT-captured evidence for this task's latest run (task 25a25d84)
  // — populated client-side via useGateEvidence, not by the timeline API;
  // optional so existing timeline payloads (no evidence fetched yet) still
  // satisfy the type.
  evidence?: GateEvidence | null;
};
export type Timeline = {
  window: { start: number; end: number };
  lanes: GanttLane[];
  gates: GanttGate[];
};

function clockHM(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}
function fmtDur(s: number): string {
  if (s >= 3600) return `${(s / 3600).toFixed(1)}h`;
  if (s >= 60) return `${Math.round(s / 60)}m`;
  return `${Math.round(s)}s`;
}
function shortId(id: string): string {
  return id.length > 12 ? `${id.slice(0, 8)}…` : id;
}

// Fetch the task's newest EvidenceReceipt, projected for a gate card. Shared
// by StepRail and LiveBar so "click a gate chip -> see the captured evidence"
// reads off ONE fetch path (GET /api/tasks/:id/gate_evidence) everywhere.
export function useGateEvidence(taskId?: string, project = "default"): {
  data: GateEvidence | null; loading: boolean; error: boolean;
} {
  const [data, setData] = useState<GateEvidence | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => {
    if (!taskId) { setData(null); setLoading(false); setError(false); return; }
    let live = true;
    setLoading(true);
    setError(false);
    api.get<GateEvidence>(`/api/tasks/${taskId}/gate_evidence?project=${project}`)
      .then((d) => { if (live) { setData(d); setLoading(false); } })
      .catch(() => { if (live) { setData(null); setError(true); setLoading(false); } });
    return () => { live = false; };
  }, [taskId, project]);
  return { data, loading, error };
}

function fmtWhen(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

// The evidence block a gate chip expands into (task 25a25d84): the RECEIPT's
// own captured screenshot/video (existing EvidenceGallery lightbox — never a
// bare <img>/<a> to the raw file), verbatim per-test assertion source for a
// proof_type=test gate, and a provenance line. When none of the three
// modalities exist this reads HONESTLY as uncaptured — never silently blank,
// never borrowing the driving agent's hand-attached proof markdown as if it
// were system-captured evidence.
export function GateEvidenceBlock({ taskId, project = "default", proofType }: {
  taskId?: string; project?: string; proofType?: string;
}) {
  const { data, loading, error } = useGateEvidence(taskId, project);
  if (!taskId) return null;
  const media: EvidenceItem[] = (data?.artifacts ?? [])
    .filter((a) => a.kind === "screenshot" || a.kind === "video")
    .map((a) => ({
      url: a.path,
      name: a.path.split("/").pop()?.split("?")[0] || a.kind,
      kind: a.kind === "video" ? "video" : "image",
      caption: a.kind === "video" ? "walkthrough" : "screenshot",
    }));
  const assertions = data?.assertions ?? [];
  const hasProvenance = !!(data?.captured_by || data?.captured_at);
  const nothingCaptured = !loading && !error && media.length === 0 && assertions.length === 0;

  return (
    <div className="space-y-2.5">
      {loading && (
        <div className="text-2xs opacity-60">loading captured evidence…</div>
      )}
      {error && (
        <div className="text-2xs" style={{ color: "var(--accent-amber-fg)" }}>
          could not load the receipt's captured evidence
        </div>
      )}
      {media.length > 0 && <EvidenceGallery items={media} thumb="md" />}
      {assertions.length > 0 && (
        <div className="space-y-1.5">
          <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
            verbatim assertion source
          </div>
          {assertions.map((a, i) => (
            <div key={`${a.id}-${i}`} className="rounded-md border border-[color:var(--border-default)] p-2 overflow-x-auto">
              <div className="font-mono text-2xs mb-1 break-all" style={{ color: "var(--accent-teal-fg)" }}>{a.id}</div>
              <pre className="text-2xs whitespace-pre-wrap font-mono leading-relaxed" style={{ color: "var(--text-secondary)" }}>{a.source}</pre>
            </div>
          ))}
        </div>
      )}
      {hasProvenance && (
        <div className="text-2xs font-mono" style={{ color: "var(--text-muted)" }}>
          captured by {data?.captured_by || "—"}
          {data?.captured_at ? ` · ${fmtWhen(data.captured_at)}` : ""}
          {data?.build ? ` · build ${data.build}` : ""}
          {data?.tree_sha ? ` · tree ${data.tree_sha.slice(0, 10)}` : ""}
        </div>
      )}
      {nothingCaptured && (
        <div className="text-2xs rounded-md border border-dashed border-[color:var(--border-default)] px-2.5 py-2 leading-relaxed" style={{ color: "var(--accent-amber-fg)" }}>
          ⚠ no captured evidence — not evidence-backed
          {/^test$/i.test(proofType ?? "") ? " (this is a test-proof gate — see the Tests tab for its oracle)" : ""}
        </div>
      )}
    </div>
  );
}

export default function TaskActivityGantt({
  timeline, reduced, taskId, project,
}: {
  timeline: Timeline; reduced?: boolean | null;
  // Optional: when given, a drilled-in gate marker also shows its RECEIPT's
  // captured evidence (task 25a25d84). Omitted by callers that don't have a
  // task in scope — the drill panel then keeps its prior receipt-only view.
  taskId?: string; project?: string;
}) {
  const [open, setOpen] = useState<string | null>(null);
  const { start, end } = timeline.window;
  const span = Math.max(1, end - start);
  const pct = (ts: number) =>
    Math.max(0, Math.min(100, ((ts - start) / span) * 100));

  // axis ticks at 0/25/50/75/100% of the window
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => start + f * span);

  if (timeline.lanes.length === 0 && timeline.gates.length === 0) {
    return (
      <p className="text-[12px] opacity-40 italic py-3">
        No activity recorded yet.
      </p>
    );
  }

  return (
    <div className="mt-2 select-none">
      {/* time axis */}
      <div className="relative h-4 ml-[120px] border-b border-[color:var(--border-default)]/40">
        {ticks.map((t, i) => (
          <span
            key={i}
            className="absolute -translate-x-1/2 text-2xs font-mono text-[color:var(--text-muted)] opacity-60"
            style={{ left: `${pct(t)}%` }}
          >
            {i === ticks.length - 1 ? "now" : clockHM(t)}
          </span>
        ))}
      </div>

      {/* session lanes */}
      {timeline.lanes.map((l) => {
        const left = pct(l.start);
        const width = Math.max(1.5, pct(l.end) - left);
        return (
          <div key={l.session_id} className="flex items-center h-11 gap-2">
            <button
              onClick={() => setOpen(open === l.session_id ? null : l.session_id)}
              className="w-[112px] shrink-0 text-right font-mono text-2xs text-[color:var(--text-secondary)] truncate hover:opacity-100 opacity-80"
              title={l.session_id}
            >
              {shortId(l.session_id)}
            </button>
            <div className="relative flex-1 h-7">
              <motion.div
                className="absolute top-1/2 -translate-y-1/2 h-5 rounded-sm cursor-pointer"
                style={{
                  left: `${left}%`, width: `${width}%`,
                  background: l.live
                    ? "var(--accent-amber-fg)"
                    : "var(--accent-amber-bg)",
                  boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)",
                }}
                title={`${l.session_id} · ${fmtDur(l.duration_s)} · ${l.skills} skills${l.live ? " · live" : ""}`}
                onClick={() => setOpen(open === l.session_id ? null : l.session_id)}
                initial={false}
                animate={l.live && !reduced
                  ? { opacity: [1, 0.55, 1] } : { opacity: 1 }}
                transition={l.live && !reduced
                  ? { duration: 1.4, repeat: Infinity, ease: "easeInOut" } : { duration: 0.2 }}
              />
            </div>
            <span className="w-[64px] shrink-0 text-2xs font-mono text-[color:var(--text-muted)] tabular-nums">
              {fmtDur(l.duration_s)}{l.skills ? ` · ${l.skills}sk` : ""}
            </span>
          </div>
        );
      })}

      {/* gate marker lane */}
      <div className="flex items-center h-11 gap-2 mt-1">
        <span className="w-[112px] shrink-0 text-right font-mono text-2xs text-[color:var(--text-muted)] uppercase tracking-wider">
          gates
        </span>
        <div className="relative flex-1 h-7">
          {timeline.gates.map((g, i) => {
            const color = g.override
              ? "var(--accent-amber-fg)" : "var(--accent-emerald-fg, #34d399)";
            return (
              <button
                key={i}
                onClick={() => setOpen(open === `g${i}` ? null : `g${i}`)}
                className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2 grid place-items-center"
                style={{ left: `${pct(g.ts)}%` }}
                title={`${g.gate}_gate · ${g.override ? "OVERRIDE" : "verified"} · ${g.actor}${g.proof ? " · " + g.proof : ""}\n${g.reason}`}
              >
                <span
                  className="block h-3.5 w-3.5 rotate-45"
                  style={{ background: color, boxShadow: `0 0 5px ${color}` }}
                />
                <span
                  className="absolute -bottom-3 text-2xs font-mono uppercase"
                  style={{ color }}
                >
                  {g.gate}{g.override ? "!" : "✓"}
                </span>
              </button>
            );
          })}
        </div>
        <span className="w-[64px] shrink-0" />
      </div>

      {/* drill-in detail for a clicked gate */}
      {open?.startsWith("g") && (() => {
        const g = timeline.gates[Number(open.slice(1))];
        if (!g) return null;
        return (
          <div className="ml-[120px] mt-4 text-2xs leading-snug rounded bg-[color:var(--surface-2)]/50 p-2 border border-[color:var(--border-default)]/40">
            <span className="font-mono uppercase tracking-wider text-[color:var(--text-secondary)]">
              {g.gate}_gate · {g.override ? "⚠ override" : "✓ verified"}
            </span>
            <div className="mt-1 opacity-70">actor: <span className="font-mono">{g.actor || "—"}</span>{g.proof ? ` · proof: ${g.proof}` : ""}</div>
            <div className="mt-1 opacity-50 break-words">{g.reason}</div>
            {taskId && (
              <div className="mt-2 pt-2 border-t border-[color:var(--border-default)]/40">
                <GateEvidenceBlock taskId={taskId} project={project} />
              </div>
            )}
          </div>
        );
      })()}

      {/* legend */}
      <div className="ml-[120px] mt-5 flex flex-wrap gap-x-4 gap-y-1 text-2xs font-mono text-[color:var(--text-muted)] opacity-70">
        <span><span className="inline-block h-2 w-3 align-middle rounded-sm" style={{ background: "var(--accent-amber-bg)" }} /> work session</span>
        <span><span className="inline-block h-2 w-2 align-middle rotate-45" style={{ background: "var(--accent-amber-fg)" }} /> gate ! override</span>
        <span><span className="inline-block h-2 w-2 align-middle rotate-45" style={{ background: "var(--accent-emerald-fg, #34d399)" }} /> gate ✓ verified</span>
      </div>
    </div>
  );
}
