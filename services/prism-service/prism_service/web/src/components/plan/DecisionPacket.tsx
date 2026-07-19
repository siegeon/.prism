import { useState, useEffect } from "react";

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
};

// AC-5: card state -> label + tone, mapped onto the existing Hermes accent vars.
const TONE: Record<Packet["state"], { label: string; bg: string; fg: string; ring: string }> = {
  pending:   { label: "Pending",   bg: "--accent-amber-bg", fg: "--accent-amber-fg", ring: "--accent-amber-ring" },
  failed:    { label: "Failed",    bg: "--accent-rose-bg",  fg: "--accent-rose-fg",  ring: "--accent-rose-ring" },
  recovered: { label: "Recovered", bg: "--accent-sage-bg",  fg: "--accent-sage-fg",  ring: "--accent-sage-ring" },
  waived:    { label: "Waived",    bg: "--accent-amber-bg", fg: "--text-muted",      ring: "--midground-base" },
  done:      { label: "Done",      bg: "--accent-sage-bg",  fg: "--accent-sage-fg",  ring: "--accent-sage-ring" },
};

function Row({ icon, title, summary, empty, children }: {
  icon: string; title: string; summary: string; empty: boolean; children?: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-[color:var(--midground-base)]/15 first:border-t-0">
      <button
        onClick={() => !empty && setOpen((o) => !o)}
        className="w-full flex items-center gap-2.5 py-2 text-left"
        style={{ cursor: empty ? "default" : "pointer" }}
      >
        <span className="text-[color:var(--text-muted)] text-[10px] w-2.5 shrink-0" style={{ transform: open ? "rotate(90deg)" : "none", transition: "transform .15s" }}>▸</span>
        <span className="w-4 text-center shrink-0 text-[12px]">{icon}</span>
        <span className="text-[12.5px] font-medium">{title}</span>
        <span className="ml-auto text-[12px] text-[color:var(--text-secondary)]">{empty ? "none" : summary}</span>
      </button>
      {open && !empty && <div className="pb-3 pl-9 pr-1">{children}</div>}
    </div>
  );
}

const mono = "font-mono text-[11.5px] leading-relaxed text-[color:var(--text-secondary)]";

export default function DecisionPacket({ taskId, project, state, step }: {
  taskId?: string; project?: string; state?: string; step?: string;
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

      <Row icon="⚚" title="Oracle receipt" empty={!pkt.receipt}
           summary={pkt.receipt ? `${pkt.receipt.adapter} · ${pkt.receipt.status}` : ""}>
        <div className={mono}>
          {pkt.receipt && (
            <>
              <div>{pkt.receipt.passed ? "passed" : "not passed"} · {pkt.receipt.adapter} · {pkt.receipt.ended_at}</div>
              <div className="opacity-80 mt-1">{pkt.receipt.reason}</div>
            </>
          )}
        </div>
      </Row>

      <Row icon="▣" title="Screenshots" empty={pkt.screenshots.length === 0}
           summary={`${pkt.screenshots.length}`}>
        <div className="flex gap-2 flex-wrap">
          {pkt.screenshots.map((s) => (
            <a key={s.name} href={s.url} target="_blank" rel="noreferrer" title={s.name}>
              <img src={s.url} alt={s.name} className="h-16 rounded border border-[color:var(--midground-base)]/20" />
            </a>
          ))}
        </div>
      </Row>
    </div>
  );
}
