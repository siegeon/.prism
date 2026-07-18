/**
 * Ladle stories for Lozenge (task d30c9a75) — the status chip used across
 * TaskDetailPage (status/priority/gate/role) and StepRail (per-step role +
 * gate). Every color is the real --accent-{tone}-* token triple from
 * index.css via the Ladle Provider (.ladle/components.tsx), so this must
 * look identical to the chips on those pages, not a bare unstyled span.
 */
import type { Story, StoryDefault } from "@ladle/react";
import { Lozenge, type LozengeTone } from "./Lozenge";

export default {
  title: "Primitives / Lozenge",
} satisfies StoryDefault;

const TONES: LozengeTone[] = ["neutral", "info", "ok", "warn", "danger", "new"];

/** Every tone side by side — the same six-tone vocabulary TaskDetailPage
 * documents for status/gate/role chips. */
export const AllTones: Story = () => (
  <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
    {TONES.map((tone) => (
      <Lozenge key={tone} tone={tone}>{tone}</Lozenge>
    ))}
  </div>
);

/** Live-controls variant: drag `tone`/`children` in the Ladle Controls
 * panel to see any tone + label combination. */
export const Playground: Story<{ tone: LozengeTone; label: string }> = ({
  tone,
  label,
}) => <Lozenge tone={tone}>{label}</Lozenge>;
Playground.args = { tone: "ok", label: "passed" };
Playground.argTypes = {
  tone: {
    options: TONES,
    control: { type: "select" },
  },
};

/** As it actually appears in TaskDetailPage's header row: status + priority
 * + assigned-agent chips together. */
export const TaskHeaderRow: Story = () => (
  <div style={{ display: "flex", gap: 8 }}>
    <Lozenge tone="info">in progress</Lozenge>
    <Lozenge tone="warn">priority 2</Lozenge>
    <Lozenge tone="new">claude-opus-4-8</Lozenge>
    <Lozenge tone="neutral">#ui-redesign</Lozenge>
  </div>
);
