// Clickable cross-reference token -- slice S3.
//
// A MARKED code token (a markdown backtick span, or an OKF evidence file_path)
// is resolved EAGERLY on mount via the micro-batched lib/xref. The Brain only
// indexes a fraction of what prose references, so MOST chips do not resolve --
// those render as an ordinary inert code chip (exactly the old `<code>` look),
// NOT a broken/strikethrough link. Only a token that genuinely resolves to a
// concept/symbol/file becomes a teal, clickable link. This is the difference
// between "nearly every chip says could not resolve" and a clean page where the
// few navigable references quietly stand out.
//
// Styling is literal Hermes --accent-*/--text-*/--surface-* CSS vars (same
// palette source as <Markdown>'s code chip + link) -- no hardcoded hex.
import { useEffect, useState, type MouseEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useProject } from "@/lib/project";
import { resolveXref, type XrefResolution } from "@/lib/xref";

const CHIP_BASE =
  "text-[0.85em] font-mono px-1 py-0.5 rounded break-all align-baseline";

// Inert code chip: what a token that does not resolve looks like (neutral, no
// link affordance) -- visually the plain inline `<code>` it would have been.
const PLAIN =
  "bg-[color:var(--surface-2)] text-[color:var(--text-secondary)]";

// Navigable token: teal, underlined, clickable.
const LINK =
  "bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] " +
  "cursor-pointer underline decoration-dotted underline-offset-2 hover:opacity-80";

export default function XrefLink({ token }: { token: string }) {
  const navigate = useNavigate();
  const [project] = useProject();
  const [res, setRes] = useState<XrefResolution | null>(null);

  // Resolve up front (coalesced into one batch per doc). Until it settles, and
  // for anything that does not resolve, the chip stays plain -- never a link
  // that goes nowhere.
  useEffect(() => {
    let alive = true;
    resolveXref(token, project).then((r) => {
      if (alive) setRes(r);
    });
    return () => {
      alive = false;
    };
  }, [token, project]);

  const navigable = !!res && res.kind !== "unresolved" && !!res.href;

  if (!navigable) {
    return <code className={`${CHIP_BASE} ${PLAIN}`}>{token}</code>;
  }

  const href = res!.href;
  const onActivate = (e: MouseEvent<HTMLAnchorElement>) => {
    e.preventDefault();
    if (/^https?:\/\//.test(href)) {
      window.open(href, "_blank", "noreferrer");
      return;
    }
    navigate(href); // SPA route -- no full page reload
  };

  return (
    <a
      href={href}
      onClick={onActivate}
      title={res!.summary}
      className={`${CHIP_BASE} ${LINK}`}
    >
      {res!.label || token}
    </a>
  );
}
