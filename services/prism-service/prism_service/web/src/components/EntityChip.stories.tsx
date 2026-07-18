/**
 * Ladle stories for EntityChip (task d30c9a75) — "the unit of connectedness":
 * a typed glyph + name, used in TaskDetailPage's Trace rail (session/code/
 * memory/task/test/gate mentions). Kind encodes both shape (glyph) AND the
 * --et-<kind> categorical color from index.css — this story exists to prove
 * both survive outside the app shell.
 */
import type { Story, StoryDefault } from "@ladle/react";
import { MemoryRouter } from "react-router-dom";
import { EntityChip, GlyphIcon, type EntityKind } from "./EntityChip";

export default {
  title: "Primitives / EntityChip",
  // EntityChip renders a react-router <Link> when `to` is set.
  decorators: [(Component) => <MemoryRouter><Component /></MemoryRouter>],
} satisfies StoryDefault;

const KINDS: EntityKind[] = ["code", "memory", "task", "test", "gate", "session"];

/** Every entity kind — glyph shape + --et-* fill, exactly as the graph
 * legend and Trace rail render them. */
export const AllKinds: Story = () => (
  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
    {KINDS.map((kind) => (
      <EntityChip key={kind} kind={kind} label={kind} />
    ))}
  </div>
);

/** Glyphs alone (no chip chrome) — the legend/canvas reuse of the shape. */
export const Glyphs: Story = () => (
  <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
    {KINDS.map((kind) => (
      <GlyphIcon key={kind} kind={kind} size={20} />
    ))}
  </div>
);

/** Live-controls variant: drag `kind`/`label` to preview any chip. */
export const Playground: Story<{ kind: EntityKind; label: string }> = ({
  kind,
  label,
}) => <EntityChip kind={kind} label={label} />;
Playground.args = { kind: "code", label: "api/jira_sync.py" };
Playground.argTypes = {
  kind: {
    options: KINDS,
    control: { type: "select" },
  },
};

/** As it appears in TaskDetailPage's Trace rail: a session mention that
 * links out, next to a machine-adjudicated Lozenge fallback. */
export const TraceRailRow: Story = () => (
  <EntityChip kind="session" label="a1b2c3d4 · drive" to="/sessions/a1b2c3d4" />
);
