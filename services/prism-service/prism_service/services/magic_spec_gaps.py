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


def find_gaps(spec: dict) -> list[dict]:
    """Return the ordered list of gaps in a PI-produced spec — structural
    first (blocking), then confidence/clarifying. Each gap carries a
    plain question for the SLM to voice."""
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
                gaps.append(_gap("orphan_fk", en, r.get("field"),
                                 f"'{en}.{r.get('field')}' references '{ref}', "
                                 "which isn't defined.",
                                 f"You linked {en} to {ref}, but there's no {ref} "
                                 f"yet — should I create a {ref} table, or did you "
                                 "mean something else?"))
        for f in fields:
            fn = f.get("name", "")
            ft = (f.get("type") or "").upper()
            low = fn.lower()
            if fn.endswith("_id") and fn not in fk_fields:
                target = fn[:-3]
                gaps.append(_gap("unlinked_fk", en, fn,
                                 f"'{en}.{fn}' looks like a reference with no target.",
                                 f"Should {en}.{fn} always point to an existing "
                                 f"{target}?"))
            if low in _ENUM_HINTS and ft == "TEXT" and fn not in enum_fields:
                gaps.append(_gap("missing_enum", en, fn,
                                 f"'{en}.{fn}' has no allowed values.",
                                 f"What values can a {_singular(en)}'s {fn} be? "
                                 "(for example: new, paid, shipped)"))
            if any(h == low or h in low for h in _MIN_HINTS) and \
                    ft in ("REAL", "INTEGER") and fn not in min_fields:
                gaps.append(_gap("maybe_min", en, fn,
                                 f"'{en}.{fn}' has no bound.",
                                 f"Can {en}.{fn} be negative, or should it be at "
                                 "least 0?"))
    # blocking gaps first
    gaps.sort(key=lambda g: (not g["blocking"],))
    return gaps


def is_complete(spec: dict) -> bool:
    """True when no BLOCKING gap remains (clarifying gaps may still exist)."""
    return not any(g["blocking"] for g in find_gaps(spec))


def check_spec(spec: dict) -> dict:
    gaps = find_gaps(spec)
    return {"complete": not any(g["blocking"] for g in gaps),
            "blocking": [g for g in gaps if g["blocking"]],
            "clarifying": [g for g in gaps if not g["blocking"]],
            "questions": [g["question"] for g in gaps]}
