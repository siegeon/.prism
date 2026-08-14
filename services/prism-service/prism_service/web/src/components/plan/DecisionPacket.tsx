import { useState, useEffect } from "react";
import { EvidenceGallery } from "@/components/EvidenceGallery";

// The server-assembled evidence packet (task a1e4120f, slices 2/4). Every field
// is sourced from a REAL artifact by GET /api/conductor/decision-packet — the
// approval gate never shows a bare "No recorded evidence" box again.
type Packet = {
  state: "pending" | "failed" | "recovered" | "waived" | "done";
  diff_stat: {
    files: number;
    insertions: number;
    deletions: number;
    entries: { path: string; insertions: number; deletions: number }[];
  };
  commits: { sha: string; subject: string }[];
  receipt: { adapter: string; passed: boolean; status: string; ended_at: string; reason: string } | null;
  screenshots: { name: string; url: string }[];
  // task 31c345b7: the story/plan text THIS gate is deciding on. Only
  // non-null at story_gate/plan_gate — post-code gates keep None here.
  subject: { kind: string; title: string; markdown: string; diagram: string } | null;
};

// AC-5: card state -> label + tone, mapped onto the existing Hermes accent vars.
const TONE: Record<Packet["state"], { label: string; bg: string; fg: string; ring: string }> = {
  pending:   { label: "Pending",   bg: "--accent-amber-bg", fg: "--accent-amber-fg", ring: "--accent-amber-ring" },
  failed:    { label: "Failed",    bg: "--accent-rose-bg",  fg: "--accent-rose-fg",  ring: "--accent-rose-ring" },
  recovered: { label: "Recovered", bg: "--accent-sage-bg",  fg: "--accent-sage-fg",  ring: "--accent-sage-ring" },
  waived:    { label: "Waived",    bg: "--accent-amber-bg", fg: "--text-muted",      ring: "--midground-base" },
  done:      { label: "Done",      bg: "--accent-sage-bg",  fg: "--accent-sage-fg",  ring: "--accent-sage-ring" },
};

function Row({ icon, title, note, summary, empty, children }: {
  icon: string; title: string; note?: string; summary: string; empty: boolean; children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-[color:var(--midground-base)]/15 first:border-t-0">
      <button
        onClick={() => !empty && setOpen((o) => !o)}
        className="w-full flex items-center gap-2.5 py-2 text-left"
        style={{ cursor: empty ? "default" : "pointer" }}
      >
        <span className="text-[color:var(--text-muted)] text-[11px] w-2.5 shrink-0" style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s" }}>▸</span>
        <span className="w-4 text-center shrink-0 text-[12px]">{icon}</span>
        <span className="text-[12.5px] font-medium shrink-0">{title}</span>
        {/* A row that names its own source can't be misread as another row's
            fact (mx-352a3d, v7.1.24) — stays visible while collapsed. */}
        {note && <span className="text-[11px] font-mono truncate text-[color:var(--text-muted)]">{note}</span>}
        <span className="ml-auto text-[12px] text-[color:var(--text-secondary)] shrink-0">{empty ? "none" : summary}</span>
      </button>
      {open && !empty && <div className="pb-3 pl-9 pr-1">{children}</div>}
    </div>
  );
}

const mono = "font-mono text-[11.5px] leading-relaxed text-[color:var(--text-secondary)]";

export default function DecisionPacket({ taskId, project, state, step, latestReceipt }: {
  taskId?: string; project?: string; state?: string; step?: string;
  // The panel's own view of the newest receipt (tree_sha/job_id from
  // GET /api/tasks/:id/tests) — the packet fetches only a projection with no
  // tree on it, so the tree it was measured at is handed down.
  latestReceipt?: { job_id: string; tree_sha: string } | null;
}) {
  const [pkt, setPkt] = useState<Packet | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    if (!taskId) return;
    let live = true;
    setPkt(null); setErr(false);
    fetch(`/api/conductor/decision-packet?task_id=${taskId}&project=${project ?? "default"}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then((d) => { if (live) setPkt(d as Packet); })
      .catch(() => { if (live) setErr(true); });
    return () => { live = false; };
  }, [taskId, project, step]);

  if (err || !pkt) return null; // fall back to the sibling GateEvidence note
  const ds = pkt.diff_stat;
  // Prefer the live gate_state passed by the card; else the packet's own state.
  const st = (["pending", "failed", "recovered", "waived", "done"].includes(state ?? "")
    ? (state as Packet["state"]) : pkt.state);
  const tone = TONE[st] ?? TONE.pending;
  // The tree the latest receipt was measured at: preferred from the panel's
  // receipt context, else the packet's own head commit.
  const receiptSha = latestReceipt?.tree_sha || pkt.commits[0]?.sha || "";

  return (
    <div
      className="rounded-md p-3"
      style={{ background: `var(${tone.bg})`, boxShadow: `inset 0 0 0 1px var(${tone.ring})` }}
    >
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-2xs uppercase tracking-wider font-medium" style={{ color: `var(${tone.fg})` }}>
          Decision packet · server-assembled
        </span>
        <span
          className="ml-auto text-2xs uppercase tracking-wider font-semibold px-2 py-0.5 rounded-full"
          style={{ color: `var(${tone.fg})`, boxShadow: `inset 0 0 0 1px var(${tone.ring})` }}
        >
          {tone.label}
        </span>
      </div>

      {/* Subject channel (task 31c345b7): the story/plan text this gate is
          actually deciding on. Only story_gate/plan_gate carry a subject —
          post-code gates keep the four rows below untouched (epic stop_if:
          no panel grows at a gate that already has full evidence). */}
      {(step === "story_gate" || step === "plan_gate") && (
        <Row icon="§" title={pkt.subject ? pkt.subject.title : "Story / plan under judgment"}
             empty={!pkt.subject} summary={pkt.subject ? "1" : ""}>
          {pkt.subject && (
            <div className={mono} style={{ whiteSpace: "pre-wrap" }}>{pkt.subject.markdown}</div>
          )}
          {pkt.subject?.diagram && <pre className={mono}>{pkt.subject.diagram}</pre>}
        </Row>
      )}

      <Row icon="◨" title="Diff vs baseline" empty={ds.files === 0}
           summary={`+${ds.insertions} −${ds.deletions} · ${ds.files} files`}>
        <ul className={mono}>
          {ds.entries.map((e) => (
            <li key={e.path} className="flex gap-3">
              <span className="text-[color:var(--accent-sage-fg)]">+{e.insertions}</span>
              <span className="text-[color:var(--accent-rose-fg)]">−{e.deletions}</span>
              <span className="truncate">{e.path}</span>
            </li>
          ))}
        </ul>
      </Row>

      <Row icon="⎇" title="Commits" empty={pkt.commits.length === 0}
           summary={`${pkt.commits.length}`}>
        <ul className={mono}>
          {pkt.commits.map((c) => (
            <li key={c.sha}><span className="opacity-60">{c.sha}</span> {c.subject}</li>
          ))}
        </ul>
      </Row>

      {/* This receipt is oracle_spec.latest_receipt() (services/decision_packet.py:86-98):
          the NEWEST receipt on the task, whatever gate it served, whatever tree
          it measured. Unlabelled it reads as this gate's verdict and contradicts
          the gate-readiness row next to it (task 5a6837a0). */}
      <Row icon="⚚" title="Oracle receipt" empty={!pkt.receipt}
           note={`latest on this task · any gate${receiptSha ? ` · ${receiptSha.slice(0, 7)}` : ""}`}
           summary={pkt.receipt ? `${pkt.receipt.adapter} · ${pkt.receipt.status}` : ""}>
        <div className={mono}>
          {pkt.receipt && (
            <>
              <div>{pkt.receipt.passed ? "passed" : "not passed"} · {pkt.receipt.adapter} · {pkt.receipt.ended_at}</div>
              <div className="opacity-80 mt-1">{pkt.receipt.reason}</div>
            </>
          )}
          <div className="opacity-70 mt-1">
            the latest receipt on this task{receiptSha ? `, measured at ${receiptSha.slice(0, 7)}` : ""} — it does not
            necessarily serve {step || "this gate"}; the gate's own tooth is the "oracle receipt" check row below.
          </div>
        </div>
      </Row>

      {/* "Visual evidence", not "Screenshots" (owner 2026-07-29). The row is
          what a reviewer scans for when deciding whether they can SEE the
          outcome; naming it after the file type buried that. The screenshots
          themselves still live inside it. */}
      <Row icon="▣" title="Visual evidence" empty={pkt.screenshots.length === 0}
           summary={`${pkt.screenshots.length}`}>
        {/* Open evidence IN-APP (owner: a screenshot must open in the viewer,
            not navigate the browser to the raw .png). Reuse the shared
            EvidenceGallery lightbox. */}
        <EvidenceGallery
          thumb="sm"
          items={pkt.screenshots.map((s) => ({
            url: s.url, name: s.name, kind: "image" as const,
          }))}
        />
      </Row>
    </div>
  );
}
