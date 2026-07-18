/**
 * Ladle stories for the shared v5 primitives in ui.tsx (task d30c9a75) —
 * Card, SectionLabel, and Empty are the load-bearing wrappers TaskDetailPage,
 * TasksPage, and PlanView all import directly. Rendered through the real
 * index.css token layer (see .ladle/components.tsx), so surface elevation
 * (surface-0 -> surface-1 -> surface-2) and text hierarchy read exactly as
 * they do on those pages.
 */
import type { Story, StoryDefault } from "@ladle/react";
import { Card, SectionLabel, Empty, Page } from "./ui";

export default {
  title: "Primitives / Card & Page",
} satisfies StoryDefault;

/** Bare Card on the page surface — surface-0 -> surface-1. */
export const Default: Story = () => (
  <Card>
    <SectionLabel>Details</SectionLabel>
    <div style={{ color: "var(--text-primary)" }}>Card content sits on --surface-1.</div>
  </Card>
);

/** `nested` — a Card inside a Card, lifting to surface-2 (RailCard-style
 * grouping in TaskDetailPage's Trace rail). */
export const Nested: Story = () => (
  <Card>
    <SectionLabel>Outer — surface-1</SectionLabel>
    <Card nested>
      <SectionLabel>Nested — surface-2</SectionLabel>
      <div style={{ color: "var(--text-secondary)" }}>Lifted a step above its parent.</div>
    </Card>
  </Card>
);

/** `raised` — the dominant card on a page (strong border, surface-2). */
export const Raised: Story = () => (
  <Card raised>
    <SectionLabel>Raised</SectionLabel>
    <div style={{ color: "var(--text-primary)" }}>border-strong + surface-2.</div>
  </Card>
);

/** Empty-state — TaskDetailPage's loading/blocked/no-data fallback,
 * always wrapped in a Card. */
export const EmptyState: Story = () => (
  <Card>
    <Empty>No trace yet — this task has no recorded agent runs.</Empty>
  </Card>
);

/** Live-controls variant for Card's boolean props. */
export const Playground: Story<{ nested: boolean; raised: boolean; text: string }> = ({
  nested,
  raised,
  text,
}) => (
  <Card nested={nested} raised={raised}>
    <SectionLabel>Playground</SectionLabel>
    <div style={{ color: "var(--text-primary)" }}>{text}</div>
  </Card>
);
Playground.args = { nested: false, raised: false, text: "Toggle nested/raised in Controls." };

/** Page — the app's outer padding/spacing wrapper, shown with a Card
 * inside it so the p-8 + space-y-6 rhythm is visible. */
export const InPage: Story = () => (
  <Page>
    <Card>
      <SectionLabel>Section</SectionLabel>
      <div style={{ color: "var(--text-primary)" }}>Page applies p-8 + space-y-6.</div>
    </Card>
  </Page>
);
