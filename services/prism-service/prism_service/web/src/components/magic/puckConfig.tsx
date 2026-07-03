/**
 * Puck render config for a customer's Magic-built app (task 10e00424).
 *
 * Ported per our port-and-reskin convention from Puck (github.com/puckeditor/
 * puck, MIT) — the render CONTRACT: a customer app is a JSON tree whose nodes
 * may only be the components registered here, so the output can never freestyle
 * off-brand. EntityTable / EntityForm are DATA-AWARE: they fetch live from the
 * deployed Magic tenant through PRISM's proxy (/api/magic/data/<mod>/<entity>),
 * so the preview shows REAL rows and a real create round-trips through the same
 * rule guards the interview captured. Themed entirely by the generated
 * --app-* CSS vars, never Hermes chrome — this is the customer's brand.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Render, type Config } from "@measured/puck";
import "@measured/puck/puck.css";
import { api } from "@/lib/api";

type Field = { name: string; type: string };
type Rule =
  | { type: "enum"; field: string; values: string[] }
  | { type: "fk"; field: string; ref: string }
  | { type: "min"; field: string; value: number };

const DATA_CHANGED = "magic-data-changed";
const dataPath = (m: string, e: string) =>
  `/api/magic/data/${encodeURIComponent(m)}/${encodeURIComponent(e)}`;

/** Live rows from the deployed entity endpoint; refetches on create. */
function EntityTable({ module, entity, fields }:
  { module: string; entity: string; fields: Field[] }) {
  const [rows, setRows] = useState<Record<string, unknown>[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    api.get<{ rows: unknown }>(dataPath(module, entity))
      .then((d) => setRows(Array.isArray(d.rows) ? d.rows as Record<string, unknown>[] : []))
      .catch((e) => setErr(String(e)))
      .finally(() => setLoading(false));
  }, [module, entity]);
  useEffect(() => {
    load();
    const h = (ev: Event) => {
      const d = (ev as CustomEvent).detail;
      if (d?.module === module && d?.entity === entity) load();
    };
    window.addEventListener(DATA_CHANGED, h);
    return () => window.removeEventListener(DATA_CHANGED, h);
  }, [load, module, entity]);
  const cols = fields.length ? fields : [{ name: "id", type: "INTEGER" }];
  return (
    <div className="app-card">
      <div className="app-card-title">{entity}</div>
      {err && <div className="app-error">{err}</div>}
      <table className="app-table">
        <thead><tr>{cols.map((f) => <th key={f.name}>{f.name}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>{cols.map((f) => <td key={f.name}>{String(r[f.name] ?? "")}</td>)}</tr>
          ))}
          {!rows.length && !loading && (
            <tr><td colSpan={cols.length} className="app-empty">No rows yet — add one above.</td></tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

/** Add-a-row form. enum -> <select>, min -> min attr; POST round-trips the
 * SAME rule guards the backend enforces, then signals the table to refetch. */
function EntityForm({ module, entity, fields, rules }:
  { module: string; entity: string; fields: Field[]; rules: Rule[] }) {
  const [val, setVal] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const enumOf = (f: string) =>
    (rules.find((r) => r.type === "enum" && r.field === f) as
      { values: string[] } | undefined)?.values;
  const minOf = (f: string) =>
    (rules.find((r) => r.type === "min" && r.field === f) as
      { value: number } | undefined)?.value;
  const submit = async () => {
    setBusy(true); setMsg("");
    try {
      await api.post(dataPath(module, entity), { record: val });
      setMsg("Added ✓"); setVal({});
      window.dispatchEvent(new CustomEvent(DATA_CHANGED, { detail: { module, entity } }));
    } catch (e) {
      setMsg(String(e).replace(/^Error:\s*/, ""));
    } finally { setBusy(false); }
  };
  return (
    <div className="app-card">
      <div className="app-card-title">Add {entity.replace(/s$/, "")}</div>
      <div className="app-form">
        {fields.map((f) => {
          const opts = enumOf(f.name);
          return (
            <label key={f.name} className="app-field">
              <span>{f.name}</span>
              {opts ? (
                <select value={val[f.name] ?? ""}
                  onChange={(e) => setVal({ ...val, [f.name]: e.target.value })}>
                  <option value="" disabled>choose…</option>
                  {opts.map((o) => <option key={o} value={o}>{o}</option>)}
                </select>
              ) : (
                <input
                  type={/INT|REAL|NUM/.test(f.type) ? "number" : "text"}
                  min={minOf(f.name)}
                  value={val[f.name] ?? ""}
                  onChange={(e) => setVal({ ...val, [f.name]: e.target.value })} />
              )}
            </label>
          );
        })}
      </div>
      <div className="app-form-actions">
        <button className="app-btn" disabled={busy} onClick={submit}>
          {busy ? "Saving…" : "Add"}
        </button>
        {msg && <span className="app-msg">{msg}</span>}
      </div>
    </div>
  );
}

export const puckConfig: Config = {
  root: {
    fields: { title: { type: "text" } },
    render: ({ title, children }: { title?: string; children?: ReactNode }) => (
      <div className="app-page">
        {title && <h2 className="app-page-title">{title}</h2>}
        {children}
      </div>
    ),
  },
  components: {
    EntityTable: {
      fields: { entity: { type: "text" } },
      render: ({ module, entity, fields }) =>
        <EntityTable module={module as string} entity={entity as string}
          fields={(fields as Field[]) ?? []} />,
    },
    EntityForm: {
      fields: { entity: { type: "text" } },
      render: ({ module, entity, fields, rules }) =>
        <EntityForm module={module as string} entity={entity as string}
          fields={(fields as Field[]) ?? []} rules={(rules as Rule[]) ?? []} />,
    },
  },
};

export { Render };
