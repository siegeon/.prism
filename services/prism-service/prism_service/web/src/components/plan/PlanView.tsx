import { useState } from "react";
import { Empty } from "@/components/ui";
import Markdown from "@/components/Markdown";
import Mermaid from "./Mermaid";
import SdlcProgress, { type PhaseProgress, type Activity } from "@/components/conductor/SdlcProgress";
import StepRail, { type StepTurn } from "@/components/conductor/StepRail";
import { type Timeline } from "@/components/conductor/TaskActivityGantt";

/**
 * The task's work panel, as TABS in one slot — Prototype (clickable mock,
 * iframed) · Diagram (Mermaid) · Proposed change (markdown) · Implementation
 * (the conductor drive). The Implementation tab MERGES what used to be the
 * separate Conductor + Activity cards: an SdlcProgress minimap, a compact
 * collapsible StepRail (gates expand to their receipts), a Rail/Timeline
 * sub-toggle onto the retained wall-clock TaskActivityGantt, and the pending-
 * gate resolve form. "This is how we drive PRISM locally" — not a product tab.
 * Full-width (max-w-none) so the task page uses the whole screen.
 */
export type ConductorInfo = {
  step?: string;
  gateState?: string;
  gateReason?: string;
  phase?: PhaseProgress | null;
  status?: string;
  // Honest work state — drives the pulse/pill (only "working" animates).
  activity?: Activity | null;
  timeline?: Timeline | null;
  // Raw audit turns (same rows as the Timeline card) so each StepRail step
  // can DRILL DOWN into the turns that happened on it — the implementation
  // view and the timeline are one thing, disclosed hierarchically.
  turns?: StepTurn[];
};
export type GateControls = {
  reason: string;
  setReason: (s: string) => void;
  override: boolean;
  setOverride: (b: boolean) => void;
  decide: (action: "approve" | "reject") => void;
  busy: boolean;
};

export default function PlanView({
  diagram,
  doc,
  prototypeSrc,
  conductor,
  reduced,
  gate,
  onValidation,
}: {
  diagram?: string;
  doc?: string;
  prototypeSrc?: string;
  conductor?: ConductorInfo | null;
  reduced?: boolean | null;
  gate?: GateControls | null;
  onValidation?: () => void;
}) {
  const hasDiagram = !!diagram?.trim();
  const hasDoc = !!doc?.trim();
  const hasProto = !!prototypeSrc;
  const hasImpl = !!conductor && !!(conductor.step || (conductor.gateState && conductor.gateState !== "none"));

  const tabs: { key: string; label: string }[] = [];
  if (hasProto) tabs.push({ key: "prototype", label: "Prototype" });
  if (hasDiagram) tabs.push({ key: "diagram", label: "Diagram" });
  if (hasDoc) tabs.push({ key: "doc", label: "Proposed change" });
  if (hasImpl) tabs.push({ key: "implementation", label: "Implementation" });

  // Default to Implementation when the conductor is engaged (the live status),
  // else the first available artifact tab.
  const [active, setActive] = useState(hasImpl ? "implementation" : tabs[0]?.key ?? "doc");

  if (tabs.length === 0) return <Empty>No plan yet.</Empty>;
  const cur = tabs.some((t) => t.key === active) ? active : tabs[0].key;
  const c = conductor;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1 border-b border-[color:var(--border-default)]">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActive(t.key)}
            className={
              "px-3 py-2 text-[11px] uppercase tracking-wider -mb-px border-b-2 transition-colors " +
              (cur === t.key
                ? "border-[color:var(--accent-teal-fg)] text-[color:var(--accent-teal-fg)]"
                : "border-transparent text-[color:var(--text-muted)] hover:text-[color:var(--text-secondary)]")
            }
          >
            {t.label}
          </button>
        ))}
        {cur === "prototype" && (
          <a
            href={prototypeSrc}
            target="_blank"
            rel="noreferrer"
            className="ml-auto px-2 text-[11px] uppercase tracking-wider text-[color:var(--accent-teal-fg)] hover:opacity-80"
          >
            open full screen ↗
          </a>
        )}
      </div>

      {cur === "prototype" && hasProto && (
        <div className="rounded-md overflow-hidden border border-[color:var(--border-default)]">
          <iframe
            title="prototype"
            src={prototypeSrc}
            className="w-full block"
            style={{ height: "80vh", background: "var(--background-base)" }}
          />
        </div>
      )}
      {cur === "diagram" && hasDiagram && (
        <div className="rounded-lg p-4 bg-[color:var(--midground-base)]/5">
          <Mermaid chart={diagram!} />
        </div>
      )}
      {cur === "doc" && hasDoc && (
        <Markdown text={doc!} className="space-y-4 max-w-none" />
      )}

      {cur === "implementation" && hasImpl && c && (
        <div>
          <div className="mt-1 mb-1">
            <SdlcProgress step={c.step} phase={c.phase} status={c.status} activity={c.activity} reduced={reduced} />
          </div>

          <StepRail
            step={c.step}
            gateState={c.gateState}
            phase={c.phase}
            status={c.status}
            activity={c.activity}
            gates={c.timeline?.gates ?? []}
            turns={c.turns ?? []}
            reduced={reduced}
          />

          {c.gateReason && c.gateState !== "pending" && onValidation && (
            <button
              onClick={onValidation}
              className="mt-3 flex items-center gap-1.5 text-left group"
            >
              <span className="text-[11px] uppercase tracking-wider text-[color:var(--text-muted)]">
                {c.gateState === "failed" ? "failure reason" : "validation"}
              </span>
              <span className="text-[12px] leading-snug opacity-80 group-hover:opacity-100 truncate max-w-[520px]">{c.gateReason}</span>
              <span className="opacity-50 group-hover:opacity-100 shrink-0">→</span>
            </button>
          )}

          {c.gateState === "pending" && gate && (
            <div className="mt-4 pt-4 border-t border-[color:var(--midground-base)]/15">
              <div className="opacity-50 mb-2 text-[11px] uppercase tracking-wider">Resolve gate</div>
              <textarea
                value={gate.reason}
                onChange={(e) => gate.setReason(e.target.value)}
                required
                placeholder="Reason (required) — why approve or reject this gate?"
                rows={3}
                className="w-full text-[13px] rounded-md bg-[color:var(--background-base)]/40 border border-[color:var(--midground-base)]/20 p-2 leading-relaxed resize-y"
              />
              <label className="flex items-center gap-2 mt-2 text-[12px] opacity-80 cursor-pointer">
                <input type="checkbox" checked={gate.override} onChange={(e) => gate.setOverride(e.target.checked)} />
                override (bypass the verifier and release on manual judgment)
              </label>
              <div className="flex gap-2 mt-3">
                <button
                  type="button"
                  disabled={gate.busy || !gate.reason.trim()}
                  onClick={() => gate.decide("approve")}
                  className="text-[11px] uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
                  style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)" }}
                >
                  Approve
                </button>
                <button
                  type="button"
                  disabled={gate.busy || !gate.reason.trim()}
                  onClick={() => gate.decide("reject")}
                  className="text-[11px] uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
                  style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)" }}
                >
                  Reject
                </button>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
