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
