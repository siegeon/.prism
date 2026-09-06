import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";
import { Empty } from "@/components/ui";

// Archify Maps — a rendered architecture/workflow map embedded as a sandboxed
// iframe, backed by GET/POST /api/archify/maps/<kind> (see the Archify x
// PRISM integration contract). Three project-level kinds (code / concepts /
// language) share one tab strip; the task kind renders standalone with no
// tabs, mounted on the task detail page.

export type ArchifyKind = "code" | "concepts" | "language" | "task";

const TABS: { kind: ArchifyKind; label: string }[] = [
  { kind: "code", label: "Code" },
  { kind: "concepts", label: "Concepts" },
  { kind: "language", label: "Language" },
];

type ArchifyMeta = {
  kind: string;
  diagram_type: string;
  task_id: string | null;
  title: string;
  built_at: string;
  ok: boolean;
  components: number;
  connections: number;
  error: string;
  html_url: string;
};

type ArchifyReceipt = {
  diagnostics?: { message: string }[];
};

// Archify id slug rule (vendor/archify/_layout.slug): lowercase, anything
// outside [a-zA-Z0-9_-] becomes "-", repeats collapse, and a leading digit is
// prefixed with "n-" so the id stays a valid archify identifier.
export function slugForFocus(text: string): string {
  const lowered = text.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/-+/g, "-");
  return /^[0-9]/.test(lowered) ? `n-${lowered}` : lowered;
}

export default function ArchifyMaps({
  project,
  kind: fixedKind,
  taskId,
  focusId,
}: {
  project: string;
  kind?: ArchifyKind;
  taskId?: string;
  focusId?: string;
}) {
  const tabbed = !fixedKind;
  const [activeTab, setActiveTab] = useState<ArchifyKind>("code");
  const kind = fixedKind ?? activeTab;

  const [meta, setMeta] = useState<ArchifyMeta | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [building, setBuilding] = useState(false);
  const [firstDiagnostic, setFirstDiagnostic] = useState<string>("");

  const taskQuery = kind === "task" && taskId ? `&task_id=${encodeURIComponent(taskId)}` : "";

  const load = () => {
    if (kind === "task" && !taskId) { setMeta(null); setLoaded(true); return; }
    setLoaded(false);
    api
      .get<ArchifyMeta>(`/api/archify/maps/${kind}?project=${encodeURIComponent(project)}${taskQuery}`)
      .then((m) => { setMeta(m); setLoaded(true); })
      .catch(() => { setMeta(null); setLoaded(true); });
  };

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [project, kind, taskId]);

  useEffect(() => {
    if (!meta || meta.ok) { setFirstDiagnostic(""); return; }
    api
      .get<ArchifyReceipt>(`/api/archify/maps/${kind}/receipt?project=${encodeURIComponent(project)}${taskQuery}`)
      .then((r) => setFirstDiagnostic(r.diagnostics?.[0]?.message ?? meta.error ?? ""))
      .catch(() => setFirstDiagnostic(meta.error ?? ""));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meta]);

  const build = () => {
    setBuilding(true);
    api
      .post<ArchifyMeta>(`/api/archify/maps/${kind}/build?project=${encodeURIComponent(project)}${taskQuery}`, {})
      .then((m) => setMeta(m))
      .catch(() => {})
      .finally(() => setBuilding(false));
  };

  const iframeSrc = useMemo(() => {
    if (!meta?.html_url) return null;
    const base = `${meta.html_url}&theme=dark`;
    const slug = kind === "concepts" && focusId ? slugForFocus(focusId) : null;
    return slug ? `${base}#focus=${slug}` : base;
  }, [meta, kind, focusId]);

  const height = fixedKind === "task" ? "50vh" : "70vh";

  return (
    <div className="space-y-3">
      {tabbed && (
        <div className="flex gap-0.5 border-b border-[color:var(--border-default)]" role="tablist">
          {TABS.map((t) => (
            <button
              key={t.kind}
              role="tab"
              aria-selected={activeTab === t.kind}
              onClick={() => setActiveTab(t.kind)}
              className={`-mb-px border-b-2 px-3.5 py-2 text-[13px] font-medium ${
                activeTab === t.kind
                  ? "border-[color:var(--accent-teal-fg)] text-[color:var(--accent-teal-fg)]"
                  : "border-transparent text-[color:var(--text-muted)] hover:text-[color:var(--text-primary)]"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>
      )}

      {kind === "task" && !taskId ? null : !loaded ? (
        <Empty>Loading map…</Empty>
      ) : !meta ? (
        <div className="space-y-2">
          <Empty>No {kind} map built yet.</Empty>
          <button
            type="button"
            disabled={building}
            onClick={build}
            className="text-2xs font-semibold px-2.5 py-1 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
            style={{ color: "var(--accent-teal-fg)" }}
          >
            {building ? "Building…" : "Build map"}
          </button>
        </div>
      ) : (
        <div className="space-y-2">
          <div className="flex items-center justify-between gap-3 text-2xs text-[color:var(--text-muted)]">
            <span>
              built {meta.built_at ? String(meta.built_at).slice(0, 19).replace("T", " ") : "—"} ·{" "}
              {meta.components} components · {meta.connections} connections
            </span>
            <button
              type="button"
              disabled={building}
              onClick={build}
              className="text-2xs font-semibold px-2.5 py-1 rounded border border-[color:var(--border-default)] hover:bg-[color:var(--surface-2)] disabled:opacity-40"
              style={{ color: "var(--accent-teal-fg)" }}
            >
              {building ? "Building…" : "Rebuild"}
            </button>
          </div>
          {!meta.ok && (
            <div
              className="rounded-md px-3 py-2 text-[12px] leading-relaxed"
              style={{
                background: "var(--accent-amber-bg, var(--accent-sage-bg))",
                boxShadow: "inset 0 0 0 1px var(--accent-amber-ring, var(--accent-sage-ring))",
              }}
            >
              {firstDiagnostic || meta.error || "the last build did not validate"}
            </div>
          )}
          {iframeSrc && (
            <div className="rounded-md overflow-hidden border border-[color:var(--border-default)]">
              <iframe
                key={kind}
                title={`${kind} map`}
                src={iframeSrc}
                sandbox="allow-scripts allow-same-origin"
                className="w-full block"
                style={{ height, background: "var(--background-base)" }}
              />
            </div>
          )}
        </div>
      )}
    </div>
  );
}
