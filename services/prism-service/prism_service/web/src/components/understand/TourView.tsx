import { FileCode, ArrowRight } from "lucide-react";
import { Empty, SectionLabel } from "@/components/ui";

type Step = {
  ordinal?: number;
  title?: string;
  anchor_file?: string;
  anchor_symbol?: string;
  narration?: string;
  follow_ups?: string[];
};

type Tour = {
  schema?: string;
  status?: string;
  title?: string;
  summary?: string;
  steps?: Step[];
  continuation_hint?: string;
};

export default function TourView({ data }: { data: Tour }) {
  const steps = data.steps ?? [];
  if (steps.length === 0) {
    return <Empty>No steps in this tour yet.</Empty>;
  }

  return (
    <div className="space-y-4">
      {data.title && (
        <div>
          <SectionLabel>Tour</SectionLabel>
          <h2 className="font-serif text-xl tracking-tight">{data.title}</h2>
          {data.summary && (
            <p className="text-sm opacity-70 mt-1">{data.summary}</p>
          )}
        </div>
      )}
      <ol className="space-y-3">
        {steps.map((step, i) => (
          <StepCard key={i} step={step} fallbackOrdinal={i + 1} />
        ))}
      </ol>
      {data.continuation_hint && (
        <div className="text-[11px] opacity-60 italic">
          continuation: {data.continuation_hint}
        </div>
      )}
    </div>
  );
}


function StepCard({ step, fallbackOrdinal }: { step: Step; fallbackOrdinal: number }) {
  const ord = step.ordinal ?? fallbackOrdinal;
  return (
    <li className="rounded-md border border-[color:var(--midground-base)]/15 bg-[color:var(--background-base)]/40 p-4 flex gap-4">
      <div className="font-mono text-xs opacity-50 mt-1 min-w-[24px]">
        {String(ord).padStart(2, "0")}
      </div>
      <div className="flex-1 min-w-0">
        <div className="font-serif text-base tracking-tight">
          {step.title ?? "(untitled step)"}
        </div>
        {step.narration && (
          <p className="text-sm opacity-75 mt-1 leading-relaxed">
            {step.narration}
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2 mt-3 text-[11px]">
          {step.anchor_file && (
            <span className="inline-flex items-center gap-1 px-2 py-1 rounded font-mono bg-[color:var(--accent-teal-bg)] text-[color:var(--accent-teal-fg)] ring-1 ring-inset ring-[color:var(--accent-teal-ring)]">
              <FileCode className="w-3 h-3" />
              {step.anchor_file}
            </span>
          )}
          {step.anchor_symbol && (
            <span className="inline-flex items-center gap-1 px-2 py-1 rounded font-mono bg-[color:var(--accent-amber-bg)] text-[color:var(--accent-amber-fg)] ring-1 ring-inset ring-[color:var(--accent-amber-ring)]">
              <ArrowRight className="w-3 h-3" />
              {step.anchor_symbol}
            </span>
          )}
          {step.follow_ups?.slice(0, 6).map((ref) => (
            <span
              key={ref}
              className="px-2 py-1 rounded font-mono bg-[color:var(--accent-violet-bg)] text-[color:var(--accent-violet-fg)] ring-1 ring-inset ring-[color:var(--accent-violet-ring)]"
            >
              {ref}
            </span>
          ))}
        </div>
      </div>
    </li>
  );
}
