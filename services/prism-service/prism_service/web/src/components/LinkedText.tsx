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
import { useEffect, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { linkText, type LinkedSpan } from "@/lib/api";
import { useProject } from "@/lib/project";

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
  if (spans.length === 0) return <span className={className}>{text}</span>;

  const nodes: ReactNode[] = [];
  let last = 0;
  spans.forEach((s, i) => {
    if (s.start > last) nodes.push(text.slice(last, s.start));
    nodes.push(<EntitySpan key={i} span={s} />);
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
