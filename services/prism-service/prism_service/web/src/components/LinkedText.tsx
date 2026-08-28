// LinkedText — plain-text surfaces (signal subject/body, list rows, task
// oracle prose) cross-clicked to their ontology data (task 6968cc39, epic
// 47bba8fe, owner relayed: "make sure we have cross clicking on every noun
// verb etc in the tasks"). Fetches entity_linker.link()'s spans for `text`
// on mount and renders the matched ranges as <Link>/<a>, everything else as
// plain text — never a rewrite of the string itself, so unresolved text
// looks exactly as it always did. TaskDetailPage/UnderstandPage keep their
// MARKDOWN surfaces on the separate splice-into-markdown-source path
// (api.ts's spliceLinkedMarkdown) so <Markdown> stays the one renderer for
// those; this component is for text that was never markdown to begin with.
//
// A LOCAL, SYNCHRONOUS pass (task 2bfe49db, epic 61821448: "the law runs
// over a diff at the gates") runs FIRST, before the async entity_linker
// spans: services/law_check.py's green_gate reason names a promoted rule
// ("Rule <name>"), the memory it came from ("mx-<id>"), and the module
// paths involved, and this text renders on task.gate_reason /
// task.blocked_reason the moment the gate parks — it must not wait on a
// network round trip, and a just-promoted rule/memory may not even be
// indexed by entity_linker's ontology-backed lookup yet. "Rule <name>"
// links straight to the Ontology page's Rules tab; "mx-<id>" links to the
// same Understand memory route entity_linker itself uses
// (_memory_href); a bare module path (e.g. "models/x.py") renders as
// plain <code> text, never a link — the file may not exist as its own
// ontology Document yet. Local spans win over any overlapping async span.
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { linkText, type LinkedSpan } from "@/lib/api";
import { useProject } from "@/lib/project";

const RULE_RE = /\bRule ([a-z0-9-]+)\b/g;
const MEMORY_RE = /\bmx-[0-9a-f]{6,}\b/g;
const MODULE_PATH_RE = /\b[\w][\w./-]*\.py\b/g;

type LocalSpan = { start: number; end: number; node: ReactNode };

function localSpans(text: string): LocalSpan[] {
  const found: { start: number; end: number; node: ReactNode }[] = [];
  for (const m of text.matchAll(RULE_RE)) {
    const start = m.index ?? 0;
    found.push({
      start, end: start + m[0].length,
      node: <Link to="/ontology?tab=rules" title={`Rule · ${m[1]}`}
                 className="underline decoration-dotted underline-offset-2 text-[color:var(--accent-teal-fg)] hover:opacity-80">
              {m[0]}
            </Link>,
    });
  }
  for (const m of text.matchAll(MEMORY_RE)) {
    const start = m.index ?? 0;
    found.push({
      start, end: start + m[0].length,
      node: <Link to={`/understand?concept=${encodeURIComponent(m[0])}`} title={`Memory · ${m[0]}`}
                 className="underline decoration-dotted underline-offset-2 text-[color:var(--accent-teal-fg)] hover:opacity-80">
              {m[0]}
            </Link>,
    });
  }
  for (const m of text.matchAll(MODULE_PATH_RE)) {
    const start = m.index ?? 0;
    found.push({
      start, end: start + m[0].length,
      node: <code className="font-mono text-2xs">{m[0]}</code>,
    });
  }
  found.sort((a, b) => a.start - b.start);
  const out: LocalSpan[] = [];
  let lastEnd = -1;
  for (const s of found) {
    if (s.start >= lastEnd) { out.push(s); lastEnd = s.end; }
  }
  return out;
}

export default function LinkedText({ text, className }: { text: string; className?: string }) {
  const [project] = useProject();
  const [spans, setSpans] = useState<LinkedSpan[]>([]);

  useEffect(() => {
    let alive = true;
    setSpans([]);
    linkText(project, text ?? "").then((s) => { if (alive) setSpans(s); }).catch(() => {});
    return () => { alive = false; };
  }, [text, project]);

  if (!text) return null;

  const local = localSpans(text);
  // Server spans that overlap a local (Rule/mx-/path) span are dropped —
  // the local, synchronous rendering wins so a just-parked gate reason
  // never waits on entity_linker's ontology-backed lookup.
  const remote = spans
    .filter((s) => !local.some((l) => s.start < l.end && s.end > l.start))
    .map((s, i) => ({ start: s.start, end: s.end, node: <EntitySpan key={`r${i}`} span={s} /> }));
  const all = [...local, ...remote].sort((a, b) => a.start - b.start);

  if (all.length === 0) return <span className={className}>{text}</span>;

  const nodes: ReactNode[] = [];
  let last = 0;
  all.forEach((s, i) => {
    if (s.start > last) nodes.push(text.slice(last, s.start));
    nodes.push(<span key={i}>{s.node}</span>);
    last = s.end;
  });
  if (last < text.length) nodes.push(text.slice(last));
  return <span className={className}>{nodes}</span>;
}

// One resolved span — a real <Link> for an in-app href, a new-tab <a> for an
// external one, or a plain inert span (still tagged data-cls) when the
// entity is known but has no destination yet (e.g. a Person with no actor
// page). Every variant carries data-cls + a "Class · label" title tooltip.
function EntitySpan({ span }: { span: LinkedSpan }) {
  const title = `${span.cls} · ${span.label}`;
  const cls = "underline decoration-dotted underline-offset-2 text-[color:var(--accent-teal-fg)] hover:opacity-80";
  if (!span.href) {
    return <span data-cls={span.cls} title={title}>{span.text}</span>;
  }
  if (/^https?:\/\//.test(span.href)) {
    return (
      <a href={span.href} target="_blank" rel="noreferrer" data-cls={span.cls} title={title} className={cls}>
        {span.text}
      </a>
    );
  }
  return (
    <Link to={span.href} data-cls={span.cls} title={title} className={cls}>
      {span.text}
    </Link>
  );
}
