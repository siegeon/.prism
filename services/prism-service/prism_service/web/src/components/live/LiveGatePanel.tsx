import { useCallback, useEffect, useState } from "react";
import { api, approveDesignPacket } from "@/lib/api";
import DesignPacket from "@/components/plan/DesignPacket";
import DecisionPacket from "@/components/plan/DecisionPacket";

/**
 * Task d56f3b25 (S3, conductor-into-live migration, mx-e5c400): the gate
 * decision surface for a card parked at plan_gate/green_gate, opened from
 * /live's own docked action strip -- never a second hand-built packet, and
 * never a redirect to the task detail page. Mounts the SAME DesignPacket
 * (Plan, hideApproval) and DecisionPacket (Evidence) TaskDetailPage's own
 * gate card renders, and drives the SAME POST /api/conductor/gate
 * TaskDetailPage.gateDecide already calls.
 */

type GateReadiness = {
  receipt_ok: boolean;
  receipt_refusal?: string;
  manual_review?: boolean;
  receipt?: {
    adapter?: string; passed?: boolean; status?: string;
    ended_at?: string; reason?: string;
  };
};

type DecisionResult = { kind: "checking" | "ok" | "refused"; text: string };

export default function LiveGatePanel({ taskId, project = "default", onClose }: {
  taskId: string;
  project?: string;
  onClose?: () => void;
}) {
  const [readiness, setReadiness] = useState<GateReadiness | null>(null);
  const [stale, setStale] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<DecisionResult | null>(null);
  const [me, setMe] = useState<{ id: string; email: string } | null>(null);

  useEffect(() => {
    let cancel = false;
    api.get<{ user?: { id: string; email: string } }>("/api/auth/me")
      .then((r) => { if (!cancel) setMe(r.user ?? null); })
      .catch(() => { if (!cancel) setMe(null); });
    return () => { cancel = true; };
  }, []);
  const approverIdentity = me?.email || me?.id || "";

  // AC-8/NFR-1: ONE choke point for GET /api/conductor/gate/readiness --
  // read on open below AND re-read after a decision inside gateDecide's
  // finally{} -- never the live work-graph node snapshot (that boot/SSE
  // feed only decides whether the strip button exists at all).
  const refreshReadiness = useCallback(async (): Promise<GateReadiness | null> => {
    try {
      const gr = await api.get<GateReadiness>(
        `/api/conductor/gate/readiness?task_id=${taskId}&project=${project}`);
      setReadiness(gr);
      setStale(false);
      return gr;
    } catch {
      // A failed re-read keeps showing the LAST known readiness, marked
      // stale (neutral amber) -- never silently blanking to a red refusal
      // that never actually happened.
      setStale(true);
      return null;
    }
  }, [taskId, project]);

  useEffect(() => { refreshReadiness(); }, [refreshReadiness]);

  // At plan_gate awaiting design approval, receipt_ok reads false from the
  // design-packet adapter -- "not yet approved by you", never "no evidence
  // exists" (mirrors TaskDetailPage's isAwaitingDesignApproval).
  const isAwaitingDesignApproval =
    readiness?.receipt?.adapter === "design-packet" &&
    readiness?.receipt?.passed === false;

  const gateDecide = async (action: "approve" | "reject") => {
    if (action === "reject" && !reason.trim()) {
      setResult({ kind: "refused", text: "A reason is required to reject the gate." });
      return;
    }
    const decisionReason = reason.trim() || "approved by owner (one-click from /live)";
    setBusy(true);
    setResult({ kind: "checking", text: "recording the decision…" });
    try {
      if (isAwaitingDesignApproval && action === "approve") {
        await approveDesignPacket(taskId, approverIdentity, project);
      }
      const r = await fetch(`/api/conductor/gate?project=${project}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task_id: taskId,
          action,
          reason: decisionReason,
          override: false,
          actor: approverIdentity,
        }),
      });
      const body = await r.json().catch(() => ({}));
      if (body.ok === false) {
        setResult({ kind: "refused", text: `${action} refused: ${body.reason ?? "unknown"}` });
      } else {
        setResult({ kind: "ok", text: `Gate ${action}d${body.to_step ? ` → ${body.to_step}` : ""}.` });
        setReason("");
      }
    } catch (e) {
      setResult({ kind: "refused", text: e instanceof Error ? e.message : "request failed" });
    } finally {
      setBusy(false);
      refreshReadiness();
    }
  };

  // NFR-1: verdict tone -- settled (already decided) / a genuine refusal /
  // stale (a failed re-read, neutral amber, never red) render distinctly.
  const verdict = stale
    ? { tone: "amber" as const, text: "readiness re-read failed — showing the last known state (stale)" }
    : !readiness
      ? { tone: "amber" as const, text: "reading readiness…" }
      : readiness.receipt?.adapter === "settled"
        ? { tone: "sage" as const, text: readiness.receipt.reason || "settled — this gate is already decided" }
        : readiness.receipt_ok
          ? { tone: "sage" as const, text: readiness.manual_review ? "awaiting your review — no machine tooth ran" : "ready — evidence receipt passes" }
          : { tone: "rose" as const, text: readiness.receipt_refusal || "blocked — evidence not on file" };
  const toneStyle = verdict.tone === "sage"
    ? { background: "var(--accent-sage-bg)", color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)" }
    : verdict.tone === "amber"
      ? { background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }
      : { background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" };

  return (
    <div
      className="absolute inset-3 z-20 overflow-y-auto rounded-lg border p-4 space-y-4"
      style={{ background: "var(--surface-1)", borderColor: "var(--border-default)" }}
    >
      <div className="flex items-center justify-between">
        <div className="text-sm font-semibold" style={{ color: "var(--text-primary)" }}>
          Gate decision
        </div>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]"
          >
            close
          </button>
        )}
      </div>

      <div className="rounded-md px-3 py-2 text-[12px]" style={toneStyle}>
        receipt_ok: {String(readiness?.receipt_ok ?? "…")} — {verdict.text}
      </div>

      {/* AC-4/mx-d0ed12: Plan is ONE section holding the real DesignPacket
          (hideApproval -- this panel's own footer below is the single
          approval affordance); Diagram + Prototype nest INSIDE its own
          body, never as top-level siblings here. Evidence is a sibling
          section holding the real DecisionPacket. Never a hand-built
          second packet of either kind. */}
      <div>
        <div className="text-2xs uppercase tracking-wider mb-1.5 text-[color:var(--text-muted)]">Plan</div>
        <DesignPacket taskId={taskId} project={project} hideApproval />
      </div>

      <div>
        <div className="text-2xs uppercase tracking-wider mb-1.5 text-[color:var(--text-muted)]">Evidence</div>
        <DecisionPacket taskId={taskId} project={project} />
      </div>

      <div className="space-y-2 border-t pt-3" style={{ borderColor: "var(--border-default)" }}>
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="reason (required to reject)"
          className="w-full text-[12px] rounded-md border p-2"
          style={{ borderColor: "var(--border-default)", background: "var(--surface-1)" }}
          rows={2}
        />
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => gateDecide("approve")}
            disabled={busy || readiness?.receipt_ok !== true}
            className="text-2xs font-semibold px-3 py-1.5 rounded border disabled:opacity-40"
            style={{ borderColor: "var(--border-default)", color: "var(--accent-sage-fg)" }}
          >
            Approve
          </button>
          <button
            type="button"
            onClick={() => gateDecide("reject")}
            disabled={busy}
            className="text-2xs font-semibold px-3 py-1.5 rounded border disabled:opacity-40"
            style={{ borderColor: "var(--border-default)", color: "var(--accent-rose-fg)" }}
          >
            Reject
          </button>
          {result && (
            <div className="text-2xs" style={{ color: "var(--text-muted)" }}>{result.text}</div>
          )}
        </div>
      </div>
    </div>
  );
}
