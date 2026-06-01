import { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";
import { Empty } from "@/components/ui";

/**
 * Renders a Mermaid diagram (sequence / UML / flow) from raw source.
 *
 * Mermaid is initialised once with a dark-leaning theme tuned to the
 * PRISM design-system tokens, so the diagram reads against the dark
 * surface instead of the library's default light card.
 */
let _initialised = false;
function ensureInit() {
  if (_initialised) return;
  mermaid.initialize({
    startOnLoad: false,
    theme: "dark",
    securityLevel: "strict",
    themeVariables: {
      fontFamily: "var(--font-sans, ui-sans-serif)",
    },
  });
  _initialised = true;
}

export default function Mermaid({ chart }: { chart: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const source = chart.trim();
    if (!source) return;
    ensureInit();
    const id = `mmd-${Math.random().toString(36).slice(2)}`;
    mermaid
      .render(id, source)
      .then(({ svg }) => {
        if (!cancelled && ref.current) {
          ref.current.innerHTML = svg;
          setError(null);
        }
      })
      .catch((e) => {
        if (!cancelled) setError(String(e?.message ?? e));
      });
    return () => {
      cancelled = true;
    };
  }, [chart]);

  if (!chart.trim()) return <Empty>No diagram.</Empty>;
  if (error) {
    return (
      <div className="text-[12px] text-rose-300/90 leading-relaxed">
        Diagram failed to render: {error}
      </div>
    );
  }
  return <div ref={ref} className="overflow-x-auto [&_svg]:max-w-full" />;
}
