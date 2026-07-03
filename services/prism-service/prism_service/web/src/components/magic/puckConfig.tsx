/**
 * Puck render config for a customer's Magic-built app (task 10e00424).
 *
 * Ported per our port-and-reskin convention from Puck (github.com/puckeditor/
 * puck, MIT) — the render CONTRACT: a customer app is a JSON tree whose nodes
 * may only be the components registered here, so the output can never freestyle
 * off-brand. EntityTable / EntityForm are DATA-AWARE: they fetch live from the
 * deployed Magic tenant through PRISM's proxy (/api/magic/data/<mod>/<entity>),
 * so the preview shows REAL rows and a real create round-trips through the same
 * rule guards the interview captured.
 *
 * Rendered with Astryx / XDS (github.com/facebook/astryx, @astryxdesign/core,
 * MIT) — accessible, themeable React components. The customer's generated
 * --app-* brand tokens are mapped onto Astryx's --color-* and --radius-* theme vars
 * in MagicPreviewPage, so every generated app inherits a professionally
 * designed component substrate while still reading as the customer's brand.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Render, type Config } from "@measured/puck";
import "@measured/puck/puck.css";
import { Table, type TableColumn } from "@astryxdesign/core/Table";
import { Card } from "@astryxdesign/core/Card";
import { Button } from "@astryxdesign/core/Button";
import { TextInput } from "@astryxdesign/core/TextInput";
import { NumberInput } from "@astryxdesign/core/NumberInput";
import { Selector } from "@astryxdesign/core/Selector";
import "@astryxdesign/core/astryx.css";
import { api } from "@/lib/api";

type Field = { name: string; type: string };
type Rule =
  | { type: "enum"; field: string; values: string[] }
  | { type: "fk"; field: string; ref: string }
  | { type: "min"; field: string; value: number };
type Row = Record<string, unknown>;

const DATA_CHANGED = "magic-data-changed";
const isNumeric = (t: string) => /INT|REAL|NUM/.test(t);
const dataPath = (m: string, e: string) =>
  `/api/magic/data/${encodeURIComponent(m)}/${encodeURIComponent(e)}`;

/** Live rows from the deployed entity endpoint; refetches on create. */
function EntityTable({ module, entity, fields }:
  { module: string; entity: string; fields: Field[] }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(true);
  const load = useCallback(() => {
    setLoading(true);
    api.get<{ rows: unknown }>(dataPath(module, entity))
      .then((d) => setRows(Array.isArray(d.rows) ? d.rows as Row[] : []))
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
  const columns: TableColumn<Row>[] = cols.map((f) => ({
    key: f.name,
    header: f.name,
    renderCell: (item: Row) => String(item[f.name] ?? ""),
  }));
  return (
    <Card className="app-card">
      <div className="app-card-title">{entity}</div>
      {err && <div className="app-error">{err}</div>}
      <Table data={rows} columns={columns} hasHover dividers="rows" />
      {!rows.length && !loading && (
        <div className="app-empty">No rows yet — add one above.</div>
      )}
    </Card>
  );
}

/** Add-a-row form. enum -> Selector, numeric+min -> NumberInput (min guard),
 * else TextInput; POST round-trips the SAME rule guards the backend enforces,
 * then signals the table to refetch. */
function EntityForm({ module, entity, fields, rules }:
  { module: string; entity: string; fields: Field[]; rules: Rule[] }) {
  const [val, setVal] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (f: string, v: string) => setVal((s) => ({ ...s, [f]: v }));
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
    <Card className="app-card">
      <div className="app-card-title">Add {entity.replace(/s$/, "")}</div>
      <div className="app-form">
        {fields.map((f) => {
          const opts = enumOf(f.name);
          const min = minOf(f.name);
          if (opts) {
            return (
              <Selector key={f.name} label={f.name} placeholder="choose…"
                value={val[f.name] ?? ""}
                options={opts.map((o) => ({ value: o, label: o }))}
                onChange={(v) => set(f.name, v)} />
            );
          }
          if (isNumeric(f.type) || min !== undefined) {
            const raw = val[f.name];
            return (
              <NumberInput key={f.name} label={f.name} min={min}
                value={raw === undefined || raw === "" ? null : Number(raw)}
                onChange={(v) => set(f.name, v === null ? "" : String(v))} />
            );
          }
          return (
            <TextInput key={f.name} label={f.name}
              value={val[f.name] ?? ""}
              onChange={(v) => set(f.name, v)} />
          );
        })}
      </div>
      <div className="app-form-actions">
        <Button label={busy ? "Saving…" : "Add"} variant="primary"
          type="button" isDisabled={busy} onClick={submit} />
        {msg && <span className="app-msg">{msg}</span>}
      </div>
    </Card>
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
