import { useState, useEffect } from "react";
import { Link } from "react-router-dom";
import { Empty } from "@/components/ui";
// Mermaid/Markdown rendering for the design tab now lives INSIDE
// DesignPacket.tsx (task c016667f) — it fetches the assembled packet and
// lazy-loads Mermaid itself, so PlanView no longer imports either directly.
import SdlcProgress, { type PhaseProgress, type Activity } from "@/components/conductor/SdlcProgress";
import StepRail, { type StepTurn } from "@/components/conductor/StepRail";
import { type Timeline } from "@/components/conductor/TaskActivityGantt";
import { stepLabel } from "@/lib/workflowChips";
import { useTaskEvidence } from "@/components/EvidenceGallery";
import DesignPacket from "./DesignPacket";

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
  // Server-scoped per-step token totals (api.tasks._step_token_totals),
  // windowed to each step's own [entry, exit) span — see StepRail's prop
  // comment for why this can't be derived from summing turns[].turn_tokens.
  stepTokens?: Record<string, number>;
};
export type GateControls = {
  reason: string;
  setReason: (s: string) => void;
  override: boolean;
  setOverride: (b: boolean) => void;
  decide: (action: "approve" | "reject") => void;
  busy: boolean;
};

// One committed RED test that pins the task's oracle: its function name, its
// docstring (whose lead "AC-N / FR-N —" correlates it to an acceptance
// criterion), and the file it lives in. Sourced from GET /api/tasks/:id/tests.
// `status` is the latest ACTUAL pytest outcome (?run=true) — absent when the
// run couldn't happen.
export type PinTest = {
  name: string;
  doc?: string;
  file?: string;
  status?: string;
  line?: number;
  snippet?: string;
};

// Pull the leading acceptance-criterion id(s) out of a test's docstring.
// Docs read like "AC-1 / FR-1 — a header row pins to the top". We surface the
// AC id (preferring AC over FR) as a badge and keep the remainder as the
// human explanation. No leading id → no badge, whole doc is the explanation.
export function parseAc(doc?: string): { badge: string | null; rest: string } {
  const d = (doc ?? "").trim();
  // Leading run of "AC-N" / "FR-N" ids joined by "/", then an em/en dash or
  // hyphen separator before the prose.
  const m = /^((?:AC|FR)-\d+)(?:\s*\/\s*(?:AC|FR)-\d+)*\s*(?:[—–-]\s*)?/i.exec(d);
  if (!m) return { badge: null, rest: d };
  const ids = m[0].match(/(?:AC|FR)-\d+/gi)?.map((s) => s.toUpperCase()) ?? [];
  const badge = ids.find((x) => x.startsWith("AC")) ?? ids[0] ?? null;
  return { badge, rest: d.slice(m[0].length).trim() };
}

// Ascending numeric order of the AC/FR ids (AC-2 before AC-10); tests without
// a parsed id sink to the end but keep a stable order.
function acOrder(t: PinTest): number {
  const b = parseAc(t.doc).badge;
  const n = b ? parseInt(b.replace(/\D+/g, ""), 10) : NaN;
  return Number.isFinite(n) ? n : Number.MAX_SAFE_INTEGER;
}

// Pull the acceptance-criteria lines out of the proposed change (plan_doc) so
// the Tests tab can show the oracle tied to the ACs it covers. A plan lists
// them as bullet lines that name an AC id ("AC-1, AC-2 -> assertion_source_for
// …"); we take those, stripped of markdown bullets, in document order.
function parseAcLines(planDoc?: string): string[] {
  if (!planDoc) return [];
  const out: string[] = [];
  for (const raw of planDoc.split("\n")) {
    // Only the mapping lines — a bullet that STARTS with an AC id, e.g.
    // "- AC-1, AC-2 -> assertion_source_for (...)". Prose that merely mentions
    // "Covers AC-3" is not an AC definition and must not be swept in.
    const line = raw.trim().replace(/^[-*+]\s*/, "").replace(/`/g, "").trim();
    if (/^AC-\d/.test(line)) out.push(line);
  }
  return out;
}

// ── Gate evidence ("what you're approving") ─────────────────────────────
// When a gate is PENDING a human is asked to approve/reject with NO visible
// evidence of what led here. This surfaces that evidence straight from the
// audit turns: the validation note(s) the conductor recorded on the
// advance(s) into this gate (e.g. "Red tests committed (c0581c2…): 3 failing
// — hierarchy cap ignored …, no ETag header, /neighbors 404"). Real history,
// never a hardcoded string.

// The validation text an advance_task turn carried (its work-done evidence),
// with any trailing "; gate=<state>" marker stripped off.
function advanceValidation(t: StepTurn): string {
  if (t.action !== "advance_task") return "";
  const v = /validation=([\s\S]*)$/.exec(t.details ?? "")?.[1]?.trim() ?? "";
  return v.replace(/;\s*gate=\w+\s*$/, "").trim();
}

// Evidence lines for the CURRENT pending gate: every advance validation
// recorded SINCE the last resolved gate (that's the work this gate reviews).
// Falls back to the single most-recent advance validation if no prior gate
// boundary exists. Each note is split into distinct, deduped, readable lines.
export function gateEvidenceLines(turns: StepTurn[]): string[] {
  const rows = turns ?? [];
  let start = 0;
  for (let i = rows.length - 1; i >= 0; i--) {
    if (rows[i].action === "gate_decide") { start = i + 1; break; }
  }
  const notes: string[] = [];
  for (let i = start; i < rows.length; i++) {
    const v = advanceValidation(rows[i]);
    if (v) notes.push(v);
  }
  if (notes.length === 0) {
    for (let i = rows.length - 1; i >= 0; i--) {
      const v = advanceValidation(rows[i]);
      if (v) { notes.push(v); break; }
    }
  }
  const lines: string[] = [];
  const seen = new Set<string>();
  for (const note of notes) {
    // Split on sentence boundaries so distinct claims bullet separately.
    for (const part of note.split(/(?<=\.)\s+(?=[A-Z0-9/])/)) {
      const s = part.trim();
      if (s && !seen.has(s)) { seen.add(s); lines.push(s); }
    }
  }
  return lines;
}

function GateEvidence({ step, turns }: { step?: string; turns: StepTurn[] }) {
  const lines = gateEvidenceLines(turns);
  const label = stepLabel(step ?? "");
  return (
    <div
      className="rounded-md p-3"
      style={{
        background: "var(--accent-amber-bg)",
        boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)",
      }}
    >
      <div className="flex items-center gap-2 mb-2">
        <span className="text-[13px]" style={{ color: "var(--accent-amber-fg)" }}>⚖</span>
        <span
          className="text-2xs uppercase tracking-wider font-medium"
          style={{ color: "var(--accent-amber-fg)" }}
        >
          What you're approving{label ? ` — ${label}` : ""}
        </span>
      </div>
      {lines.length > 0 ? (
        <ul className="space-y-1.5">
          {lines.map((l, i) => (
            <li
              key={i}
              className="flex items-start gap-2 text-[12.5px] leading-relaxed text-[color:var(--text-secondary)]"
            >
              <span
                className="mt-[6px] h-1.5 w-1.5 rounded-full shrink-0"
                style={{ background: "var(--accent-amber-fg)" }}
              />
              <span>{l}</span>
            </li>
          ))}
        </ul>
      ) : (
        <div className="text-[12px] text-[color:var(--text-muted)]">
          No recorded evidence for this step yet — the conductor logged no
          validation note on the advance into this gate.
        </div>
      )}
    </div>
  );
}

export default function PlanView({
  diagram,
  doc,
  prototypeSrc,
  conductor,
  reduced,
  gate,
  onValidation,
  pinTests,
  onRunTests,
  runningTests,
  gateReadiness,
  onMintEvidence,
  tabRequest,
  taskId,
  project,
  proofType,
  oracle,
  completionProof,
  likelyMisfire,
  fullOutcomeComplete,
  isAwaitingDesignApproval,
  onApproveDesign,
}: {
  diagram?: string;
  doc?: string;
  prototypeSrc?: string;
  conductor?: ConductorInfo | null;
  reduced?: boolean | null;
  gate?: GateControls | null;
  onValidation?: () => void;
  // RED tests pinning this task, drilled from the parent (avoids a 2nd fetch).
  pinTests?: PinTest[];
  // Run the pinned tests on demand (GET ...?run=true) and refresh the rows.
  // Page load never runs them — that is what kept the page fast, and what left
  // the badges blind until this action existed (task f3e8d477).
  onRunTests?: () => void;
  runningTests?: boolean;
  // LIVE evidence-tooth state (GET /api/conductor/gate/readiness) — the gate
  // card renders CURRENT truth + a concrete action, never a stale snapshot.
  gateReadiness?: {
    receipt_ok: boolean;
    receipt_refusal?: string;
    // receipt_ok=true here can mean "a human review is the accepted
    // evidence route", not "something passed" (task 72d3e0d1) — combined
    // with receipt.adapter==="human" it names that bare-entitlement case.
    manual_review?: boolean;
    receipt?: { adapter?: string; passed?: boolean; status?: string; ended_at?: string; reason?: string };
  } | null;
  // Gate-card action: re-run the oracle inside the daemon and mint a fresh
  // EvidenceReceipt (POST /api/conductor/gate/mint), then refresh readiness.
  onMintEvidence?: () => void;
  // External tab drive: the oracle card's "view tests" summary bumps `n` to
  // switch this panel to the Tests tab. The nonce lets a repeat click re-fire.
  tabRequest?: { tab: string; n: number } | null;
  // Task id + project — used to fetch the evidence store so a gate row can show
  // the artifacts that passed it. Omitted → no evidence fetch.
  taskId?: string;
  project?: string;
  // The task's proof_type (test / demo / ui / …) — tailors a gate's empty
  // evidence message (a test-proof gate points at the Tests tab, not a photo).
  proofType?: string;
  // The task's oracle — the plain-language acceptance criterion. Shown with
  // the tests in the Tests tab.
  oracle?: string;
  // Completion proof & risk — rendered in the gate's evidence area (not a
  // separate overview card), so the proof lives with the pipeline.
  completionProof?: string;
  likelyMisfire?: string;
  fullOutcomeComplete?: boolean;
  // A populated-but-unapproved design packet is waiting (task 76df7520) - the
  // owner must land on the Design tab, never the empty green-gate-shaped
  // Implementation view, and the packet's Approve control must call the SAME
  // path the main gate panel does.
  isAwaitingDesignApproval?: boolean;
  onApproveDesign?: () => void;
}) {
  const hasDiagram = !!diagram?.trim();
  const hasDoc = !!doc?.trim();
  const hasProto = !!prototypeSrc;
  const hasImpl = !!conductor && !!(conductor.step || (conductor.gateState && conductor.gateState !== "none"));
  const tests = pinTests ?? [];
  const hasTests = tests.length > 0;

  // DESIGN IS ONE PACKAGE, not three tabs (owner 2026-07-22). The prototype,
  // the diagram and the proposed change are the same artifact seen three ways;
  // splitting them made the reviewer hunt for the parts and let a design be
  // "approved" having seen only whichever tab happened to be open.
  const hasDesign = hasProto || hasDiagram || hasDoc;

  const tabs: { key: string; label: string }[] = [];
  if (hasDesign) tabs.push({ key: "design", label: "Design" });
  if (hasImpl) tabs.push({ key: "implementation", label: "Implementation" });
  if (hasTests) tabs.push({ key: "tests", label: "Tests" });

  // Default to Implementation when the conductor is engaged (the live status),
  // else the first available artifact tab.
  const [active, setActive] = useState(hasImpl ? "implementation" : tabs[0]?.key ?? "design");

  // Honor an external tab request (e.g. the oracle "N RED" summary click).
  // Keyed on the nonce so clicking again after a manual tab change re-fires.
  useEffect(() => {
    if (!tabRequest?.tab) return;
    // Callers still ask for the old split keys; they all land on Design now.
    const req = tabRequest.tab;
    setActive(["prototype", "diagram", "doc"].includes(req) ? "design" : req);
  }, [tabRequest?.n, tabRequest?.tab]);

  // gateReadiness (and so isAwaitingDesignApproval) loads AFTER this
  // component's first paint, so the initial useState default above cannot
  // see it - land the owner on Design the moment it becomes true, not just
  // at mount (task 76df7520 clause G).
  useEffect(() => {
    if (isAwaitingDesignApproval) setActive("design");
  }, [isAwaitingDesignApproval]);

  // Tests tab: which pin is expanded to show its actual assertion source —
  // a reviewer must be able to EVALUATE a pin, not just read its name.
  const [openTest, setOpenTest] = useState<string | null>(null);

  // The task's evidence store — handed to the rail so a gate row, when
  // expanded, shows the VISUAL artifacts that passed it (screenshots/video).
  const evidence = useTaskEvidence(taskId, project);
  // Acceptance criteria parsed from the proposed change — shown with the
  // oracle in the Tests tab, tying the oracle to what it must satisfy.
  const acLines = parseAcLines(doc);

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
              "px-3 py-2 text-2xs uppercase tracking-wider -mb-px border-b-2 transition-colors " +
              (cur === t.key
                ? "border-[color:var(--accent-teal-fg)] text-[color:var(--accent-teal-fg)]"
                : "border-transparent text-[color:var(--text-muted)] hover:text-[color:var(--text-secondary)]")
            }
          >
            {t.label}
          </button>
        ))}
        {cur === "design" && hasProto && (
          <a
            href={prototypeSrc}
            target="_blank"
            rel="noreferrer"
            className="ml-auto px-2 text-2xs uppercase tracking-wider text-[color:var(--accent-teal-fg)] hover:opacity-80"
          >
            open full screen ↗
          </a>
        )}
      </div>

      {cur === "design" && (
        // ONE assembled, interactable card (task c016667f, FR-3) — the
        // prototype, diagram and proposed change used to be three sub-tabs
        // seen one at a time (owner 2026-07-22 killed the FIRST split into a
        // fifth top-level tab; 2026-08-03 killed this second split too: a
        // reviewer could "approve" a design having seen only whichever
        // sub-tab happened to be open). DesignPacket fetches the SAME
        // server-assembled packet + approval status the gate itself reads.
        <DesignPacket
          taskId={taskId ?? ""}
          project={project}
          prototypeSrc={prototypeSrc}
          onApprove={onApproveDesign}
        />
      )}

      {cur === "tests" && hasTests && (
        <div className="space-y-3">
          {/* The oracle lives WITH the tests — the observable completion signal
              and the acceptance criteria (from the proposed change) that the
              tests below prove. Visual evidence is separate: it's at the gate
              in the Implementation tab. */}
          {(oracle || acLines.length > 0) && (
            <div className="rounded-md px-3 py-2.5 leading-relaxed"
              style={{ background: "var(--accent-teal-bg)", boxShadow: "inset 0 0 0 1px var(--accent-teal-ring)" }}>
              <div className="text-2xs uppercase tracking-wider mb-1 font-semibold" style={{ color: "var(--accent-teal-fg)" }}>
                oracle — observable completion signal
              </div>
              {oracle && <div className="text-[12.5px] leading-relaxed" style={{ color: "var(--text-primary)" }}>{oracle}</div>}
              {acLines.length > 0 && (
                <div className="mt-2">
                  <div className="text-2xs uppercase tracking-wider mb-1" style={{ color: "var(--accent-teal-fg)" }}>
                    acceptance criteria · from the proposed change
                  </div>
                  <ul className="space-y-0.5">
                    {acLines.map((l, i) => (
                      <li key={i} className="text-[12px] leading-relaxed flex gap-1.5" style={{ color: "var(--text-secondary)" }}>
                        <span className="shrink-0" style={{ color: "var(--accent-teal-fg)" }}>›</span>
                        <span>{l}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
          <div className="flex items-start justify-between gap-3">
            <div className="text-[12px] text-[color:var(--text-secondary)] leading-relaxed">
              The committed tests that pin this task's acceptance criteria.
              Badges show each test's <span className="font-medium">latest
              actual run</span> — red while the outcome is unmet, green once it
              holds.
            </div>
            {/* Explicit run (task f3e8d477). Page load stays cheap (run=false,
                no pytest); this is how a driver or reviewer PRODUCES the
                evidence on demand. The result is persisted, so the badge
                survives a reload instead of resetting to NOT RUN. */}
            {onRunTests && (
              <button
                type="button"
                disabled={runningTests}
                onClick={() => onRunTests()}
                className="shrink-0 text-2xs font-semibold px-2.5 py-1 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
                style={{ color: "var(--accent-teal-fg)" }}
              >
                {runningTests ? "Running…" : "Run tests"}
              </button>
            )}
          </div>
          <ul className="space-y-3">
            {[...tests].sort((a, b) => acOrder(a) - acOrder(b)).map((t) => {
              const { badge, rest } = parseAc(t.doc);
              // Honest per-test badge from the REAL run (api ?run=true).
              // No status = the run couldn't happen: say "not run", never
              // paint a phase-colored guess.
              const st = (t.status || "").toLowerCase();
              const chip = st === "passed"
                ? { label: "✓ pass", fg: "var(--accent-sage-fg)", ring: "var(--accent-sage-ring)", title: "this test currently PASSES" }
                : st === "failed" || st === "error"
                  ? { label: "✗ red", fg: "var(--accent-rose-fg)", ring: "var(--accent-rose-ring)", title: "this test is currently RED" }
                  : st === "skipped"
                    ? { label: "skipped", fg: "var(--accent-amber-fg)", ring: "var(--accent-amber-ring)", title: "this test was skipped in the last run" }
                    : { label: "not run", fg: "var(--text-muted)", ring: "var(--border-default)", title: "no recorded run for this test yet" };
              const key = `${t.file}:${t.name}`;
              const isOpen = openTest === key;
              return (
                <li
                  key={key}
                  className="rounded-md p-3 border border-[color:var(--border-default)] bg-[color:var(--surface-1)]"
                >
                  <button
                    type="button"
                    onClick={() => setOpenTest(isOpen ? null : key)}
                    className="flex items-center gap-2 flex-wrap w-full text-left"
                    aria-expanded={isOpen}
                    title={isOpen ? "collapse" : "show what this test asserts"}
                  >
                    <span className="text-2xs w-3 shrink-0" style={{ color: "var(--text-muted)" }}>
                      {isOpen ? "▾" : "▸"}
                    </span>
                    {badge && (
                      <span
                        className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
                        style={{
                          background: "var(--accent-violet-bg)",
                          color: "var(--accent-violet-fg)",
                          boxShadow: "inset 0 0 0 1px var(--accent-violet-ring)",
                        }}
                        title="pins acceptance criterion"
                      >
                        {badge}
                      </span>
                    )}
                    <span className="font-mono text-[12px] text-[color:var(--text-primary)] break-all bg-transparent">
                      {t.name}
                    </span>
                    <span
                      className="text-2xs uppercase tracking-wider px-1.5 py-0.5 rounded shrink-0"
                      style={{ color: chip.fg, boxShadow: `inset 0 0 0 1px ${chip.ring}` }}
                      title={chip.title}
                    >
                      {chip.label}
                    </span>
                  </button>
                  {rest && (
                    <div className="text-[12.5px] text-[color:var(--text-secondary)] leading-relaxed mt-1.5 pl-5">
                      {rest}
                    </div>
                  )}
                  {isOpen && t.snippet && (
                    <pre className="mt-2 ml-5 p-2.5 rounded text-[12px] font-mono leading-relaxed overflow-x-auto whitespace-pre bg-[color:var(--surface-2)] text-[color:var(--text-secondary)] border border-[color:var(--border-subtle)]">
                      {t.snippet}
                    </pre>
                  )}
                  {t.file && (
                    <div className="text-2xs font-mono mt-1.5 pl-5 truncate">
                      <Link
                        to={`/artifact?focus=${encodeURIComponent(t.file)}`}
                        className="underline decoration-dotted underline-offset-2 hover:opacity-80"
                        style={{ color: "var(--text-muted)" }}
                        title="open the full test file"
                      >
                        {t.file}{t.line ? ` · L${t.line}` : ""} →
                      </Link>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {cur === "implementation" && hasImpl && c && (
        <div>
          {/* A done task's flow is CLOSED: no mid-flight phase readout. The
              "PLAN GATE · 54%" header + half-filled bar read as 'stopped
              halfway' even next to a DONE chip (owner hit exactly this) — so
              status=done replaces the progress header with a closed banner.
              The rail below stays as frozen history (no pulse, no gate). */}
          <div className="mt-1 mb-1">
            {c.status === "done" ? (
              <div
                className="flex items-center gap-2 rounded-md px-3 py-2 text-[12px] uppercase tracking-wider"
                style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)", boxShadow: "inset 0 0 0 1px var(--accent-emerald-ring)" }}
              >
                <span>✓ closed — done</span>
                <span className="normal-case tracking-normal opacity-80">
                  flow ended at {c.step?.replace(/_/g, " ")}; steps below are history, not remaining work
                </span>
              </div>
            ) : (
              <SdlcProgress step={c.step} phase={c.phase} status={c.status} activity={c.activity} reduced={reduced} />
            )}
          </div>

          <StepRail
            step={c.step}
            gateState={c.status === "done" ? "none" : c.gateState}
            // The SAME gateReadiness this card already receives as a prop
            // (task 8e5aa63b) — so the rail's gate row and this card's own
            // banner-style summary below derive from identical input and
            // cannot disagree.
            gateReadiness={gateReadiness}
            phase={c.phase}
            status={c.status}
            activity={c.status === "done" ? null : c.activity}
            gates={c.timeline?.gates ?? []}
            turns={c.turns ?? []}
            stepTokens={c.stepTokens}
            reduced={reduced}
            evidence={evidence}
            proofType={proofType}
            completion={{ proofType, proof: completionProof, misfire: likelyMisfire, fullOutcome: fullOutcomeComplete, taskId }}
          />

          {c.status === "done" && (c.gateState === "pending" || c.gateState === "failed") && (
            <div className="mt-3 text-[12px] text-[color:var(--text-muted)] leading-relaxed">
              Task closed as done while the conductor flow was parked at{" "}
              <span className="font-mono">{c.step?.replace(/_/g, " ")}</span> — the remaining
              ceremony was waived by the owner; completion proof lives on the task (audit in history).
            </div>
          )}

          {c.gateReason && c.gateState !== "pending" && onValidation && (
            <button
              onClick={onValidation}
              className="mt-3 flex items-center gap-1.5 text-left group"
            >
              <span className="text-2xs uppercase tracking-wider text-[color:var(--text-muted)]">
                {c.gateState === "failed" ? "failure reason" : "validation"}
              </span>
              <span className="text-[12px] leading-snug opacity-80 group-hover:opacity-100 truncate max-w-[520px]">{c.gateReason}</span>
              <span className="opacity-50 group-hover:opacity-100 shrink-0">→</span>
            </button>
          )}

          {c.status !== "done" && (c.gateState === "pending" || c.gateState === "failed") && gate && (
            <div className="mt-4 pt-4 border-t border-[color:var(--midground-base)]/15">
              {/* Show the evidence FIRST — what the reviewer is approving —
                  then the approve/reject control directly beneath it. */}
              <GateEvidence step={c.step} turns={c.turns ?? []} />
              {c.gateState === "failed" && (
                <div
                  className="mt-4 rounded-md p-3 text-[12.5px] leading-relaxed"
                  style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)", boxShadow: "inset 0 0 0 1px var(--accent-rose-ring)" }}
                >
                  <span className="uppercase tracking-wider text-2xs mr-2">last decision · gate failed</span>
                  {c.gateReason || "no reason recorded"}
                  <div className="opacity-70 mt-1 text-[11.5px]">
                    This is the stored snapshot from the LAST decision — the live evidence check below is current truth.
                  </div>
                </div>
              )}
              {gateReadiness && (
                <div
                  className="mt-4 rounded-md p-3 text-[12.5px] leading-relaxed"
                  style={!gateReadiness.receipt_ok
                    ? { background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }
                    : gateReadiness.manual_review && gateReadiness.receipt?.adapter === "human"
                      ? { background: "var(--accent-amber-bg)", color: "var(--accent-amber-fg)", boxShadow: "inset 0 0 0 1px var(--accent-amber-ring)" }
                      : { background: "var(--accent-sage-bg)", color: "var(--accent-sage-fg)", boxShadow: "inset 0 0 0 1px var(--accent-sage-ring)" }}
                >
                  {/* receipt_ok=true can mean "a human review is the accepted
                      evidence route" (adapter="human", manual_review=true) -
                      NOT that anything passed (task 72d3e0d1). An epic
                      roll-up sets manual_review too but over REAL child
                      evidence (adapter="epic-rollup"), so it keeps the
                      affirmative branch below. */}
                  <span className="uppercase tracking-wider text-2xs mr-2">evidence check · live</span>
                  {gateReadiness.receipt_ok && gateReadiness.manual_review && gateReadiness.receipt?.adapter === "human" ? (
                    <>
                      Awaiting your review — no machine evidence at this tree
                      {gateReadiness.receipt?.reason ? `: ${gateReadiness.receipt.reason}` : ""}.
                      <div className="opacity-90 mt-1">
                        <b>Action:</b> this oracle has no machine runner — your review IS the sign-off.
                        Look at the evidence, then type a reason and click Approve.
                      </div>
                    </>
                  ) : gateReadiness.receipt_ok ? (
                    <>
                      ✓ Oracle evidence receipt is FRESH and PASSING
                      {gateReadiness.receipt?.adapter ? ` (${gateReadiness.receipt.adapter} — a real run, not a claim)` : ""}.
                      <div className="opacity-90 mt-1">
                        <b>Action:</b> type a reason and click Approve — the evidence tooth passes now.
                        If the shell verifier still refuses (known engine defect 68e5c699), check override:
                        that is the audited manual release, and your evidence is already on file.
                      </div>
                    </>
                  ) : (
                    <>
                      Oracle evidence is NOT ready: {gateReadiness.receipt_refusal || "no receipt on file"}
                      <div className="opacity-90 mt-1 flex items-center gap-3 flex-wrap">
                        <b>Action:</b>
                        {onMintEvidence && (
                          <button
                            type="button"
                            onClick={onMintEvidence}
                            className="text-2xs uppercase tracking-wider px-2.5 py-1 rounded"
                            style={{ background: "var(--accent-amber-fg)", color: "var(--accent-amber-bg)" }}
                            title="run the oracle now, inside the daemon, and mint a fresh evidence receipt"
                          >
                            ↻ re-run oracle now
                          </button>
                        )}
                        <span>then Approve — or check override to release on manual judgment (audited).</span>
                      </div>
                    </>
                  )}
                </div>
              )}
              <div className="opacity-50 mb-2 mt-4 text-2xs uppercase tracking-wider">
                {c.gateState === "failed" ? "Recover gate" : "Resolve gate"}
              </div>
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
                  className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
                  style={{ background: "var(--accent-emerald-bg)", color: "var(--accent-emerald-fg)" }}
                  title={c.gateState === "failed" ? "re-runs the machine check on current evidence; override forces past a failing check" : undefined}
                >
                  {c.gateState === "failed" ? "Approve (recover)" : "Approve"}
                </button>
                {c.gateState !== "failed" && (
                <button
                  type="button"
                  disabled={gate.busy || !gate.reason.trim()}
                  onClick={() => gate.decide("reject")}
                  className="text-2xs uppercase tracking-wider px-3 py-1.5 rounded disabled:opacity-40"
                  style={{ background: "var(--accent-rose-bg)", color: "var(--accent-rose-fg)" }}
                >
                  Reject
                </button>
                )}
              </div>
              {!gate.reason.trim() && (
                <div className="text-2xs mt-1.5 text-[color:var(--text-muted)]">
                  type a reason above to enable the button{c.gateState === "failed" ? "" : "s"}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
