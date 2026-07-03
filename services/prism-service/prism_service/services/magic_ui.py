"""Deterministic UI renderer — the customer's Magic backend gets a face.

The interview spec {entities, fields(typed), rules(fk/min/enum/capacity)} is a
complete CRUD-UI specification. render_ui turns it into Puck JSON (github.com/
puckeditor/puck, MIT) — a render tree whose nodes may ONLY be props of the
components the SPA registers (EntityTable / EntityForm). No model runs at
generation time: same keyless/deterministic philosophy as the Hyperlambda
renderer, so the built UI always renders and never freestyles off-brand.

The SAME spec that generates the backend generates the UI, so they never
drift: an enum rule becomes a Hyperlambda validator AND a <select>; an fk rule
becomes a guard AND a lookup field. The registered components fetch live from
the deployed tenant endpoints magic/modules/<mod>/<entity> (proxied through
PRISM), so the preview shows REAL data flowing from Magic.
"""

from __future__ import annotations

from prism_service.services import magic_app_builder as ab


def _title(name: str) -> str:
    """appointments -> Appointments; work_orders -> Work Orders."""
    return " ".join(w.capitalize() for w in str(name).replace("_", " ").split())


def default_tokens(spec: dict) -> dict:
    """Neutral on-brand design tokens (CSS custom properties). design_intel's
    per-industry BM25 pass overrides these later; this keeps the preview
    themed and legible with zero external dependency."""
    return {
        "--app-brand": "#4f46e5", "--app-accent": "#22d3ee",
        "--app-bg": "#0b0b0f", "--app-surface": "#14141b",
        "--app-fg": "#e5e7eb", "--app-muted": "#8b8f9a",
        "--app-border": "#262631", "--app-radius": "12px",
        "--app-font": "Inter, ui-sans-serif, system-ui, sans-serif",
    }


def _fields_payload(ent: dict) -> list[dict]:
    """Typed field list the components render from (name + coarse type)."""
    out = []
    for f in ent.get("fields", []) or []:
        if not f.get("name"):
            continue
        out.append({"name": f["name"], "type": (f.get("type") or "TEXT").upper()})
    return out


def _rules_payload(ent: dict) -> list[dict]:
    """Rules the FORM renders as input affordances: enum -> <select>,
    fk -> lookup, min -> min attribute. Same rules the backend enforces."""
    out = []
    for r in ent.get("rules", []) or []:
        t = r.get("type")
        if t == "enum" and r.get("field") and r.get("values"):
            out.append({"type": "enum", "field": r["field"], "values": list(r["values"])})
        elif t == "fk" and r.get("field") and r.get("ref"):
            out.append({"type": "fk", "field": r["field"], "ref": r["ref"]})
        elif t == "min" and r.get("field"):
            out.append({"type": "min", "field": r["field"], "value": r.get("value", 0)})
    return out


def _entity_page(mod: str, ent: dict) -> dict:
    """One entity -> a Puck page: an add FORM over a live TABLE. Puck's data
    contract is {root, content, zones}; each node's `type` must match a
    component the SPA registered, so output quality == our component set."""
    name = ent["name"]
    fields = _fields_payload(ent)
    rules = _rules_payload(ent)
    return {
        "root": {"props": {"title": _title(name)}},
        "content": [
            {"type": "EntityForm", "props": {
                "id": f"form-{name}", "module": mod, "entity": name,
                "fields": fields, "rules": rules}},
            {"type": "EntityTable", "props": {
                "id": f"table-{name}", "module": mod, "entity": name,
                "fields": fields}},
        ],
        "zones": {},
    }


def render_ui(spec: dict, tokens: dict | None = None) -> dict:
    """Spec -> the whole app UI: one Puck page per entity + design tokens.
    Pure + deterministic (no network, no model) so it is unit-testable and
    always renders. `tokens` lets design_intel inject a per-industry palette;
    omitted -> neutral defaults."""
    spec = ab.normalize_spec(spec)
    mod = spec["module"]
    tokens = tokens or default_tokens(spec)
    pages = {ent["name"]: _entity_page(mod, ent) for ent in spec["entities"]}
    return {
        "module": mod,
        "app": _title(spec.get("db") or mod),
        "entities": [e["name"] for e in spec["entities"]],
        "pages": pages,
        "tokens": tokens,
    }


_FRONTEND_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__APP__</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=__FONT__:wght@400;600;700;800&display=swap" rel="stylesheet">
<style>
:root { __TOKENS__ }
* { box-sizing: border-box; margin: 0; }
html { -webkit-font-smoothing: antialiased; }
body { background: var(--app-bg); color: var(--app-fg);
  font-family: var(--app-font); min-height: 100vh;
  padding: clamp(16px, 4vw, 48px); line-height: 1.5; }
.shell { max-width: 980px; margin: 0 auto; }
header { margin-bottom: 28px; }
h1 { font-size: clamp(26px, 4vw, 38px); font-weight: 800;
  letter-spacing: -0.02em; }
h1::after { content: ""; display: block; width: 56px; height: 4px;
  margin-top: 10px; border-radius: 2px;
  background: linear-gradient(90deg, var(--app-brand), var(--app-accent)); }
.sub { color: var(--app-muted); font-size: 13px; margin-top: 10px;
  text-transform: uppercase; letter-spacing: 0.14em; }
.tabs { display: flex; gap: 8px; margin-bottom: 22px; flex-wrap: wrap; }
.tab { padding: 8px 18px; border-radius: 999px;
  border: 1px solid var(--app-border); background: var(--app-surface);
  color: var(--app-muted); font-weight: 600; cursor: pointer;
  font-size: 14px; font-family: inherit;
  transition: transform .12s ease, box-shadow .12s ease, color .12s ease; }
.tab:hover { transform: translateY(-1px); color: var(--app-fg);
  box-shadow: 0 4px 14px rgba(0,0,0,.12); }
.tab.on { background: var(--app-brand); color: #fff;
  border-color: var(--app-brand);
  box-shadow: 0 6px 18px color-mix(in srgb, var(--app-brand) 45%, transparent); }
.card { background: var(--app-surface); border: 1px solid var(--app-border);
  border-radius: var(--app-radius); padding: 22px 24px; margin-bottom: 18px;
  box-shadow: 0 1px 2px rgba(0,0,0,.06), 0 8px 24px rgba(0,0,0,.06); }
.card h2 { font-size: 15px; margin-bottom: 16px; text-transform: capitalize;
  letter-spacing: 0.01em; display: flex; align-items: center; gap: 8px; }
.card h2::before { content: ""; width: 8px; height: 8px; border-radius: 3px;
  background: var(--app-accent); display: inline-block; }
form { display: grid; gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); }
label { display: flex; flex-direction: column; gap: 6px; font-size: 12px;
  font-weight: 600; color: var(--app-muted); text-transform: capitalize;
  letter-spacing: 0.03em; }
input, select { background: var(--app-bg); color: var(--app-fg);
  border: 1px solid var(--app-border); border-radius: 10px;
  padding: 10px 12px; font-size: 14px; font-family: inherit;
  transition: border-color .12s ease, box-shadow .12s ease; }
input:focus, select:focus { outline: none; border-color: var(--app-brand);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--app-brand) 25%, transparent); }
.actions { grid-column: 1 / -1; display: flex; align-items: center;
  gap: 14px; margin-top: 2px; }
button.add { background: var(--app-brand); color: #fff; border: 0;
  border-radius: 10px; padding: 11px 26px; font-weight: 700;
  cursor: pointer; font-size: 14px; font-family: inherit;
  box-shadow: 0 6px 18px color-mix(in srgb, var(--app-brand) 40%, transparent);
  transition: transform .12s ease, filter .12s ease; }
button.add:hover { transform: translateY(-1px); filter: brightness(1.08); }
button.add:active { transform: translateY(0); }
.msg { font-size: 13px; font-weight: 600; color: var(--app-accent); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th { text-align: left; padding: 10px 12px; color: var(--app-muted);
  border-bottom: 2px solid var(--app-border); text-transform: capitalize;
  font-size: 12px; letter-spacing: 0.06em; }
td { padding: 11px 12px; border-bottom: 1px solid var(--app-border); }
tbody tr { transition: background .12s ease; }
tbody tr:hover { background: color-mix(in srgb, var(--app-brand) 7%, transparent); }
.pill { display: inline-block; padding: 3px 10px; border-radius: 999px;
  font-size: 12px; font-weight: 600;
  background: color-mix(in srgb, var(--app-accent) 18%, transparent);
  color: var(--app-accent); text-transform: capitalize; }
.empty { color: var(--app-muted); font-style: italic; text-align: center;
  padding: 22px; }
footer { color: var(--app-muted); font-size: 12px; text-align: center;
  margin-top: 26px; opacity: .8; }
</style>
</head>
<body>
<div class="shell">
<header>
<h1>__APP__</h1>
<div class="sub">built with PRISM &middot; running on Magic</div>
</header>
<div class="tabs" id="tabs"></div>
<div id="view"></div>
<footer>Generated for you &middot; every rule you confirmed is enforced by the backend</footer>
</div>
<script>
var APP = __SPEC__;
var cur = APP.entities[0].name;
function h(tag, attrs, kids) {
  var el = document.createElement(tag);
  for (var k in (attrs || {})) {
    if (k === "onclick") el.onclick = attrs[k];
    else if (k === "text") el.textContent = attrs[k];
    else el.setAttribute(k, attrs[k]);
  }
  (kids || []).forEach(function (c) { el.appendChild(c); });
  return el;
}
function ent() { return APP.entities.filter(function (e) { return e.name === cur; })[0]; }
function drawTabs() {
  var t = document.getElementById("tabs"); t.innerHTML = "";
  APP.entities.forEach(function (e) {
    t.appendChild(h("button", { class: "tab" + (e.name === cur ? " on" : ""),
      text: e.name, onclick: function () { cur = e.name; draw(); } }));
  });
}
function enumOf(e, f) {
  var r = (e.rules || []).filter(function (x) {
    return x.type === "enum" && x.field === f; })[0];
  return r ? r.values : null;
}
function draw() {
  drawTabs();
  var e = ent(), v = document.getElementById("view"); v.innerHTML = "";
  var form = h("form", {});
  e.fields.forEach(function (f) {
    var opts = enumOf(e, f.name), input;
    if (opts) {
      input = h("select", { name: f.name },
        [h("option", { value: "", text: "choose…", disabled: "", selected: "" })]
          .concat(opts.map(function (o) { return h("option", { value: o, text: o }); })));
    } else {
      input = h("input", { name: f.name,
        type: /INT|REAL|NUM/.test(f.type || "") ? "number" : "text" });
    }
    form.appendChild(h("label", { text: f.name.replace(/_/g, " ") }, [input]));
  });
  var msg = h("span", { class: "msg" });
  form.appendChild(h("div", { class: "actions" },
    [h("button", { class: "add", type: "submit", text: "Add" }), msg]));
  form.onsubmit = function (ev) {
    ev.preventDefault();
    var rec = {};
    e.fields.forEach(function (f) {
      var el = form.querySelector('[name="' + f.name + '"]');
      if (el && el.value !== "") rec[f.name] = el.value;
    });
    fetch("/magic/modules/" + APP.module + "/" + e.name, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify(rec) })
      .then(function (r) { if (!r.ok) throw new Error("save failed (" + r.status + ")");
        msg.textContent = "Added ✓"; form.reset(); load(); })
      .catch(function (err) { msg.textContent = String(err.message || err); });
  };
  v.appendChild(h("div", { class: "card" },
    [h("h2", { text: "Add " + cur.replace(/s$/, "") }), form]));
  var tbl = h("table", {}, [h("thead", {}, [h("tr", {},
    e.fields.map(function (f) { return h("th", { text: f.name.replace(/_/g, " ") }); }))]),
    h("tbody", { id: "rows" })]);
  v.appendChild(h("div", { class: "card" }, [h("h2", { text: cur }), tbl]));
  load();
}
function load() {
  var e = ent();
  fetch("/magic/modules/" + APP.module + "/" + e.name)
    .then(function (r) { return r.json(); })
    .then(function (data) {
      var rows = Array.isArray(data) ? data : [];
      var tb = document.getElementById("rows"); tb.innerHTML = "";
      if (!rows.length) {
        var td = h("td", { class: "empty", text: "No rows yet — add one above." });
        td.setAttribute("colspan", String(e.fields.length));
        tb.appendChild(h("tr", {}, [td]));
        return;
      }
      rows.forEach(function (r) {
        tb.appendChild(h("tr", {}, e.fields.map(function (f) {
          var val = r[f.name] == null ? "" : String(r[f.name]);
          if (val && enumOf(e, f.name)) {
            return h("td", {}, [h("span", { class: "pill", text: val })]);
          }
          return h("td", { text: val });
        })));
      });
    });
}
draw();
</script>
</body>
</html>
"""


def render_frontend(spec: dict, tokens: dict | None = None) -> str:
    """The customer's STANDALONE web app — one self-contained HTML file,
    themed by their design tokens, that Magic itself hosts under /etc/www.
    Same-origin with the deployed CRUD endpoints, so no proxy, no CORS and
    no PRISM in the serving path: the customer walks away with a URL.
    Deterministic (no model), like every other renderer here."""
    import json
    spec = ab.normalize_spec(spec)
    tokens = tokens or default_tokens(spec)
    payload = {"module": spec["module"],
               "entities": [{"name": e["name"],
                             "fields": _fields_payload(e),
                             "rules": _rules_payload(e)}
                            for e in spec["entities"]]}
    css = " ".join(f"{k}: {v};" for k, v in tokens.items())
    font = (tokens.get("--app-font", "").split(",")[0]
            .strip().strip("'\"")) or "Inter"
    html = _FRONTEND_TEMPLATE
    html = html.replace("__FONT__", font.replace(" ", "+"))
    html = html.replace("__APP__", _title(spec.get("db") or spec["module"]))
    html = html.replace("__TOKENS__", css)
    html = html.replace("__SPEC__", json.dumps(payload))
    return html
