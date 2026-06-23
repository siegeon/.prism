// Shared markdown renderer — the ONE block-markdown + verdict-token renderer
// for the whole SPA. Fuses OnboardingView's dependency-free block parser
// (H1/H2/H3, paragraphs, `-/*` unordered lists, inline `code` + **bold**) with
// EvidenceView's verdict-token highlighter (PASSED/GREEN/"N passed"→emerald,
// warning→amber, FAIL(ED)→rose). Every color flows from the Hermes
// --accent-*/--text-*/--surface-* CSS vars — no hardcoded hex, no tailwind
// color-palette bypass, no <pre>. OnboardingView + EvidenceView delegate here
// so the parser/highlighter logic lives in exactly one place (AC-1).
import { useMemo, type ReactNode } from "react";

export default function Markdown({ text, className }: { text: string; className?: string }) {
  const blocks = useMemo(() => parseMarkdown(text ?? ""), [text]);
  if (!text || !text.trim() || blocks.length === 0) return null;
  return <div className={className ?? "prose-like space-y-4 max-w-[840px]"}>{blocks}</div>;
}


// Block parser — splits raw markdown into structured heading/list/paragraph
// blocks (carried over verbatim from OnboardingView's parseMarkdown).
export function parseMarkdown(text: string): ReactNode[] {
  const lines = text.split(/\r?\n/);
  const blocks: ReactNode[] = [];
  let i = 0;
  let para: string[] = [];
  let listItems: string[] = [];

  const flushPara = () => {
    if (para.length === 0) return;
    blocks.push(
      <p key={`p${blocks.length}`} className="text-sm leading-relaxed text-[color:var(--text-secondary)]">
        {renderInline(para.join(" "))}
      </p>,
    );
    para = [];
  };
  const flushList = () => {
    if (listItems.length === 0) return;
    blocks.push(
      <ul key={`ul${blocks.length}`} className="text-sm leading-relaxed text-[color:var(--text-secondary)] list-disc pl-5 space-y-1">
        {listItems.map((it, k) => (
          <li key={k}>{renderInline(it)}</li>
        ))}
      </ul>,
    );
    listItems = [];
  };

  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); i++; continue; }

    const h1 = /^#\s+(.*)$/.exec(line);
    const h2 = /^##\s+(.*)$/.exec(line);
    const h3 = /^###\s+(.*)$/.exec(line);
    const bul = /^[-*]\s+(.*)$/.exec(line);

    if (h1) {
      flushPara(); flushList();
      blocks.push(
        <h1 key={`h${blocks.length}`} className="font-serif text-2xl tracking-tight">
          {renderInline(h1[1])}
        </h1>,
      );
    } else if (h2) {
      flushPara(); flushList();
      blocks.push(
        <h2 key={`h${blocks.length}`} className="font-serif text-xl tracking-tight pt-2 border-t border-[color:var(--midground-base)]/10">
          {renderInline(h2[1])}
        </h2>,
      );
    } else if (h3) {
      flushPara(); flushList();
      blocks.push(
        <h3 key={`h${blocks.length}`} className="font-serif text-base tracking-tight">
          {renderInline(h3[1])}
        </h3>,
      );
    } else if (bul) {
      flushPara();
      listItems.push(bul[1]);
    } else {
      flushList();
      para.push(line);
    }
    i++;
  }
  flushPara(); flushList();
  return blocks;
}


// Verdict-token highlighter — colorizes test/gate result tokens so the signal
// pops out of prose (carried over from EvidenceView's highlight()). PASSED /
// GREEN / "N passed" / ok:true -> emerald, warning -> amber, FAIL(ED) /
// ok:false -> rose. Each tone reads its explicit Hermes CSS var (literal so the
// palette source is greppable + can't drift into a tailwind/hex bypass).
const TONE_FG: Record<string, string> = {
  emerald: "var(--accent-emerald-fg)",
  amber: "var(--accent-amber-fg)",
  rose: "var(--accent-rose-fg)",
};
export function highlight(s: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\d[\d,.]*\s+(?:passed|failed|warning|tests?)|ok:true|ok:false|GREEN|PASSED|FAIL(?:ED)?)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let k = 0;
  while ((m = re.exec(s))) {
    if (m.index > last) out.push(s.slice(last, m.index));
    const tok = m[0];
    const tone = /passed|ok:true|GREEN|PASSED/.test(tok)
      ? "emerald"
      : /warning/.test(tok)
        ? "amber"
        : "rose";
    out.push(
      <span key={`h${k++}`} className="font-medium" style={{ color: TONE_FG[tone] }}>
        {tok}
      </span>,
    );
    last = m.index + tok.length;
  }
  if (last < s.length) out.push(s.slice(last));
  return out;
}


// Inline renderer — `code` spans (teal mono chips), **bold**, and verdict-token
// coloring on the remaining plain text. Code first so `**...**` inside backticks
// stays literal; plain runs flow through highlight() for verdict tones.
export function renderInline(text: string): ReactNode {
  const parts: ReactNode[] = [];
  let rest = text;
  let key = 0;
  while (rest.length > 0) {
    const code = /`([^`]+)`/.exec(rest);
    const bold = /\*\*([^*]+)\*\*/.exec(rest);
    const next = pickFirst(code, bold);
    if (!next) { parts.push(<span key={key++}>{highlight(rest)}</span>); break; }
    if (next.index > 0) parts.push(<span key={key++}>{highlight(rest.slice(0, next.index))}</span>);
    if (next === code) {
      parts.push(
        <code key={key++} className="text-[12px] font-mono px-1 py-0.5 rounded bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] break-all">
          {next[1]}
        </code>,
      );
    } else {
      parts.push(<strong key={key++} className="font-semibold">{next[1]}</strong>);
    }
    rest = rest.slice(next.index + next[0].length);
  }
  return parts;
}

function pickFirst(...matches: (RegExpExecArray | null)[]): RegExpExecArray | null {
  let best: RegExpExecArray | null = null;
  for (const m of matches) {
    if (m && (best === null || m.index < best.index)) best = m;
  }
  return best;
}
