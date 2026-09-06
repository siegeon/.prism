"""Archify map builder registry.

Each supported `kind` (code / concepts / language / task) maps to a builder
module exposing `DIAGRAM_TYPE: str` and `build(project, *, task_id=None) ->
dict` (the archify IR dict, not yet validated or rendered).
"""

from __future__ import annotations

from prism_service.services.archify_maps import code, concepts, language, task

BUILDERS: dict[str, object] = {
    "code": code,
    "concepts": concepts,
    "language": language,
    "task": task,
}


def build_ir(project: str, kind: str, task_id: str | None = None) -> tuple[str, dict]:
    """Look up the builder for `kind` and build its IR.

    Returns (diagram_type, ir). Raises KeyError for an unknown kind — the
    caller (ArchifyService / the API layer) turns that into a 400.
    """
    module = BUILDERS[kind]
    ir = module.build(project, task_id=task_id)
    return module.DIAGRAM_TYPE, ir
