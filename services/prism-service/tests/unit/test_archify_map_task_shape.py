"""The task map must stay inside the workflow schema's own limits.

Two defects this pins, both found by rendering real tasks rather than a
fixture: a lane longer than the step lane ran past the last legal column, and
a concept title wider than its node broke the render. Both produced a task
whose map could never be built, and neither showed up in a shape-only test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from prism_service.services.archify_maps import task as taskmap
from prism_service.vendor.archify_paths import ARCHIFY_BIN, ARCHIFY_DIR, node_executable


class _Task:
    id = "ce471e06-bbbb-4db6-8ac9-2eb1b9f287c8"
    title = "A very long task title that would overflow any node box on the map"
    status = "in_progress"
    workflow_step = "implement_tasks"
    workflow = "implement"


class _Child:
    def __init__(self, i):
        self.id = f"child-{i:04d}"
        self.title = "A child task whose title is also far too long to fit"
        self.status = "pending"


class _TaskSvc:
    def get(self, task_id):
        return _Task() if task_id == _Task.id else None

    def list(self, parent_id=None):
        return [_Child(i) for i in range(9)]  # more children than legal columns


def _concepts(n):
    return [
        {"id": f"mx-{i:06x}",
         "title": "project-adjudicator-owns-every-gate-and-then-some",
         "type": "decision",
         "domain": "architecture-principles-with-a-long-name"}
        for i in range(n)
    ]


@pytest.fixture()
def ir(monkeypatch):
    import prism_service.services.archify_maps.task as mod

    class _Ctx:
        task_svc = _TaskSvc()
        memory_svc = object()

    monkeypatch.setattr(mod, "get_project", lambda p: _Ctx())
    monkeypatch.setattr(mod, "OkfHost", lambda *a, **k: type(
        "H", (), {"task_concepts": lambda self, tid: _concepts(9)})())
    return mod.build("prism", task_id=_Task.id)


def test_no_lane_runs_past_the_last_legal_column(ir):
    for node in ir["nodes"]:
        assert node["col"] <= 5, f"{node['id']} sits at column {node['col']}"


def test_every_edge_endpoint_is_a_node_that_exists(ir):
    ids = {n["id"] for n in ir["nodes"]}
    for edge in ir["edges"]:
        assert edge["from"] in ids, edge
        assert edge["to"] in ids, edge


def test_a_label_fits_inside_its_node(ir):
    """Archify refuses a label wider than the box that holds it."""
    for node in ir["nodes"]:
        assert len(node["label"]) <= 16, node


def test_a_missing_task_id_is_refused():
    with pytest.raises(ValueError):
        taskmap.build("prism", task_id=None)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
def test_the_task_map_renders(ir):
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump(ir, fh)
        path = fh.name
    try:
        out = subprocess.run(
            [node_executable(), str(ARCHIFY_BIN), "validate", "workflow",
             path, "--json"],
            capture_output=True, text=True, timeout=180, cwd=str(ARCHIFY_DIR),
        )
        assert json.loads(out.stdout).get("ok") is True, out.stdout[:900]
    finally:
        Path(path).unlink(missing_ok=True)
