"""Tests for archify task workflow map builder."""

from __future__ import annotations

import json
import pytest
from unittest.mock import Mock, MagicMock

from prism_service.services.archify_maps.task import build, DIAGRAM_TYPE


def test_diagram_type():
    """Verify the diagram type is workflow."""
    assert DIAGRAM_TYPE == "workflow"


def test_missing_task_id():
    """Raise ValueError when task_id is missing."""
    with pytest.raises(ValueError, match="task_id is required"):
        build("prism", task_id=None)


def test_task_not_found():
    """Raise ValueError when task does not exist."""
    mock_ctx = Mock()
    mock_ctx.task_svc.get.return_value = None

    # Monkey-patch get_project
    import prism_service.services.archify_maps.task as task_module
    original_get_project = task_module.get_project
    task_module.get_project = lambda p: mock_ctx

    try:
        with pytest.raises(ValueError, match="not found"):
            build("prism", task_id="unknown-task")
    finally:
        task_module.get_project = original_get_project


def test_ir_schema_validation():
    """IR structure has required fields and valid workflow v2 schema."""
    mock_task = Mock()
    mock_task.id = "task-123"
    mock_task.title = "Test task"
    mock_task.status = "pending"
    mock_task.workflow = "implement"
    mock_task.workflow_step = "draft_story"

    mock_ctx = Mock()
    mock_ctx.task_svc.get.return_value = mock_task
    mock_ctx.task_svc.list.return_value = []
    mock_ctx.memory_svc = Mock()

    import prism_service.services.archify_maps.task as task_module
    original_get_project = task_module.get_project
    original_okf = task_module.OkfHost

    task_module.get_project = lambda p: mock_ctx
    task_module.OkfHost = Mock(return_value=Mock(task_concepts=Mock(return_value=[])))

    try:
        ir = build("prism", task_id="task-123")

        # Schema v2, workflow diagram type
        assert ir["schema_version"] == 2
        assert ir["diagram_type"] == "workflow"

        # Required top-level fields
        assert "meta" in ir
        assert "lanes" in ir
        assert "nodes" in ir
        assert "edges" in ir

        # Meta fields
        assert ir["meta"]["title"]
        assert ir["meta"]["subtitle"]
        assert ir["meta"]["visual_preset"] == "blueprint"
        assert ir["meta"]["animation"] == "none"

        # Lanes
        assert len(ir["lanes"]) == 3
        lane_ids = [l["id"] for l in ir["lanes"]]
        assert "flow" in lane_ids
        assert "knowledge" in lane_ids
        assert "work" in lane_ids

        # mainPath and phases
        assert "mainPath" in ir
        assert isinstance(ir["mainPath"], list)
        assert "phases" in ir
        assert isinstance(ir["phases"], list)

        # Cards
        assert "cards" in ir
        assert len(ir["cards"]) >= 1
        for card in ir["cards"]:
            assert "dot" in card
            assert "title" in card
            assert "items" in card

    finally:
        task_module.get_project = original_get_project
        task_module.OkfHost = original_okf


def test_node_validity():
    """Nodes have valid id, type, label, and placement."""
    mock_task = Mock()
    mock_task.id = "task-123"
    mock_task.title = "Test task"
    mock_task.status = "pending"
    mock_task.workflow = "implement"
    mock_task.workflow_step = "draft_story"

    mock_ctx = Mock()
    mock_ctx.task_svc.get.return_value = mock_task
    mock_ctx.task_svc.list.return_value = []
    mock_ctx.memory_svc = Mock()

    import prism_service.services.archify_maps.task as task_module
    original_get_project = task_module.get_project
    original_okf = task_module.OkfHost

    task_module.get_project = lambda p: mock_ctx
    task_module.OkfHost = Mock(return_value=Mock(task_concepts=Mock(return_value=[])))

    try:
        ir = build("prism", task_id="task-123")

        # Check nodes
        assert len(ir["nodes"]) > 0
        node_ids = set()

        for node in ir["nodes"]:
            assert "id" in node
            assert "lane" in node
            assert "col" in node
            assert "type" in node
            assert "label" in node

            # ID validation: ^[a-zA-Z][a-zA-Z0-9_-]*$
            assert node["id"][0].isalpha(), f"Node id must start with letter: {node['id']}"
            node_ids.add(node["id"])

            # Type must be valid
            assert node["type"] in ["backend", "external", "cloud"], f"Invalid type: {node['type']}"

            # Label must be non-empty
            assert node["label"], f"Node {node['id']} has empty label"

            # Col must be non-negative integer
            assert isinstance(node["col"], int) and node["col"] >= 0

        # No duplicate ids
        assert len(node_ids) == len(ir["nodes"]), "Duplicate node ids found"

    finally:
        task_module.get_project = original_get_project
        task_module.OkfHost = original_okf


def test_edge_validity():
    """Edges reference existing nodes and have valid structure."""
    mock_task = Mock()
    mock_task.id = "task-123"
    mock_task.title = "Test task"
    mock_task.status = "pending"
    mock_task.workflow = "implement"
    mock_task.workflow_step = "draft_story"

    mock_ctx = Mock()
    mock_ctx.task_svc.get.return_value = mock_task
    mock_ctx.task_svc.list.return_value = []
    mock_ctx.memory_svc = Mock()

    import prism_service.services.archify_maps.task as task_module
    original_get_project = task_module.get_project
    original_okf = task_module.OkfHost

    task_module.get_project = lambda p: mock_ctx
    task_module.OkfHost = Mock(return_value=Mock(task_concepts=Mock(return_value=[])))

    try:
        ir = build("prism", task_id="task-123")

        node_ids = {n["id"] for n in ir["nodes"]}

        for edge in ir["edges"]:
            assert "from" in edge
            assert "to" in edge
            assert edge["from"] in node_ids, f"Edge from {edge['from']} not in nodes"
            assert edge["to"] in node_ids, f"Edge to {edge['to']} not in nodes"
            assert edge["from"] != edge["to"], "No self-loops allowed"

    finally:
        task_module.get_project = original_get_project
        task_module.OkfHost = original_okf


def test_with_concepts_and_children():
    """IR includes concepts and child tasks in lanes."""
    mock_task = Mock()
    mock_task.id = "task-123"
    mock_task.title = "Test task"
    mock_task.status = "pending"
    mock_task.workflow = "implement"
    mock_task.workflow_step = "draft_story"

    mock_child = Mock()
    mock_child.id = "child-456"
    mock_child.title = "Child task"
    mock_child.status = "pending"

    mock_ctx = Mock()
    mock_ctx.task_svc.get.return_value = mock_task
    mock_ctx.task_svc.list.return_value = [mock_child]
    mock_ctx.memory_svc = Mock()

    mock_concepts = [
        {"id": "concept-1", "title": "Concept One", "domain": "core", "recall_count": 2},
        {"id": "concept-2", "title": "Concept Two", "domain": "arch", "recall_count": 1},
    ]

    import prism_service.services.archify_maps.task as task_module
    original_get_project = task_module.get_project
    original_okf = task_module.OkfHost

    task_module.get_project = lambda p: mock_ctx
    task_module.OkfHost = Mock(return_value=Mock(task_concepts=Mock(return_value=mock_concepts)))

    try:
        ir = build("prism", task_id="task-123")

        # Check for concept nodes
        concept_nodes = [n for n in ir["nodes"] if n["lane"] == "knowledge"]
        assert len(concept_nodes) == 2

        # Check for child nodes
        work_nodes = [n for n in ir["nodes"] if n["lane"] == "work"]
        assert len(work_nodes) == 1

        # Check for edges from concepts to first step
        concept_to_step_edges = [
            e for e in ir["edges"]
            if any(n["id"] == e["from"] for n in concept_nodes)
        ]
        assert len(concept_to_step_edges) > 0

    finally:
        task_module.get_project = original_get_project
        task_module.OkfHost = original_okf
