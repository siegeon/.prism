/**
 * Preview of a customer's Magic-built app (task 10e00424) — the FACE the
 * interview → READY → build pipeline was missing. Loads the generated Puck
 * JSON + design tokens (/api/magic/ui/<project>) and renders the app
 * deterministically with <Render/>, one tab per entity, LIVE data flowing
 * from the deployed Magic tenant. The canvas is themed by the customer's
 * generated --app-* tokens (their brand), NOT Hermes chrome.
 */
import { useEffect, useMemo, useState } from "react";
import { useProject } from "@/lib/project";
import { api } from "@/lib/api";
import { Page, Empty, ErrorBanner } from "@/components/ui";
import { puckConfig, Render } from "@/components/magic/puckConfig";
import type { Data } from "@measured/puck";

type PuckData = { root: { props: Record<string, unknown> };
  content: unknown[]; zones: Record<string, unknown> };
type UiApp = { app: string; module: string; entities: string[];
  pages: Record<string, PuckData>; tokens: Record<string, string> };

const CANVAS_CSS = `
.mp-canvas { background: var(--app-bg); color: var(--app-fg);
  font-family: var(--app-font); border-radius: var(--app-radius);
  border: 1px solid var(--app-border); padding: 20px; }
.mp-canvas .app-page { display: flex; flex-direction: column; gap: 18px; }
.mp-canvas .app-page-title { font-size: 20px; font-weight: 700; margin: 0; }
.mp-canvas .app-card { background: var(--app-surface);
  border: 1px solid var(--app-border); border-radius: var(--app-radius);
  padding: 16px; }
.mp-canvas .app-card-title { font-weight: 600; margin-bottom: 12px;
  color: var(--app-fg); text-transform: capitalize; }
.mp-canvas .app-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.mp-canvas .app-table th { text-align: left; padding: 8px 10px;
  color: var(--app-muted); border-bottom: 1px solid var(--app-border);
  font-weight: 600; }
.mp-canvas .app-table td { padding: 8px 10px;
  border-bottom: 1px solid var(--app-border); }
.mp-canvas .app-empty { color: var(--app-muted); font-style: italic;
  text-align: center; padding: 18px; }
.mp-canvas .app-form { display: grid; gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); }
.mp-canvas .app-field { display: flex; flex-direction: column; gap: 4px;
  font-size: 13px; color: var(--app-muted); }
.mp-canvas .app-field input, .mp-canvas .app-field select {
  background: var(--app-bg); color: var(--app-fg);
  border: 1px solid var(--app-border); border-radius: 8px;
  padding: 8px 10px; font-size: 14px; }
.mp-canvas .app-form-actions { display: flex; align-items: center;
  gap: 12px; margin-top: 14px; }
.mp-canvas .app-btn { background: var(--app-brand); color: #fff; border: 0;
  border-radius: 8px; padding: 9px 18px; font-weight: 600; cursor: pointer; }
.mp-canvas .app-btn:disabled { opacity: .6; cursor: default; }
.mp-canvas .app-msg { font-size: 13px; color: var(--app-accent); }
.mp-canvas .app-error { color: #f87171; font-size: 13px; margin-bottom: 8px; }
`;

export default function MagicPreviewPage() {
  const [project] = useProject();
  const [ui, setUi] = useState<UiApp | null>(null);
  const [tab, setTab] = useState("");
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!project) return;
    setLoading(true); setErr("");
    api.get<UiApp>(`/api/magic/ui/${encodeURIComponent(project)}`)
      .then((d) => { setUi(d); setTab(d.entities[0] ?? ""); })
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [project]);

  const tokenStyle = useMemo(
    () => (ui?.tokens ?? {}) as React.CSSProperties, [ui]);

  if (loading) return <Page><Empty>Loading preview…</Empty></Page>;
  if (err) return (
    <Page>
      <ErrorBanner>{err}</ErrorBanner>
      <Empty>No built app for <b>{project}</b> yet. Run a customer
        interview to READY and the app appears here.</Empty>
    </Page>
  );
  if (!ui) return <Page><Empty>Nothing to preview.</Empty></Page>;

  const data = ui.pages[tab];
  return (
    <Page>
      <style>{CANVAS_CSS}</style>
      <div style={{ display: "flex", alignItems: "baseline", gap: 12,
        marginBottom: 14 }}>
        <h1 style={{ fontSize: 22, fontWeight: 700 }}>{ui.app}</h1>
        <span style={{ color: "var(--text-label)", fontSize: 13 }}>
          live preview · module <code>{ui.module}</code>
        </span>
      </div>
      <div style={{ display: "flex", gap: 8, marginBottom: 16, flexWrap: "wrap" }}>
        {ui.entities.map((e) => (
          <button key={e} onClick={() => setTab(e)}
            style={{
              padding: "6px 14px", borderRadius: 999, fontSize: 13,
              fontWeight: 600, cursor: "pointer",
              border: "1px solid var(--border)",
              background: e === tab ? "var(--app-brand, #4f46e5)" : "transparent",
              color: e === tab ? "#fff" : "var(--text-label)",
            }}>{e}</button>
        ))}
      </div>
      <div className="mp-canvas" style={tokenStyle}>
        {data
          ? <Render config={puckConfig} data={data as unknown as Data} />
          : <Empty>Entity has no page.</Empty>}
      </div>
    </Page>
  );
}
