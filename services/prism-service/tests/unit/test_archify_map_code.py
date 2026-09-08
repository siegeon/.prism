"""Tests for code architecture map builder."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from prism_service.services.archify_maps import build_ir
from prism_service.vendor.archify_paths import ARCHIFY_BIN, node_executable


class _ChapteredGraph:
    """Ten modules spread across every real chapter role (entry, core,
    data, web, support). tests/conftest.py pins PRISM_DATA_DIR to a fresh
    throwaway dir for the whole suite, so `build_ir("prism", "code")`
    against the real project sees an empty graph.db and degrades to the
    single-box empty diagram -- this fixture supplies real multi-module
    data the same way test_code_map_is_built_from_code.py's _FakeGraph
    does, so the chapter/label contract has something real to check."""

    def file_graph(self) -> dict:
        files = [
            {"file": "app/api/routes.py", "entities": 20},
            {"file": "app/mcp/tools.py", "entities": 10},
            {"file": "app/services/conductor.py", "entities": 30},
            {"file": "app/engines/brain.py", "entities": 15},
            {"file": "app/memory_ops/forget.py", "entities": 8},
            {"file": "app/inference/queue.py", "entities": 6},
            {"file": "app/web/pages/Home.tsx", "entities": 12},
            {"file": "app/web/lib/api.ts", "entities": 5},
            {"file": "app/scripts/build.py", "entities": 4},
            {"file": "app/assets/hook.py", "entities": 3},
        ]
        edges = [
            {"from": "app/services/conductor.py", "to": "app/api/routes.py",
             "weight": 10},
            {"from": "app/mcp/tools.py", "to": "app/api/routes.py",
             "weight": 4},
            {"from": "app/web/pages/Home.tsx", "to": "app/web/lib/api.ts",
             "weight": 2},
        ]
        return {"files": files, "edges": edges}


class TestCodeMapBuilder:
    """Code architecture map builder tests."""

    def test_build_ir_returns_architecture_diagram(self):
        """build_ir returns diagram_type and valid IR dict."""
        diagram_type, ir = build_ir("prism", "code")
        assert diagram_type == "architecture"
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert "meta" in ir
        assert "layout" in ir
        assert "components" in ir

    def test_meta_has_required_fields(self):
        """Meta object has all required fields."""
        _, ir = build_ir("prism", "code")
        meta = ir["meta"]
        assert "title" in meta
        # SUPERSEDED (task 07afdaf2): `visual_preset` used to be pinned to
        # "blueprint" here. SKILL.md says to omit it by default so the
        # diagram opens in `classic`; the code map now omits the key
        # entirely rather than setting it.
        assert "visual_preset" not in meta
        assert meta.get("animation") == "none"

    def test_views_are_curated_chapters_not_one_all_inclusive_list(self, monkeypatch):
        """meta.views drives the Named Chapter Rail, Chapter Delta Preview,
        Story Beat Navigator, Follow Camera, Director Strip, Story Horizon,
        and shareable story-moment links (viewer-runtime.md). One chapter
        whose focus lists every component makes all of that inert, so the
        map must carry 3 to 5 real chapters, and none may be the full set."""
        monkeypatch.setattr(
            "prism_service.services.archify_maps.code.get_project",
            lambda project: type("C", (), {"graph_svc": _ChapteredGraph()})(),
        )
        _, ir = build_ir("prism", "code")
        views = ir["meta"]["views"]
        assert 3 <= len(views) <= 5, views
        comp_ids = {c["id"] for c in ir["components"]}
        for view in views:
            assert view["focus"], view
            assert set(view["focus"]) != comp_ids, (
                f"chapter {view['id']!r} lists every component"
            )

    def test_connections_carry_a_semantic_label(self, monkeypatch):
        """SKILL.md: relationship labels are semantic data. A bare
        {from,to} pair with no label is decoration-only and was the shape
        this task replaced."""
        monkeypatch.setattr(
            "prism_service.services.archify_maps.code.get_project",
            lambda project: type("C", (), {"graph_svc": _ChapteredGraph()})(),
        )
        _, ir = build_ir("prism", "code")
        connections = ir.get("connections", [])
        assert connections, "no connections to assert a label on"
        for conn in connections:
            assert conn.get("label"), conn

    def test_layout_grid_mode(self):
        """Layout is in grid mode with proper config."""
        _, ir = build_ir("prism", "code")
        layout = ir["layout"]
        assert layout["mode"] == "grid"
        assert "cols" in layout
        assert layout["cols"] <= 12
        assert "cellW" in layout
        assert "cellH" in layout

    def test_components_have_required_fields(self):
        """Every component has id, type, label, and grid placement."""
        _, ir = build_ir("prism", "code")
        for comp in ir.get("components", []):
            assert "id" in comp
            assert "type" in comp
            assert "label" in comp
            assert comp["type"] in {
                "frontend",
                "backend",
                "database",
                "messagebus",
                "security",
                "external",
                "cloud",
            }
            # Grid mode requires row/col
            assert "row" in comp or "pos" in comp
            assert "col" in comp or "size" in comp

    def test_components_labels_clipped(self):
        """Component labels are at most 28 chars."""
        _, ir = build_ir("prism", "code")
        for comp in ir.get("components", []):
            assert len(comp["label"]) <= 28

    def test_components_sublabels_clipped(self):
        """Component sublabels are at most 40 chars."""
        _, ir = build_ir("prism", "code")
        for comp in ir.get("components", []):
            if "sublabel" in comp:
                assert len(comp["sublabel"]) <= 40

    def test_components_capped_at_40(self):
        """No more than 40 components."""
        _, ir = build_ir("prism", "code")
        assert len(ir.get("components", [])) <= 40

    def test_connections_endpoints_exist(self):
        """Every connection endpoint exists in components."""
        _, ir = build_ir("prism", "code")
        comp_ids = {c["id"] for c in ir.get("components", [])}
        for conn in ir.get("connections", []):
            assert conn["from"] in comp_ids, f"from={conn['from']} not in components"
            assert conn["to"] in comp_ids, f"to={conn['to']} not in components"

    def test_connections_no_self_loops(self):
        """No self-loop connections."""
        _, ir = build_ir("prism", "code")
        for conn in ir.get("connections", []):
            assert conn["from"] != conn["to"]

    def test_connections_capped_at_80(self):
        """No more than 80 connections."""
        _, ir = build_ir("prism", "code")
        assert len(ir.get("connections", [])) <= 80

    def test_boundaries_wrap_existing_ids(self):
        """Every boundary wraps only existing component ids."""
        _, ir = build_ir("prism", "code")
        comp_ids = {c["id"] for c in ir.get("components", [])}
        for boundary in ir.get("boundaries", []):
            assert "kind" in boundary
            assert "label" in boundary
            assert "wraps" in boundary
            for wrapped_id in boundary["wraps"]:
                assert wrapped_id in comp_ids

    def test_cards_present(self):
        """Cards exist and have proper structure."""
        _, ir = build_ir("prism", "code")
        cards = ir.get("cards", [])
        assert len(cards) > 0
        for card in cards:
            assert "dot" in card
            assert card["dot"] in {
                "cyan",
                "emerald",
                "violet",
                "amber",
                "rose",
                "orange",
                "slate",
            }
            assert "title" in card
            assert "items" in card

    def test_ir_validates_with_archify(self):
        """IR passes archify validate."""
        if not node_executable() or not ARCHIFY_BIN.exists():
            pytest.skip("Node or archify not available")

        _, ir = build_ir("prism", "code")

        with tempfile.TemporaryDirectory() as tmpdir:
            ir_path = Path(tmpdir) / "code.ir.json"
            with open(ir_path, "w") as f:
                json.dump(ir, f)

            result = subprocess.run(
                [
                    str(node_executable()),
                    str(ARCHIFY_BIN),
                    "validate",
                    "architecture",
                    str(ir_path),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                output = result.stdout + result.stderr
                pytest.fail(f"Validation failed:\n{output}")

            try:
                receipt = json.loads(result.stdout)
                assert receipt.get("ok") is True, f"Validate receipt: {receipt}"
            except json.JSONDecodeError:
                pytest.fail(f"Invalid JSON from validate:\n{result.stdout}\n{result.stderr}")

    def test_ir_renders_to_html(self):
        """IR renders successfully via archify deliver."""
        if not node_executable() or not ARCHIFY_BIN.exists():
            pytest.skip("Node or archify not available")

        _, ir = build_ir("prism", "code")

        with tempfile.TemporaryDirectory() as tmpdir:
            ir_path = Path(tmpdir) / "code.ir.json"
            html_path = Path(tmpdir) / "code.html"

            with open(ir_path, "w") as f:
                json.dump(ir, f)

            result = subprocess.run(
                [
                    str(node_executable()),
                    str(ARCHIFY_BIN),
                    "deliver",
                    "architecture",
                    str(ir_path),
                    str(html_path),
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                output = result.stdout + result.stderr
                pytest.fail(f"Deliver failed:\n{output}")

            try:
                receipt = json.loads(result.stdout)
                assert receipt.get("ok") is True, f"Deliver receipt: {receipt}"
                assert html_path.exists()
            except json.JSONDecodeError:
                pytest.fail(f"Invalid JSON from deliver:\n{result.stdout}\n{result.stderr}")

    def test_empty_store_degrades_gracefully(self, monkeypatch):
        """When graph_svc fails, returns valid empty diagram."""
        # Mock get_project to return a graph_svc that raises
        def mock_get_project(project):
            raise RuntimeError("graph.db unavailable")

        monkeypatch.setattr(
            "prism_service.services.archify_maps.code.get_project",
            mock_get_project,
        )

        _, ir = build_ir("prism", "code")

        # Should still be a valid IR
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert len(ir.get("components", [])) >= 1
        assert ir["components"][0]["id"] == "empty"
