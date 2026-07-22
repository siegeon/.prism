// Readable renderer for receipt-style evidence text (gate validation,
// completion proof) — these arrive as one long line with `code` spans and
// "Label: detail" clauses. Splits into rows, renders `code` as teal mono chips,
// tones the leading Label, and highlights pass/fail/green result tokens. Uses
// the shared --accent-* / --text-* palette (same as /understand). No <pre>.
//
// Task 25a25d84: also accepts a `taskId` so the page can thread through the
// RECEIPT-captured evidence (GET /api/tasks/:id/gate_evidence) — screenshot,
// walkthrough video, verbatim assertion source, and provenance — via the
// SAME GateEvidenceBlock the gate rail/live-bar chips expand into, so this
// full-page view reads identically to the inline ones. The ![img](url)
// legacy markdown path below stays for hand-authored proof text, extended to
// render a real <video controls> when the cited file is a .mp4/.webm.
import { type ReactNode } from "react";
import { highlight } from "@/components/Markdown";
import { GateEvidenceBlock } from "@/components/conductor/TaskActivityGantt";

// NUL sentinel for masking code spans — cannot appear in real evidence text.
const SENT = String.fromCharCode(0);

// Verdict/result token coloring is the shared highlight() from <Markdown>; this
// view keeps its own receipt-style row layout (Label: detail clauses) on top.
function renderInline(s: string): ReactNode[] {
  // odd indices are the contents of `backtick` spans
  return s.split(/`([^`]+)`/g).map((p, i) =>
    i % 2 === 1 ? (
      <code
        key={`c${i}`}
        className="font-mono text-2xs px-1.5 py-0.5 rounded bg-[color:var(--surface-3)] text-[color:var(--accent-teal-fg)] break-all"
      >
        {p}
      </code>
    ) : (
      <span key={`t${i}`}>{highlight(p)}</span>
    ),
  );
}

function isVideoSrc(src: string): boolean {
  return /\.(mp4|webm)(\?.*)?$/i.test(src);
}

export default function EvidenceView({ text, taskId, project, proofType }: {
  text: string;
  // Optional: renders the task's RECEIPT-captured evidence block (task
  // 25a25d84) below the prose — omitted by callers with no task in scope.
  taskId?: string;
  project?: string;
  proofType?: string;
}) {
  // Evidence images — proof text citing ![alt](src) renders the actual image
  // (or, when the cited file is a .mp4/.webm, a real <video controls>) as an
  // inline figure (owner 2026-07-16: what a gate approves must be VISIBLE in
  // PRISM, never described-but-elsewhere). Split image markdown out FIRST so
  // the receipt-row sentence machinery below can't mangle it.
  const parts = text.split(/(!\[[^\]]*\]\([^)\s]+\))/g);
  const body = parts.length > 1 ? (
    <div className="space-y-3">
      {parts.map((part, i) => {
        const img = /^!\[([^\]]*)\]\(([^)\s]+)\)$/.exec(part.trim());
        if (img) {
          const [, alt, src] = img;
          return isVideoSrc(src) ? (
            <video
              key={`vid${i}`}
              src={src}
              controls
              className="max-w-full rounded-md border border-[color:var(--border-default)]"
            />
          ) : (
            <img
              key={`img${i}`}
              src={src}
              alt={alt}
              title={alt}
              loading="lazy"
              className="max-w-full rounded-md border border-[color:var(--border-default)]"
            />
          );
        }
        return part.trim() ? <EvidenceRows key={`rows${i}`} text={part} /> : null;
      })}
    </div>
  ) : (
    <EvidenceRows text={text} />
  );

  if (!taskId) return body;
  return (
    <div className="space-y-4">
      {body}
      <div className="pt-3 border-t border-[color:var(--border-default)] space-y-2.5">
        <div className="text-2xs uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
          receipt-captured evidence
        </div>
        <GateEvidenceBlock taskId={taskId} project={project} proofType={proofType} />
      </div>
    </div>
  );
}

function EvidenceRows({ text }: { text: string }) {
  // Mask `code` spans with a NUL-wrapped index so sentence-splitting never
  // breaks on dots inside code (e.g. `.dict()`, `api/tasks.py:80`).
  const masked: string[] = [];
  const safe = text.replace(/`[^`]+`/g, (mm) => {
    masked.push(mm);
    return SENT + (masked.length - 1) + SENT;
  });
  const restore = (x: string) =>
    x.replace(new RegExp(SENT + "(\\d+)" + SENT, "g"), (_, i) => masked[Number(i)]);
  const rows = safe
    .split(/(?<=[.;])\s+(?=[-A-Z( ])/)
    .map((x) => x.trim())
    .filter(Boolean);

  return (
    <div className="space-y-1.5 max-w-[880px]">
      {rows.map((raw, i) => {
        const full = restore(raw.replace(/^[-•]\s*/, ""));
        const m = full.match(/^([A-Z][\w ./()\-]{2,44}?):\s+([\s\S]*)$/);
        return (
          <div key={i} className="flex gap-2.5 text-sm leading-relaxed">
            <span className="text-[color:var(--accent-violet-fg)] select-none mt-[3px] shrink-0">›</span>
            <p className="text-[color:var(--text-secondary)] min-w-0">
              {m ? (
                <>
                  <span className="text-[color:var(--text-primary)] font-medium">{m[1]}</span>
                  {": "}
                  {renderInline(m[2])}
                </>
              ) : (
                renderInline(full)
              )}
            </p>
          </div>
        );
      })}
    </div>
  );
}
