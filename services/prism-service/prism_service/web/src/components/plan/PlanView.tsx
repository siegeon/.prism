import { Empty, SectionLabel } from "@/components/ui";
import OnboardingView from "@/components/understand/OnboardingView";
import Mermaid from "./Mermaid";

/**
 * Renders a task plan as a readable document — a Mermaid sequence/UML
 * diagram on TOP and the proposed-change markdown BELOW. Reuses the
 * OnboardingView markdown parser for the prose so plans render with the
 * same PRISM typography as onboarding docs (no <pre>, no raw JSON).
 */
export default function PlanView({
  diagram,
  doc,
}: {
  diagram?: string;
  doc?: string;
}) {
  const hasDiagram = !!diagram?.trim();
  const hasDoc = !!doc?.trim();
  if (!hasDiagram && !hasDoc) return <Empty>No plan yet.</Empty>;

  return (
    <div className="space-y-6">
      {hasDiagram && (
        <div>
          <SectionLabel>Diagram</SectionLabel>
          <div className="mt-2 rounded-lg p-4 bg-[color:var(--midground-base)]/5">
            <Mermaid chart={diagram!} />
          </div>
        </div>
      )}
      {hasDoc && (
        <div>
          <SectionLabel>Proposed change</SectionLabel>
          <div className="mt-2">
            <OnboardingView text={doc!} />
          </div>
        </div>
      )}
    </div>
  );
}
