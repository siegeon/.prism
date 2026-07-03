"""Deterministic Magic app renderer — PRISM as the expert user of Magic.

Takes a structured app spec (entities + fields + rules) and emits
WHITESPACE-PERFECT Hyperlambda: schema DDL + list/add endpoint files
with rule guards. The idioms here are empirically verified against a
live servergardens/magic-backend:v22.10.10 tenant (v_shop/orders +
p_lib/loan_create passed objective rule-probes).

This closes the gap the 7B experiment exposed: a small model reliably
produces the SPEC but cannot emit precise Hyperlambda. So the division
of labour is: model -> spec, PRISM -> render. The render is pure code,
so the whitespace/tree structure is always exact.
"""

from __future__ import annotations

from prism_service.services import magic_client as mc

# SQLite column type -> Hyperlambda .arguments type
_ARG_TYPE = {"TEXT": "string", "INTEGER": "long", "REAL": "decimal"}

I = "   "  # one Hyperlambda indent level (exactly 3 spaces)


def _arg_type(sql_type: str) -> str:
    return _ARG_TYPE.get(sql_type.upper(), "string")


def _fields_of(entity: dict) -> list[dict]:
    return entity.get("fields", [])


# --- schema -----------------------------------------------------------------


def render_schema(spec: dict) -> str:
    """One execute payload creating every table (id PK + declared fields)."""
    lines = [f"sqlite.connect:{spec['db']}"]
    for ent in spec["entities"]:
        cols = ["id INTEGER PRIMARY KEY"]
        for f in _fields_of(ent):
            cols.append(f"{f['name']} {f['type']}")
        lines.append(f"{I}sqlite.execute:CREATE TABLE IF NOT EXISTS "
                     f"{ent['name']} ({', '.join(cols)})")
    for ent in spec["entities"]:
        for row in ent.get("seed", []):
            names = ",".join(row.keys())
            vals = ",".join("'" + str(v).replace("'", "''") + "'"
                            for v in row.values())
            lines.append(f"{I}sqlite.execute:INSERT INTO {ent['name']} "
                         f"({names}) VALUES ({vals})")
    return "\n".join(lines)


# --- endpoint bodies (verified idioms; whitespace is exact) ------------------


def render_list_endpoint(entity: dict, db: str) -> str:
    t = entity["name"]
    return (f"sqlite.connect:{db}\n"
            f"{I}sqlite.select:SELECT * FROM {t}\n"
            f"{I}return-nodes:x:@sqlite.select/*")


def _fk_guard(rule: dict) -> list[str]:
    f, ref = rule["field"], rule["ref"]
    return [
        f"{I}sqlite.select:SELECT id FROM {ref} WHERE id = @fk",
        f"{I*2}@fk:x:@.arguments/*/{f}",
        f"{I}if",
        f"{I*2}not",
        f"{I*3}exists:x:@sqlite.select/*",
        f"{I*2}.lambda",
        f"{I*3}response.status.set:400",
        f"{I*3}return",
        f"{I*4}error:{f} must reference an existing {ref}",
    ]


def _min_guard(rule: dict) -> list[str]:
    f, v = rule["field"], rule.get("value", 0)
    return [
        f"{I}if",
        f"{I*2}lt:x:@.arguments/*/{f}",
        f"{I*3}.:decimal:{v}",
        f"{I*2}.lambda",
        f"{I*3}response.status.set:400",
        f"{I*3}return",
        f"{I*4}error:{f} must be at least {v}",
    ]


def _enum_checks_inside(rule: dict) -> list[str]:
    f = rule["field"]
    flag = f".valid_{f}"
    out: list[str] = []
    for val in rule["values"]:
        out += [
            f"{I}if",
            f"{I*2}eq:x:@.arguments/*/{f}",
            f"{I*3}.:{val}",
            f"{I*2}.lambda",
            f"{I*3}set-value:x:@{flag}",
            f"{I*4}.:bool:true",
        ]
    out += [
        f"{I}if",
        f"{I*2}not",
        f"{I*3}get-value:x:@{flag}",
        f"{I*2}.lambda",
        f"{I*3}response.status.set:400",
        f"{I*3}return",
        f"{I*4}error:{f} must be one of {', '.join(rule['values'])}",
    ]
    return out


def _capacity_guard(rule: dict, child_table: str) -> list[str]:
    """Count/capacity rule: reject if the referenced parent is missing OR
    already has >= capacity children. One combined SELECT (verified idiom):
    it returns a row only when the parent exists AND has room, so a single
    if/not/exists rejects both cases (R1 missing + R2 full)."""
    f, ref = rule["field"], rule["ref"]
    cap = rule.get("capacity_field", "capacity")
    sql = (f"SELECT id FROM {ref} WHERE id = @cap AND {cap} > "
           f"(SELECT COUNT(*) FROM {child_table} WHERE {f} = @cap)")
    return [
        f"{I}sqlite.select:{sql}",
        f"{I*2}@cap:x:@.arguments/*/{f}",
        f"{I}if",
        f"{I*2}not",
        f"{I*3}exists:x:@sqlite.select/*",
        f"{I*2}.lambda",
        f"{I*3}response.status.set:400",
        f"{I*3}return",
        f"{I*4}error:{ref} not found or at capacity",
    ]


def render_add_endpoint(entity: dict, db: str) -> str:
    t = entity["name"]
    rules = entity.get("rules", [])
    lines = [".arguments"]
    for fld in _fields_of(entity):
        lines.append(f"{I}{fld['name']}:{_arg_type(fld['type'])}")
    # enum flags declared before connect (elder-sibling reachable)
    for r in rules:
        if r["type"] == "enum":
            lines.append(f".valid_{r['field']}:bool:false")
    lines.append(f"sqlite.connect:{db}")
    for r in rules:
        if r["type"] == "fk":
            lines += _fk_guard(r)
        elif r["type"] == "min":
            lines += _min_guard(r)
        elif r["type"] == "enum":
            lines += _enum_checks_inside(r)
        elif r["type"] == "capacity":
            lines += _capacity_guard(r, t)
    cols = [f["name"] for f in _fields_of(entity)]
    params = ",".join("@" + c for c in cols)
    lines.append(f"{I}sqlite.execute:INSERT INTO {t} "
                 f"({','.join(cols)}) VALUES ({params})")
    for c in cols:
        lines.append(f"{I*2}@{c}:x:@.arguments/*/{c}")
    lines += [f"{I}return", f"{I*2}status:created"]
    return "\n".join(lines)


# --- assembly + deploy ------------------------------------------------------


# placeholder tokens a small model emits when it doesn't know a real value
_PLACEHOLDERS = {"", "table", "..", "...", "x", "entity", "field", "name",
                 "string", "text", "value", "foo", "bar"}


def normalize_spec(spec: dict) -> dict:
    """Canonicalize a small-model spec so the pipeline is stable regardless of
    SLM wobble: lowercase/strip entity + field names, drop the explicit 'id'
    field (we add the PK), strip a trailing '.<col>' from an fk ref, and DROP
    malformed rules (empty field, or an fk ref that's a placeholder like
    'table'/'..'). Entities with a placeholder/empty name are dropped too."""
    # A micro-model draft may omit db or module entirely (root cause of the
    # corefit silent-build failure: db=None made deploy + UI-write throw).
    # Default them from each other so the renderer ALWAYS has both.
    if not spec.get("db"):
        spec["db"] = spec.get("module") or "app"
    if not spec.get("module"):
        spec["module"] = spec["db"]
    # Auto-link obvious references so the interview never asks a customer
    # about ids (owner feedback: "users don't know about ids and such").
    #   bookings.member_id + a members entity -> fk added silently.
    #   members.member_id -> the record's own identifier, not a reference.
    names = {(e.get("name") or "").strip().lower()
             for e in spec.get("entities", []) or []}
    for ent in spec.get("entities", []) or []:
        en = (ent.get("name") or "").strip().lower()
        own = en[:-1] if en.endswith("s") and len(en) > 3 else en
        fks = {r.get("field") for r in ent.get("rules", []) or []
               if r.get("type") == "fk"}
        for f in ent.get("fields", []) or []:
            fn = (f.get("name") or "").strip()
            if not fn.endswith("_id") or fn in fks:
                continue
            base = fn[:-3].lower()
            if base == own or base == en:
                # own identifier: redundant with the auto PK — drop the field
                # entirely (ids are for computers, not customer UIs).
                ent["fields"] = [x for x in ent["fields"]
                                 if (x.get("name") or "").strip() != fn]
                continue
            target = next((n for n in names
                           if n in (base, base + "s", base + "es")), None)
            if target and target != en:
                ent.setdefault("rules", []).append(
                    {"type": "fk", "field": fn, "ref": target})
    for ent in spec.get("entities", []):
        ent["name"] = (ent.get("name") or "").strip().lower()
        fields = []
        for f in ent.get("fields", []) or []:
            fn = (f.get("name") or "").strip()
            if fn and fn.lower() != "id":
                f["name"] = fn
                fields.append(f)
        ent["fields"] = fields
        clean = []
        for r in ent.get("rules", []) or []:
            if not (r.get("field") or "").strip():
                continue
            if r.get("type") == "fk":
                ref = (r.get("ref") or "").split(".")[0].strip().lower()
                if ref in _PLACEHOLDERS:
                    continue  # drop garbage fk (no real target)
                r["ref"] = ref
            clean.append(r)
        ent["rules"] = clean
    spec["entities"] = [e for e in spec.get("entities", [])
                        if e.get("name") and e["name"] not in _PLACEHOLDERS]
    return spec


def render_app(spec: dict) -> dict:
    """Pure render: returns the schema payload + {file_path: hl_content}.
    No network — unit-testable."""
    spec = normalize_spec(spec)
    mod = spec["module"]
    db = spec["db"]
    files: dict[str, str] = {}
    for ent in spec["entities"]:
        base = f"/modules/{mod}/{ent['name']}"
        files[f"{base}.get.hl"] = render_list_endpoint(ent, db)
        files[f"{base}.post.hl"] = render_add_endpoint(ent, db)
    return {"schema": render_schema(spec), "files": files}


def _save_payload(mod: str, path: str, content: str) -> str:
    body = content.replace('"', '""')
    return (f"io.folder.create:/modules/{mod}/\n"
            f'io.file.save:{path}\n{I}.:@"{body}"')


def deploy_app(spec: dict, execute=mc.execute) -> dict:
    """Render + deploy to the connected tenant. `execute` is injectable for
    tests. Returns the built endpoint list."""
    app = render_app(spec)
    execute(app["schema"])
    mod = spec["module"]
    for path, content in app["files"].items():
        execute(_save_payload(mod, path, content))
    endpoints = []
    for ent in spec["entities"]:
        endpoints.append(f"GET magic/modules/{mod}/{ent['name']}")
        endpoints.append(f"POST magic/modules/{mod}/{ent['name']}")
    return {"module": mod, "db": spec["db"], "endpoints": endpoints,
            "file_count": len(app["files"])}


def deploy_frontend(module: str, html: str, execute=mc.execute) -> str:
    """Host the generated app IN the customer's Magic backend: save the
    standalone index.html under /etc/www/<module>/ — Magic serves it at
    /<module>/ on the tenant's own URL. Returns the site-relative path."""
    body = html.replace('"', '""')
    execute(f"io.folder.create:/etc/www/{module}/\n"
            f'io.file.save:/etc/www/{module}/index.html\n{I}.:@"{body}"')
    return f"/{module}/"


# --- deterministic whitespace repair (closes the small-model gap) ------------


def normalize_indent(hl: str) -> str:
    """Rescale a model's indentation to exact 3-space multiples.

    A small model gets Hyperlambda's structure right but often emits a
    non-3 indent unit (2 or 4 spaces) or drifts — which trips Magic's
    whitespace-strict parser. If the emitted indentation is internally
    CONSISTENT (a single unit), its GCD is that unit, and we can map each
    line to depth = width/unit and re-emit at depth*3. Strips code fences.
    """
    lines = [l for l in hl.replace("\t", "   ").splitlines()]
    lines = [l for l in lines if not l.strip().startswith("```")]
    widths = {len(l) - len(l.lstrip(" ")) for l in lines if l.strip()}
    widths.discard(0)
    unit = 0
    for w in sorted(widths):
        unit = w if unit == 0 else _gcd(unit, w)
    out = []
    for l in lines:
        if not l.strip():
            out.append("")
            continue
        w = len(l) - len(l.lstrip(" "))
        depth = (w // unit) if unit else 0
        out.append(("   " * depth) + l.lstrip(" "))
    return "\n".join(out).strip("\n")


def _gcd(a: int, b: int) -> int:
    while b:
        a, b = b, a % b
    return a
