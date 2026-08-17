import { useState, useEffect, useCallback, lazy, Suspense } from "react";
import Markdown from "@/components/Markdown";
import { api } from "@/lib/api";

// LAZY BY DESIGN, same rationale as PlanView.tsx's own Mermaid import: the
// diagram half of the packet must not drag mermaid/cytoscape/katex into the
// task page's critical path when the card first paints.
const Mermaid = lazy(() => import("./Mermaid"));

/**
 * FR-1/FR-2/FR-3 (task c016667f): the design packet as ONE assembled,
 * interactable card — not a fifth top-level tab, not three one-at-a-time
 * sub-tabs (owner 2026-07-22/2026-08-03). Fetches the server-assembled
 * packet itself (GET /api/conductor/design-packet) so the order report and
 * approval status the gate consults are the SAME values the card renders,
 * never a client recomputation that could drift from the gate's own read.
 */

type DesignPacketData = {
  plan_doc: string;
  plan_diagram: string;
  oracle: string;
  likely_misfire: string;
  prototype: { exists: boolean; bytes: number };
  order: { admits: string; actual: string; below_order: boolean };
  content_hash: string;
  approval: { approved: boolean; stale: boolean; reason: string };
};

export default function DesignPacket({
  taskId,
  project = "default",
  prototypeSrc,
  onApprove,
  hideApproval = false,
  refreshToken,
}: {
  taskId: string;
  project?: string;
  prototypeSrc?: string;
  // ONE approve control (task 76df7520) - calls the SAME
  // approveDesignPacket-then-gate-POST path gateDecide("approve") uses on the
  // main gate panel. No local write path here anymore: a second, forked
  // approve affordance could record the ledger without ever releasing the
  // gate, which is exactly the drift this prop closes off.
  onApprove?: () => void;
  // The gate decision panel mounts this packet as the EVIDENCE under
  // approval (task b7b71225): there the panel's own Approve/decision controls
  // are the single affordance, so the packet's footer must not render a
  // second one - same forked-approve rule as onApprove above.
  hideApproval?: boolean;
  // Bumped by an external approve (task fa7735bd) to retrigger `load()`
  // without changing taskId/project - the [taskId, project]-only fetch
  // below never refires on its own after gateDecide's approveDesignPacket()
  // flips approval.approved, so this card would keep rendering the stale
  // not-approved branch until a full page reload.
  refreshToken?: number;
}) {
  const [data, setData] = useState<DesignPacketData | null>(null);

  const load = useCallback(() => {
    api
      .get<DesignPacketData>(`/api/conductor/design-packet?task_id=${taskId}&project=${project}`)
      .then(setData)
      .catch(() => setData(null));
  }, [taskId, project]);

  useEffect(() => { load(); }, [load, refreshToken]);

  if (!data) return null;

  const { order, approval } = data;
  const orderOk = !order.below_order;

  return (
    <div className="space-y-4 rounded-lg border border-[color:var(--border-default)] p-4">
      {/* FR-2: the order the feature admits vs. what the packet actually has. */}
      <div
        className="rounded-md px-3 py-2 text-[12px] leading-relaxed"
        style={{
          background: orderOk ? "var(--accent-sage-bg)" : "var(--accent-amber-bg, var(--accent-sage-bg))",
          boxShadow: `inset 0 0 0 1px ${orderOk ? "var(--accent-sage-ring)" : "var(--accent-amber-ring, var(--accent-sage-ring))"}`,
        }}
      >
        {orderOk
          ? `highest order on file: ${order.actual} (this feature admits ${order.admits})`
          : `below order: this feature admits an interactive ${order.admits}, but the packet only carries a ${order.actual}`}
      </div>

      {/* FR-6/FR-7/task 73f13267: the ONLY approval affordance — an explicit
          owner write action, never a render/browser receipt. Rendered here,
          BEFORE the prototype iframe and diagram (not last), so a reviewer
          reaches Approve without scrolling past them; the prototype below
          still renders unconditionally, never collapsed. Hidden when the
          gate panel mounts this packet as evidence: its decision controls
          own the click. */}
      {!hideApproval && (
      <div className="rounded-md px-3 py-2.5 border border-[color:var(--border-default)] space-y-2">
        {approval.approved ? (
          <div className="text-[12px]" style={{ color: "var(--accent-sage-fg)" }}>
            ✓ design packet approved — plan_gate can clear
          </div>
        ) : (
          <>
            <div className="text-[12px] text-[color:var(--text-secondary)]">
              {approval.stale
                ? "the design packet changed since it was approved — re-approval is required"
                : approval.reason || "this design packet needs an explicit owner approval before plan_gate can clear"}
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                disabled={!onApprove}
                onClick={onApprove}
                className="text-2xs font-semibold px-2.5 py-1 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
                style={{ color: "var(--accent-teal-fg)" }}
              >
                Approve design
              </button>
            </div>
          </>
        )}
      </div>
      )}

      {data.prototype.exists && prototypeSrc && (
        <div>
          <div className="text-2xs uppercase tracking-wider mb-1 text-[color:var(--text-muted)]">prototype</div>
          <div className="rounded-md overflow-hidden border border-[color:var(--border-default)]">
            <iframe
              title="prototype"
              src={prototypeSrc}
              className="w-full block"
              style={{ height: "60vh", background: "var(--background-base)" }}
            />
          </div>
        </div>
      )}

      {!!data.plan_diagram?.trim() && (
        <div>
          <div className="text-2xs uppercase tracking-wider mb-1 text-[color:var(--text-muted)]">diagram</div>
          <div className="rounded-lg p-4 bg-[color:var(--midground-base)]/5">
            <Suspense fallback={<div className="text-xs opacity-50 py-6 text-center">Loading diagram…</div>}>
              <Mermaid chart={data.plan_diagram} />
            </Suspense>
          </div>
        </div>
      )}

      {!!data.plan_doc?.trim() && (
        <div>
          <div className="text-2xs uppercase tracking-wider mb-1 text-[color:var(--text-muted)]">proposed change</div>
          <Markdown text={data.plan_doc} className="space-y-3 max-w-none" />
        </div>
      )}

      {(data.oracle || data.likely_misfire) && (
        <div className="text-[12px] leading-relaxed space-y-1">
          {data.oracle && (
            <div><span className="text-[color:var(--text-muted)]">oracle:</span> {data.oracle}</div>
          )}
          {data.likely_misfire && (
            <div><span className="text-[color:var(--text-muted)]">likely misfire:</span> {data.likely_misfire}</div>
          )}
        </div>
      )}

    </div>
  );
}
