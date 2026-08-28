// @ts-nocheck -- the block between the markers below is executed VERBATIM
// under a bare `node` by tests/unit/test_gate_banner_refreshes_on_gate_arrival.py
// (D-4: a behavioural discriminator, not a spelling check), so it must stay
// self-contained plain JS with no TypeScript-only syntax (no inline param
// types). Callers (TaskDetailPage, StepRail, ConductorPage) get real types
// from the exported aliases below regardless — this pragma only relaxes
// internal checking of the untyped implementation, never the public shape.
//
// ONE shared severity vocabulary for a pending/failed gate, consumed by the
// TaskDetailPage banner, StepRail's gate row and ConductorPage's tile
// handoff line (task 8e5aa63b) — so the three surfaces cannot legitimately
// disagree at the same moment for the same gate. Five labels: READY,
// PENDING, AWAITING YOU, WAITING ON CHILDREN, BLOCKED. A null/missing
// readiness (the tile's input today) must never escalate to BLOCKED or
// AWAITING YOU — that would be claiming a verdict the caller does not
// actually have evidence for.
//
// WAITING ON CHILDREN is its own label, not BLOCKED (owner, live, task
// 95474ec7: "no it reads blocked as best i can tell" while a
// great-grandchild was mid-implement) — an epic-rollup refusal is never
// something the owner can act on directly (they cannot force a child to
// finish), so it must not carry the same alarm word as a genuine stuck
// state (verifier_refused, no evidence at all). Same reasoning already
// applied to activity_for's "idle"/"stalled" wording (mx-dfd56a): render
// alarm vocabulary only when the owner actually has an action.
export type GateSeverityKey =
  | "ready" | "pending" | "awaiting_you" | "waiting_on_children" | "blocked";
export type GateSeverityResult = { key: GateSeverityKey; label: string };
export type GateSeveritySnapshot = {
  gate_state?: string | null;
  verifier_refused?: boolean;
  manual_review?: boolean;
  readiness?: {
    receipt_ok?: boolean;
    manual_review?: boolean;
    receipt?: { adapter?: string; passed?: boolean; reason?: string };
    blocking_children?: unknown[];
  } | null;
};

// --- GATE-SEVERITY-BLOCK-START ---
function gateSeverity(snap) {
  var gateState = (snap && snap.gate_state) || "";
  var readiness = snap && snap.readiness;
  if (gateState !== "pending" && gateState !== "failed") {
    return { key: "ready", label: "READY" };
  }
  if (readiness == null) {
    // No evidence fetched yet (or a caller with no readiness input at all,
    // e.g. the board tile) — honestly PENDING, never a claimed verdict.
    return { key: "pending", label: "PENDING" };
  }
  var receiptOk = !!readiness.receipt_ok;
  var adapter = (readiness.receipt && readiness.receipt.adapter) || "";
  var manualReview = !!readiness.manual_review || !!(snap && snap.manual_review);
  if (receiptOk) {
    if (manualReview && adapter === "human") {
      return { key: "awaiting_you", label: "AWAITING YOU" };
    }
    return { key: "ready", label: "READY" };
  }
  if (adapter === "story-rubric") {
    // Machine-owned, healthily re-swept state (mx-a31a3d) — not stuck.
    return { key: "pending", label: "PENDING" };
  }
  if (adapter === "design-packet") {
    // Unapproved design packet means "not yet approved by YOU", not
    // "no evidence exists" (task 0c49c385).
    return { key: "awaiting_you", label: "AWAITING YOU" };
  }
  if (snap && snap.verifier_refused) {
    return { key: "blocked", label: "BLOCKED" };
  }
  var blockingChildren = readiness.blocking_children || [];
  if (adapter === "epic-rollup" && blockingChildren.length > 0) {
    return { key: "waiting_on_children", label: "WAITING ON CHILDREN" };
  }
  return { key: "blocked", label: "BLOCKED" };
}
// --- GATE-SEVERITY-BLOCK-END ---

export { gateSeverity };
