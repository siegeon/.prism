import { useState, type ReactNode } from "react";
import { Empty } from "@/components/ui";
import Markdown from "@/components/Markdown";
import Mermaid from "./Mermaid";

/**
 * Renders a task plan as TABS in one place — Prototype (the clickable mock,
 * iframed), Diagram (Mermaid), Proposed change (markdown), and Metrics (the
 * per-phase SDLC breakdown) share the same slot, toggled by a tab bar.
 * Full-width (max-w-none) so the task page uses the whole screen like
 * /understand. Default tab is the Prototype when present.
 */
export default function PlanView({
  diagram,
  doc,
  prototypeSrc,
  metrics,
}: {
  diagram?: string;
  doc?: string;
  prototypeSrc?: string;
  // Per-phase metrics card (task bd1c2289) rendered as a peer tab alongside
  // Diagram / Proposed change, so effort lives in the same plan surface.
  metrics?: ReactNode;
}) {
  const hasDiagram = !!diagram?.trim();
  const hasDoc = !!doc?.trim();
  const hasProto = !!prototypeSrc;
  const hasMetrics = !!metrics;

  const tabs: { key: string; label: string }[] = [];
  if (hasProto) tabs.push({ key: "prototype", label: "Prototype" });
  if (hasDiagram) tabs.push({ key: "diagram", label: "Diagram" });
  if (hasDoc) tabs.push({ key: "doc", label: "Proposed change" });
  if (hasMetrics) tabs.push({ key: "metrics", label: "Metrics" });

  const [active, setActive] = useState(tabs[0]?.key ?? "doc");
  if (tabs.length === 0) return <Empty>No plan yet.</Empty>;
  const cur = tabs.some((t) => t.key === active) ? active : tabs[0].key;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-1 border-b border-[color:var(--border-default)]">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => setActive(t.key)}
            className={
              "px-3 py-2 text-[11px] uppercase tracking-wider -mb-px border-b-2 transition-colors " +
              (cur === t.key
                ? "border-[color:var(--accent-teal-fg)] text-[color:var(--accent-teal-fg)]"
                : "border-transparent text-[color:var(--text-muted)] hover:text-[color:var(--text-secondary)]")
            }
          >
            {t.label}
          </button>
        ))}
        {cur === "prototype" && (
          <a
            href={prototypeSrc}
            target="_blank"
            rel="noreferrer"
            className="ml-auto px-2 text-[11px] uppercase tracking-wider text-[color:var(--accent-teal-fg)] hover:opacity-80"
          >
            open full screen ↗
          </a>
        )}
      </div>

      {cur === "prototype" && hasProto && (
        <div className="rounded-md overflow-hidden border border-[color:var(--border-default)]">
          <iframe
            title="prototype"
            src={prototypeSrc}
            className="w-full block"
            style={{ height: "80vh", background: "var(--background-base)" }}
          />
        </div>
      )}
      {cur === "diagram" && hasDiagram && (
        <div className="rounded-lg p-4 bg-[color:var(--midground-base)]/5">
          <Mermaid chart={diagram!} />
        </div>
      )}
      {cur === "doc" && hasDoc && (
        // full-width (max-w-none): whole screen like /understand, not 840px.
        <Markdown text={doc!} className="space-y-4 max-w-none" />
      )}
      {cur === "metrics" && hasMetrics && (
        <div className="pt-1">{metrics}</div>
      )}
    </div>
  );
}
