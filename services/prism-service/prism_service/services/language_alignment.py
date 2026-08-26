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

import atexit
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

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


# ----------------------------------------------------------------------
# Ingestion-path coverage registry (task c7edf4e2, epic cc9a44c8 — "every
# text writer registers with Align language and cannot drift").
#
# ste.on_apply (services/ste.py) fires a listener every time ste actually
# normalises a piece of text -- one call per real write, from whichever
# code path reached it. _on_ste_apply below is that listener: it turns
# the call's stack frames into ONE path label (_path_label) and records
# one hit against it (record_coverage), so coverage(project) can answer
# "which ingestion paths has this project actually seen run through
# Align language, and which have never fired". A path this module has
# never heard from is the drift this task exists to catch.
# ----------------------------------------------------------------------

# Priority-ordered: the FIRST rule whose module appears in a call's frames
# wins. Order matters where two modules could both appear in one call's
# stack -- e.g. a REST create that reaches TaskService, which reaches
# ste -- so the caller-facing module (api.tasks) must be checked before
# the service module it happens to call (task_service, not even listed
# below, since nothing REST/MCP-originated should ever attribute there).
#
# event_handlers is deliberately checked BEFORE memory_service, even
# though the write-up that names both lists memory_service first: a
# reflection write ALWAYS reaches ste through MemoryService.store
# (services/event_handlers.py's _persist_reflection calls ctx.memory_svc.
# store directly), so the two modules co-occur in every one call's frames
# -- if memory_service were checked first, "reflection" could never be
# produced at all, which defeats the point of naming it. Same "outer,
# more specific caller wins" rule the mcp.tools check below already
# applies; task_runner/conductor_flow keep the given order because THEIR
# real co-occurrence (task_runner.run_one_step calls conductor_flow.
# flow_report) already resolves correctly that way -- task_runner is the
# more specific caller there too.
_LABEL_RULES: list[tuple[str, str]] = [
    ("prism_service.api.tasks", "api.tasks"),
    ("prism_service.api.signals", "api.signals"),
    ("prism_service.api.memory", "api.memory"),
    ("prism_service.services.work_item_sync", "work_item_sync"),
    ("prism_service.services.task_runner", "task_runner"),
    ("prism_service.api.conductor_flow", "conductor_flow"),
    ("prism_service.services.language_alignment_worker",
     "language_alignment_worker"),
    ("prism_service.services.event_handlers", "reflection"),
    ("prism_service.services.memory_service", "memory_service"),
    ("prism_service.services.signal_store", "signal_store"),
]

# The MCP dispatcher (prism_service.mcp.tools) is ONE function branching
# on `if name == "task_create":` for every tool -- there is no separate
# function per tool to key off, so this checks the frame's own `name`
# local (ste's tool_hint, see _caller_frames) instead. Checked before
# _LABEL_RULES above: an MCP call's frames also include whichever service
# module it reaches (memory_service for mcp memory_store, etc.), and the
# caller-facing MCP tool name must win over that.
_MCP_TOOL_LABELS = {
    "task_create": "mcp.task_create",
    "task_update": "mcp.task_update",
    "memory_store": "mcp.memory_store",
    "signal_post": "mcp.signal_post",
}

_COVERAGE_FILE = "align_language_coverage.json"
_WRITE_DEBOUNCE_S = 1.0

_coverage_cache: dict[str, dict[str, dict]] = {}
_last_write_at: dict[str, float] = {}
_dirty_projects: set[str] = set()


def _path_label(frames: list[tuple[str, str, str, str]]) -> str:
    """One label for a call, from its ste-captured frames. First match
    wins, checked in the fixed priority order documented above; a call
    that matches nothing named gets "unknown:<outermost prism_service
    module>" so a genuinely new ingestion path shows up as a name, never
    silently drops out of the registry."""
    for module, _function, tool_hint, _project_hint in frames:
        if module == "prism_service.mcp.tools" and tool_hint in _MCP_TOOL_LABELS:
            return _MCP_TOOL_LABELS[tool_hint]

    for wanted_module, label in _LABEL_RULES:
        for module, _function, _tool_hint, _project_hint in frames:
            if module == wanted_module:
                return label

    for module, _function, _tool_hint, _project_hint in reversed(frames):
        if module.startswith("prism_service"):
            return f"unknown:{module}"
    return "unknown:unknown"


def _project_from_frames(frames: list[tuple[str, str, str, str]]) -> str:
    """The project a call was made against, read off the same frames
    (ste's project_hint -- a local `project`/`project_id`, or a
    TaskService/MemoryService instance's own `.project`). Falls back to
    "default" when no frame carries one cheaply -- there is no
    process-wide "current project" helper in project_context.py to fall
    back to first."""
    for _module, _function, _tool_hint, project_hint in frames:
        if project_hint:
            return project_hint
    return "default"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _coverage_path(project: str) -> Path:
    from prism_service.config import project_data_dir
    return project_data_dir(project) / _COVERAGE_FILE


def _load_coverage(project: str) -> dict[str, dict]:
    if project in _coverage_cache:
        return _coverage_cache[project]
    data: dict[str, dict] = {}
    try:
        path = _coverage_path(project)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8")) or {}
    except Exception:
        data = {}
    _coverage_cache[project] = data
    return data


def _write_coverage(project: str, force: bool = False) -> None:
    """Debounced: at most one real disk write per project per second
    (task c7edf4e2). The in-memory cache (read by coverage() below) is
    always current -- only the disk write is delayed, and a delayed
    write is flushed on process exit by _flush_all."""
    now = time.monotonic()
    if not force and now - _last_write_at.get(project, 0.0) < _WRITE_DEBOUNCE_S:
        _dirty_projects.add(project)
        return
    data = _coverage_cache.get(project, {})
    try:
        path = _coverage_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        logging.getLogger(__name__).warning(
            "could not persist align-language coverage for %s", project,
            exc_info=True)
        return
    _last_write_at[project] = now
    _dirty_projects.discard(project)


def _flush_all() -> None:
    """atexit hook: a debounced write still pending when the process
    exits must not be lost."""
    for project in list(_dirty_projects):
        _write_coverage(project, force=True)


atexit.register(_flush_all)


def record_coverage(project: str, path: str) -> None:
    """One hit against ``path`` for ``project``: increments its count and
    stamps last_seen. Called by the ste.on_apply listener below; exposed
    at module level so a test can also call it directly."""
    data = _load_coverage(project)
    entry = dict(data.get(path) or {"count": 0, "last_seen": ""})
    entry["count"] = int(entry.get("count", 0)) + 1
    entry["last_seen"] = _now_iso()
    data[path] = entry
    _write_coverage(project)


def coverage(project: str) -> list[dict]:
    """{"path", "count", "last_seen", "known"} rows for ``project``,
    sorted by path. ``known`` is False for an "unknown:<module>" path --
    a real ingestion path this registry has not been taught to name yet."""
    data = _load_coverage(project)
    rows = [
        {
            "path": path,
            "count": entry.get("count", 0),
            "last_seen": entry.get("last_seen", ""),
            "known": not path.startswith("unknown:"),
        }
        for path, entry in data.items()
    ]
    rows.sort(key=lambda row: row["path"])
    return rows


def _on_ste_apply(mode: str, frames: list[tuple[str, str, str, str]]) -> None:
    """The ste.on_apply listener this module registers at import (below).
    Never raises past ste -- ste already wraps every listener call in its
    own try/except, this is a second, redundant belt for the same reason.
    """
    del mode  # path classification does not depend on strict/flavored
    try:
        project = _project_from_frames(frames)
        label = _path_label(frames)
        record_coverage(project, label)
    except Exception:
        logging.getLogger(__name__).warning(
            "align-language coverage listener failed", exc_info=True)


from prism_service.services import ste as _ste  # noqa: E402

_ste.on_apply(_on_ste_apply)
