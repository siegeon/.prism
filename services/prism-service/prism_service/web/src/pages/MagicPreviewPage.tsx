/**
 * Preview of a customer's Magic-built app (task 10e00424) — the FACE the
 * interview → READY → build pipeline was missing. Loads the generated Puck
 * JSON + design tokens (/api/magic/ui/<project>) and renders the app
 * deterministically with <Render/>, one tab per entity, LIVE data flowing
 * from the deployed Magic tenant.
 *
 * The canvas is rendered inside Astryx's <Theme> (github.com/facebook/astryx,
 * MIT) so EntityTable/EntityForm get a professionally designed component
 * substrate. The customer's generated --app-* brand tokens are translated onto
 * Astryx's --color-* and --radius-* theme vars as inline overrides on the canvas —
 * so the customer's brand drives the Astryx theme, scoped to the preview only.
 */
import { useEffect, useMemo, useState } from "react";
import { useProject } from "@/lib/project";
import { api } from "@/lib/api";
import { Page, Empty, ErrorBanner } from "@/components/ui";
import { puckConfig, Render } from "@/components/magic/puckConfig";
import { Theme } from "@astryxdesign/core/theme";
import { neutralTheme } from "@astryxdesign/theme-neutral/built";
import "@astryxdesign/theme-neutral/theme.css";
import type { Data } from "@measured/puck";

type PuckData = { root: { props: Record<string, unknown> };
  content: unknown[]; zones: Record<string, unknown> };
type UiApp = { app: string; module: string; entities: string[];
  pages: Record<string, PuckData>; tokens: Record<string, string> };

// Layout-only canvas chrome. All card/table/input/button VISUALS are now owned
// by Astryx; these rules keep the page scaffolding and read their colors from
// the Astryx theme vars (which our mapped --app-* overrides drive).
const CANVAS_CSS = `
.mp-canvas { background: var(--color-background-body);
  color: var(--color-text-primary); border-radius: var(--radius-container);
  border: 1px solid var(--color-border); padding: 20px; }
.mp-canvas .app-page { display: flex; flex-direction: column; gap: 18px; }
.mp-canvas .app-page-title { font-size: 20px; font-weight: 700; margin: 0; }
.mp-canvas .app-card { display: flex; flex-direction: column; }
.mp-canvas .app-card-title { font-weight: 600; margin-bottom: 12px;
  color: var(--color-text-primary); text-transform: capitalize; }
.mp-canvas .app-form { display: grid; gap: 12px; align-items: end;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
.mp-canvas .app-form-actions { display: flex; align-items: center;
  gap: 12px; margin-top: 14px; }
.mp-canvas .app-empty { color: var(--color-text-secondary);
  font-style: italic; text-align: center; padding: 18px; }
.mp-canvas .app-msg { font-size: 13px; color: var(--color-text-secondary); }
.mp-canvas .app-error { color: var(--color-error, #f87171);
  font-size: 13px; margin-bottom: 8px; }
`;

// Generated --app-* brand tokens -> Astryx theme vars. Applied as inline CSS
// custom props on the canvas div; inline props out-specify the @layer theme
// defaults, so the customer's brand wins inside the preview only.
function mapTokens(tokens: Record<string, string>): React.CSSProperties {
  const out: Record<string, string> = { ...tokens };
  const put = (src: string, ...dst: string[]) => {
    const v = tokens[src];
    if (v) for (const d of dst) out[d] = v;
  };
  put("--app-brand", "--color-accent");
  if (tokens["--app-brand"]) out["--color-on-accent"] = "#fff";
  put("--app-bg", "--color-background-body");
  put("--app-surface", "--color-background-surface", "--color-background-card");
  put("--app-fg", "--color-text-primary");
  put("--app-muted", "--color-text-secondary");
  put("--app-border", "--color-border");
  put("--app-radius", "--radius-element", "--radius-container");
  return out as React.CSSProperties;
}

export default function MagicPreviewPage() {
  const [project] = useProject();
  const [ui, setUi] = useState<UiApp | null>(null);
  const [knowledge, setKnowledge] = useState<{ facts: { name: string; text: string }[];
    description?: string } | null>(null);
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
    // what PRISM has LEARNED about this business — visible, per UI-first
    api.get<{ facts: { name: string; text: string }[]; description?: string }>(
      `/api/magic/knowledge/${encodeURIComponent(project)}`)
      .then(setKnowledge).catch(() => setKnowledge(null));
  }, [project]);

  const canvasStyle = useMemo(() => mapTokens(ui?.tokens ?? {}), [ui]);

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
      <Theme theme={neutralTheme}>
        {knowledge && knowledge.facts.length > 0 && (
        /* progressive disclosure: one line closed, the full learning open */
        <details style={{ marginBottom: 14, border: "1px solid var(--border)",
          borderRadius: 10, padding: "10px 14px" }}>
          <summary style={{ cursor: "pointer", fontSize: 13, fontWeight: 600 }}>
            What PRISM knows about this business — {knowledge.facts.length} facts ✓
          </summary>
          {knowledge.description && (
            <div style={{ margin: "10px 0 4px", fontSize: 13, fontStyle: "italic",
              color: "var(--text-muted)" }}>
              &ldquo;{knowledge.description}&rdquo;
            </div>
          )}
          <ul style={{ margin: "8px 0 2px", paddingLeft: 18, fontSize: 13,
            lineHeight: 1.7, color: "var(--text-secondary)" }}>
            {knowledge.facts.map((f, i) => <li key={i}>{f.text}</li>)}
          </ul>
        </details>
      )}
      <div className="mp-canvas" style={canvasStyle}>
          {data
            ? <Render config={puckConfig} data={data as unknown as Data} />
            : <Empty>Entity has no page.</Empty>}
        </div>
      </Theme>
    </Page>
  );
}
