// Readable renderer for receipt-style evidence text (gate validation,
// completion proof) — these arrive as one long line with `code` spans and
// "Label: detail" clauses. Splits into rows, renders `code` as teal mono chips,
// tones the leading Label, and highlights pass/fail/green result tokens. Uses
// the shared --accent-* / --text-* palette (same as /understand). No <pre>.
import { type ReactNode } from "react";
import { highlight } from "@/components/Markdown";

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

export default function EvidenceView({ text }: { text: string }) {
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
