"""Completeness / confidence checker — the brain-side gap detector.

Customers never arrive with a complete ruleset. PRISM's job (the middle)
is to know what a buildable app definition needs, find what's missing or
ambiguous in the PI-produced spec, and emit a structured list of GAPS —
each carrying a plain follow-up question the SLM voices on the PI screen.

Detection is PRISM's intelligence (deterministic structural checks now, a
brain-learned layer later); phrasing the question is the SLM's job. No
extra inference layer: the same front-door model just voices what PRISM
decides to ask.
"""

from __future__ import annotations

# field-name hints (heuristics the learned layer will later refine)
_ENUM_HINTS = {"status", "state", "type", "kind", "stage", "priority", "role"}
_MIN_HINTS = {"price", "amount", "total", "cost", "qty", "quantity",
              "stock", "balance", "salary", "fee", "rate", "count", "age"}

# gaps that BLOCK a build vs ones that merely sharpen it
_BLOCKING = {"no_entities", "empty_entity", "orphan_fk"}


def _gap(kind, entity, field, detail, question):
    return {"kind": kind, "entity": entity, "field": field,
            "detail": detail, "question": question,
            "blocking": kind in _BLOCKING}


def _singular(name: str) -> str:
    return name[:-1] if name.endswith("s") and len(name) > 3 else name


def _an(word: str) -> str:
    """'a client' / 'an appointment' — questions read like a person wrote them."""
    w = str(word).strip()
    return f"an {w}" if w[:1].lower() in "aeiou" else f"a {w}"


def find_gaps(spec: dict) -> list[dict]:
    """Return the ordered list of gaps in a PI-produced spec — structural
    first (blocking), then confidence/clarifying. Each gap carries a
    plain question for the SLM to voice. The spec is canonicalized first
    (names lowercased, malformed/placeholder rules dropped) so a small
    model's wobble never produces a garbage question."""
    import copy
    from prism_service.services.magic_app_builder import normalize_spec
    spec = normalize_spec(copy.deepcopy(spec))
    gaps: list[dict] = []
    entities = spec.get("entities", []) or []
    if not entities:
        return [_gap("no_entities", None, None, "No entities defined yet.",
                     "What does your business need to keep track of? "
                     "(for example: customers, orders, appointments)")]
    names = {e.get("name", "") for e in entities}
    for e in entities:
        en = e.get("name", "")
        fields = e.get("fields", []) or []
        rules = e.get("rules", []) or []
        fk_fields = {r.get("field") for r in rules if r.get("type") == "fk"}
        enum_fields = {r.get("field") for r in rules if r.get("type") == "enum"}
        min_fields = {r.get("field") for r in rules if r.get("type") == "min"}
        if not fields:
            gaps.append(_gap("empty_entity", en, None,
                             f"'{en}' has no fields.",
                             f"What details should each {_singular(en)} record hold?"))
        for r in rules:
            if r.get("type") == "fk" and r.get("ref") not in names:
                ref = r.get("ref")
                ref_h = str(ref).replace("_", " ")
                gaps.append(_gap("orphan_fk", en, r.get("field"),
                                 f"'{en}.{r.get('field')}' references '{ref}', "
                                 "which isn't defined.",
                                 f"Each {_singular(en)} is connected to a {ref_h} — "
                                 f"should your app keep track of {ref_h}s too?"))
        for f in fields:
            fn = f.get("name", "")
            ft = (f.get("type") or "").upper()
            low = fn.lower()
            if fn.endswith("_id") and fn not in fk_fields:
                # normalize_spec auto-links references whose target entity
                # exists and silences own-id fields; anything left means the
                # target is genuinely absent — ask in BUSINESS language
                # (owner feedback: no ids/tables at the customer).
                target = fn[:-3].replace("_", " ")
                own = _singular(en)
                if fn[:-3].lower() in (en, _singular(en)):
                    continue                    # own identifier, nothing to ask
                gaps.append(_gap("unlinked_fk", en, fn,
                                 f"'{en}.{fn}' looks like a reference with no target.",
                                 f"Each {own} mentions a {target} — should your "
                                 f"app also keep a list of {target}s?"))
            if low in _ENUM_HINTS and ft == "TEXT" and fn not in enum_fields:
                fn_h = fn.replace("_", " ")
                gaps.append(_gap("missing_enum", en, fn,
                                 f"'{en}.{fn}' has no allowed values.",
                                 f"What {fn_h} options should {_an(_singular(en))} "
                                 "have? (for example: new, paid, shipped)"))
            if any(h == low or h in low for h in _MIN_HINTS) and \
                    ft in ("REAL", "INTEGER") and fn not in min_fields:
                fn_h = fn.replace("_", " ")
                gaps.append(_gap("maybe_min", en, fn,
                                 f"'{en}.{fn}' has no bound.",
                                 f"Should {_an(_singular(en))}'s {fn_h} always be "
                                 "zero or more?"))
    # blocking gaps first
    gaps.sort(key=lambda g: (not g["blocking"],))
    return gaps


def is_complete(spec: dict) -> bool:
    """True when no BLOCKING gap remains (clarifying gaps may still exist)."""
    return not any(g["blocking"] for g in find_gaps(spec))


def check_spec(spec: dict, archetypes=None) -> dict:
    """Full completeness check: structural gaps (deterministic) + learned
    gaps (domain-aware). `complete` is False while a blocking gap remains;
    clarifying gaps sharpen the build but don't block."""
    arch = archetypes if archetypes is not None else current_archetypes()
    gaps = find_gaps(spec) + learned_gaps(spec, arch)
    return {"complete": not any(g["blocking"] for g in gaps),
            "domain": match_domain(spec, arch),
            "blocking": [g for g in gaps if g["blocking"]],
            "clarifying": [g for g in gaps if not g["blocking"]],
            "questions": [g["question"] for g in gaps]}


# --- learned layer: what apps of THIS business type usually need -------------
# Seed archetypes; the self-learning hook (learn_from_spec) grows them from
# real completed builds so the questions get sharper over time.
ARCHETYPES = {
    "clinic": {
        "signals": {"patient", "appointment", "doctor", "visit", "clinic",
                    "medical", "prescription"},
        "entities": {"patients": ["name", "phone", "email"],
                     "appointments": ["patient_id", "date", "status"],
                     "billing": ["appointment_id", "amount", "paid"]},
    },
    "shop": {
        "signals": {"product", "order", "customer", "cart", "item", "shop",
                    "store", "supplier", "inventory"},
        "entities": {"customers": ["name", "email"],
                     "products": ["name", "price", "stock"],
                     "orders": ["customer_id", "total", "status"],
                     "order_items": ["order_id", "product_id", "qty"]},
    },
    "crm": {
        "signals": {"lead", "deal", "contact", "company", "pipeline", "crm",
                    "opportunity"},
        "entities": {"contacts": ["name", "email", "company"],
                     "deals": ["contact_id", "amount", "stage"]},
    },
}


def _tokens(spec: dict) -> set:
    toks = set()
    for e in spec.get("entities", []) or []:
        toks.add((e.get("name") or "").lower().rstrip("s"))
        for f in e.get("fields", []) or []:
            fn = (f.get("name") or "").lower()
            toks.add(fn)
            # patient_id carries the 'patient' signal — split compounds so
            # the >=2 match threshold sees real domain evidence.
            for part in fn.split("_"):
                if part and part != "id":
                    toks.add(part)
    return toks


def match_domain(spec: dict, archetypes=ARCHETYPES):
    """Best-matching business archetype by signal overlap, or None."""
    toks = _tokens(spec)
    best, score = None, 0
    for dom, a in archetypes.items():
        s = sum(1 for sig in a["signals"] if sig in toks or sig + "s" in toks)
        if s > score:
            best, score = dom, s
    # one shared signal (e.g. 'appointments') is coincidence, not identity —
    # a dog-grooming salon must not be labeled a clinic (owner: 75733882).
    return best if score >= 2 else None


def learned_gaps(spec: dict, archetypes=ARCHETYPES) -> list[dict]:
    """Domain-aware gaps: given the matched business type, flag typical-but-
    missing entities/fields as clarifying questions ('Clinics usually also
    track billing — do you need that?'). This is the brain asking the RIGHT
    questions — the thing a generic model can't do without accumulated context."""
    dom = match_domain(spec, archetypes)
    if not dom:
        return []
    a = archetypes[dom]
    present = {(e.get("name") or "").lower().rstrip("s")
               for e in spec.get("entities", []) or []}
    out: list[dict] = []
    for ent, typ_fields in a["entities"].items():
        key = ent.rstrip("s")
        if key not in present:
            out.append(_gap("learned_entity", ent, None,
                            f"{dom} apps usually track '{ent}', which is missing.",
                            f"Businesses like yours usually also keep track "
                            f"of {ent} — would that be useful?"))
        else:
            e = next((x for x in spec["entities"]
                      if (x.get("name") or "").lower().rstrip("s") == key), None)
            have = {(f.get("name") or "").lower() for f in (e.get("fields", []) if e else [])}
            missing = [tf for tf in typ_fields
                       if tf.lower() not in have and not tf.endswith("_id")]
            if missing:
                # ONE question per entity, in plain words — never a
                # field-by-field interrogation (owner: 9f32db02).
                nice = ", ".join(m.replace("_", " ") for m in missing)
                out.append(_gap("learned_fields", ent, missing[0],
                                f"{ent} usually also records: {nice}.",
                                f"For each {_singular(ent)}, should we also "
                                f"note things like {nice}?"))
    return out[:6]


def _learned_path():
    from pathlib import Path
    from prism_service import config
    return Path(config.DATA_DIR) / "magic_learned_archetypes.json"


def current_archetypes() -> dict:
    """Seed ARCHETYPES + everything learned from completed builds, merged.
    The learned overlay persists in the data dir, so the gap detector's
    expertise SURVIVES restarts and grows with every customer app."""
    import copy
    import json as _json
    arch = copy.deepcopy(ARCHETYPES)
    try:
        raw = _json.loads(_learned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return arch
    for dom, d in raw.items():
        base = arch.setdefault(dom, {"signals": set(), "entities": {}})
        base["signals"] = set(base.get("signals", set())) | set(d.get("signals", []))
        for ent, fields in (d.get("entities") or {}).items():
            cur = base["entities"].setdefault(ent, [])
            for f in fields:
                if f not in cur:
                    cur.append(f)
    return arch


def _save_learned(arch: dict) -> None:
    import json as _json
    import os
    p = _learned_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    out = {d: {"signals": sorted(v.get("signals", [])),
               "entities": v.get("entities", {})} for d, v in arch.items()}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(_json.dumps(out, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def learn_from_spec(spec: dict, archetypes=None, domain=None) -> str | None:
    """Self-learning hook: fold a COMPLETED build's entities/fields into the
    matched archetype so future asks of this business type get sharper
    questions. With no explicit `archetypes`, learning PERSISTS to the data
    dir; an unmatched vertical becomes a NEW archetype named by the app's
    db/module (a completed build is ground truth — the dog-grooming salon
    teaches PRISM what dog-grooming salons need). Returns the domain."""
    explicit = archetypes is not None
    arch = archetypes if explicit else current_archetypes()
    dom = domain or match_domain(spec, arch)
    if not dom:
        slug = str(spec.get("db") or spec.get("module") or "").strip()             .lower().replace("_", " ")
        if not slug:
            return None
        dom = slug
        arch.setdefault(dom, {"signals": {w for w in slug.split() if len(w) > 2},
                              "entities": {}})
    if dom not in arch:
        return None
    ents = arch[dom]["entities"]
    for e in spec.get("entities", []) or []:
        name = (e.get("name") or "").lower()
        if not name:
            continue
        fields = [(f.get("name") or "").lower() for f in e.get("fields", []) or []]
        if name in ents:
            for fn in fields:
                if fn and fn not in ents[name]:
                    ents[name].append(fn)
        else:
            ents[name] = [f for f in fields if f]
    if not explicit:
        _save_learned(arch)
    return dom
