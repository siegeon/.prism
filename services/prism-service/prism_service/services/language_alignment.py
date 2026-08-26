"""Deterministic language-alignment pass over a project's tasks (task
f07c9cea, epic df0eed4a, owner rule mx-f49a5c).

``align_language`` finds tasks whose free text is not yet plain
Simplified Technical English and, in apply mode, brings it into line.
It never invents a rewrite of its own -- every fix comes from the SAME
deterministic normaliser TaskService._apply_ste already runs on every
task write (services/ste.py, ``ste.normalize``), so a task fixed here
reads exactly as it would if the fix had been made by hand at write
time.

Two safety properties this module holds itself to:

* ``plan_doc`` and ``plan_diagram`` are NEVER touched, even if a caller
  names them in ``fields`` -- the same carve-out TaskService._apply_ste
  itself makes (a plan a human already approved at plan_gate must not
  shift under them).
* Dry run (``apply=False``) writes nothing at all. Apply mode writes
  through ``TaskService.update`` with NO field kwargs -- a bare call --
  so TaskService's own ``_apply_ste`` does the actual rewrite and
  records its own ``ste_normalise`` history row for every field it
  touches. This module never hand-writes a task's fields itself; it
  only decides WHICH tasks are worth an update() call, and reads
  ``TaskService.last_style`` back afterwards to attribute the change to
  a field and a rule for its own report.
"""

from __future__ import annotations

DEFAULT_FIELDS: list[str] = [
    "title", "description", "oracle", "likely_misfire", "stop_if",
    "completion_proof", "premise_notes",
]
DEFAULT_BATCH_SIZE = 50
_SAMPLE_CAP = 10
_NEVER_TOUCH = ("plan_doc", "plan_diagram")

_MODE_FOR_FIELD = {
    "title": "flavored",
    "description": "flavored",
    "completion_proof": "flavored",
    "premise_notes": "flavored",
    "oracle": "strict",
    "likely_misfire": "strict",
    "stop_if": "strict",
}


def _would_change_field(task, field_name: str):
    """Return ``(changed, before, after, rules)`` for one field on
    ``task``, computed with the exact normaliser TaskService._apply_ste
    uses. Never writes -- a pure preview."""
    from prism_service.services import ste

    mode = _MODE_FOR_FIELD.get(field_name, "flavored")
    if field_name == "stop_if":
        before_list = list(getattr(task, "stop_if", None) or [])
        if not before_list:
            return False, before_list, before_list, []
        after_list: list[str] = []
        rules: list[str] = []
        changed = False
        for entry in before_list:
            fixed, entry_rules = ste.normalize(entry, mode="strict")
            after_list.append(fixed)
            if fixed != entry:
                changed = True
            for rule in entry_rules:
                if rule not in rules:
                    rules.append(rule)
        return changed, before_list, after_list, rules

    before = getattr(task, field_name, "") or ""
    if not before:
        return False, before, before, []
    after, rules = ste.normalize(before, mode=mode)
    return after != before, before, after, rules


def align_language(
    project: str,
    apply: bool,
    fields: "list[str] | None" = None,
    batch_size: "int | None" = None,
) -> dict:
    """One pass over ``project``'s tasks.

    ``apply=False`` (dry run) only counts and samples candidate
    changes; it writes nothing. ``apply=True`` writes each flagged task
    through ``TaskService.update`` (a bare call, no field kwargs) so
    the server's own STE pipeline performs the rewrite and records the
    ``ste_normalise`` history row, then rebuilds
    ``OntologyGraph(project)`` once at the end of the whole pass -- not
    once per task.

    At most ``batch_size`` tasks are counted per call (default
    ``DEFAULT_BATCH_SIZE``) -- a very large backlog drains over several
    calls instead of one long pass. A deleted task is skipped. A field
    with no loose text is left alone; ``plan_doc``/``plan_diagram`` are
    never touched even if named in ``fields``.

    Returns::

        {"scanned": int,
         "would_change" | "changed": int,
         "per_rule": {rule_name: count},
         "per_field": {field_name: count},
         "sample": [{"task_id", "field", "before", "after"}, ...]}

    (at most 10 sample entries). Idempotent: running this again right
    after an apply pass finds nothing left to change.
    """
    from prism_service.project_context import get_project

    field_names = [f for f in (fields or DEFAULT_FIELDS)
                   if f not in _NEVER_TOUCH]
    limit = int(batch_size) if batch_size else DEFAULT_BATCH_SIZE

    task_svc = get_project(project).task_svc

    scanned = 0
    touched = 0
    per_rule: dict[str, int] = {}
    per_field: dict[str, int] = {}
    sample: list[dict] = []

    for task in task_svc.list():
        if str(getattr(task, "status", "") or "") == "deleted":
            continue
        if touched >= limit:
            break
        scanned += 1

        candidates = []
        for field_name in field_names:
            changed, before, after, rules = _would_change_field(task, field_name)
            if changed:
                candidates.append((field_name, before, after, rules))
        if not candidates:
            continue

        touched += 1
        if apply:
            task_svc.update(task.id)  # bare -- TaskService._apply_ste writes
            style = getattr(task_svc, "last_style", {}) or {}
            fixed = style.get("fixed", {}) or {}
            after_task = task_svc.get(task.id) if fixed else None
            for field_name, before, after, _rules in candidates:
                applied_rules = fixed.get(field_name)
                if not applied_rules:
                    continue
                for rule in applied_rules:
                    per_rule[rule] = per_rule.get(rule, 0) + 1
                per_field[field_name] = per_field.get(field_name, 0) + 1
                if len(sample) < _SAMPLE_CAP:
                    real_after = (
                        getattr(after_task, field_name, after)
                        if after_task is not None else after)
                    sample.append({"task_id": task.id, "field": field_name,
                                    "before": before, "after": real_after})
        else:
            for field_name, before, after, rules in candidates:
                for rule in rules:
                    per_rule[rule] = per_rule.get(rule, 0) + 1
                per_field[field_name] = per_field.get(field_name, 0) + 1
                if len(sample) < _SAMPLE_CAP:
                    sample.append({"task_id": task.id, "field": field_name,
                                    "before": before, "after": after})

    if apply and touched:
        try:
            from prism_service.services.ontology_graph import OntologyGraph
            OntologyGraph(project).rebuild()
        except Exception:
            pass

    result: dict = {
        "scanned": scanned, "per_rule": per_rule,
        "per_field": per_field, "sample": sample,
    }
    result["changed" if apply else "would_change"] = touched
    return result
