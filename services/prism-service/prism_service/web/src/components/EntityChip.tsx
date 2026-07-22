/**
 * EntityChip — the artifact's `.ent`: THE unit of connectedness. A typed
 * glyph + a name, rendered everywhere an entity is mentioned. When `to` is
 * set it's a router <Link>; otherwise an inert <span>.
 *
 * Usage:
 *   <EntityChip kind="code" label="api/jira_sync.py" to="/code/api/jira_sync.py" />
 *   <EntityChip kind="memory" label="session-task-live-binding" />
 *   <EntityChip kind="gate" label="green_gate" title="awaiting review" />
 *
 * The glyph is also exported on its own as <GlyphIcon> so other surfaces
 * (tables, rails, graph legends) can reuse the exact shapes. Shape encodes
 * TYPE (survives grayscale + colorblindness); the --et-<kind> fill reinforces.
 */
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { cn } from "@/lib/utils";

export type EntityKind =
  | "code" | "memory" | "task" | "test" | "gate" | "session";

// Inner SVG per kind, mirroring the artifact's glyph() draws exactly, filled
// with the categorical entity-type token var(--et-<kind>) from index.css.
function shape(kind: EntityKind, fill: string): ReactNode {
  switch (kind) {
    case "code":    return <rect x="1.5" y="1.5" width="9" height="9" rx="1.5" fill={fill} />;
    case "memory":  return <path d="M6 0.8 L11.2 6 L6 11.2 L0.8 6 Z" fill={fill} />;
    case "task":    return <rect x="1" y="2.5" width="10" height="7" rx="2.4" fill={fill} />;
    case "test":    return <path d="M6 1.2 L11 10.8 L1 10.8 Z" fill={fill} />;
    case "gate":    return <path d="M6 .8 L10.8 3.4 L10.8 8.6 L6 11.2 L1.2 8.6 L1.2 3.4 Z" fill={fill} />;
    case "session": return <circle cx="6" cy="6" r="4.8" fill={fill} />;
  }
}

export const GlyphIcon = ({ kind, size = 12, className }: {
  kind: EntityKind;
  size?: number;
  className?: string;
}) => (
  <svg
    width={size} height={size} viewBox="0 0 12 12"
    role="img" aria-label={kind}
    className={cn("shrink-0", className)}
  >
    {shape(kind, `var(--et-${kind})`)}
  </svg>
);

export const EntityChip = ({ kind, label, to, title, className }: {
  kind: EntityKind;
  label: string;
  to?: string;
  title?: string;
  className?: string;
}) => {
  // .ent: glyph + name, 5px-radius bordered pill; hover shifts border + text
  // to the accent. Quiet surface/border tokens keep it from shouting inline.
  const cls = cn(
    "inline-flex items-center gap-1.5 whitespace-nowrap rounded-[5px] border py-[2px] pl-[5px] pr-2 text-xs",
    "border-[color:var(--border-default)] bg-[color:var(--surface-1)] text-[color:var(--text-primary)]",
    "hover:border-[color:var(--accent-teal-ring)] hover:text-[color:var(--accent-teal-fg)]",
    className,
  );
  const inner = (
    <>
      <GlyphIcon kind={kind} />
      <span>{label}</span>
    </>
  );
  return to ? (
    <Link to={to} title={title} className={cls}>{inner}</Link>
  ) : (
    <span title={title} className={cls}>{inner}</span>
  );
};
