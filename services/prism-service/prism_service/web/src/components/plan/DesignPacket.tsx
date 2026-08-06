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

type Me = { user?: { id: string; email: string; display_name: string } };

export default function DesignPacket({
  taskId,
  project = "default",
  prototypeSrc,
}: {
  taskId: string;
  project?: string;
  prototypeSrc?: string;
}) {
  const [data, setData] = useState<DesignPacketData | null>(null);
  const [approving, setApproving] = useState(false);
  const [me, setMe] = useState<Me["user"] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  // THE SYSTEM TRACKS THE APPROVER, THE OWNER DOES NOT TYPE IT (task
  // 7feed0c8). Same endpoint and same display_name||email precedence as the
  // page header's IdentityChip (PageHeader.tsx:32-55), so the name on the
  // receipt is the name in the header. The server resolves it again from the
  // principal, and design_packet.record_approval still raises ValueError on
  // an empty approver — this SUPPLIES the identity, it never stops requiring
  // one.
  useEffect(() => {
    let cancel = false;
    api.get<Me>("/api/auth/me")
      .then((r) => { if (!cancel) setMe(r.user ?? null); })
      .catch(() => { if (!cancel) setMe(null); });
    return () => { cancel = true; };
  }, []);

  const load = useCallback(() => {
    api
      .get<DesignPacketData>(`/api/conductor/design-packet?task_id=${taskId}&project=${project}`)
      .then(setData)
      .catch(() => setData(null));
  }, [taskId, project]);

  useEffect(() => { load(); }, [load]);

  const signedInAs = (me?.display_name || me?.email || "").trim();

  const doApprove = useCallback(() => {
    setApproving(true);
    setErr(null);
    api
      .post(`/api/conductor/design-packet/approve?project=${project}`,
           { task_id: taskId, approver: signedInAs })
      .then(() => load())
      .catch((e) => setErr(e instanceof Error ? e.message : String(e)))
      .finally(() => setApproving(false));
  }, [taskId, project, signedInAs, load]);

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

      {/* DECISION FIRST (task 7feed0c8). The reviewer lands on the decision
          and the visual artifact; the prose stays in full BELOW them, never
          collapsed behind a toggle — c016667f exists to stop approval
          without reading, and hiding the design would reintroduce it. */}
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
            <div className="flex items-center gap-2 flex-wrap">
              <button
                type="button"
                disabled={approving}
                onClick={doApprove}
                className="text-2xs font-semibold px-2.5 py-1 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
                style={{ color: "var(--accent-teal-fg)" }}
              >
                {approving ? "Approving…" : "Approve design"}
              </button>
              {signedInAs && (
                <span className="text-[11px] text-[color:var(--text-muted)]">
                  approving as {signedInAs}
                </span>
              )}
            </div>
            {err && <div className="text-[12px] text-[color:var(--accent-rose-fg,#c33)]">{err}</div>}
          </>
        )}
      </div>

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
