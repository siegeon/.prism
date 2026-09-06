"""Tests for the language ontology map builder."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from prism_service.services.archify_maps import build_ir


class TestLanguageMapBuilder:
    """Test the language map IR generation and validation."""

    def test_build_ir_returns_valid_architecture_ir(self):
        """The build() function returns a valid architecture IR dict."""
        diagram_type, ir = build_ir("prism", "language")

        assert diagram_type == "architecture"
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert "meta" in ir
        assert "title" in ir["meta"]
        assert "layout" in ir
        assert "components" in ir

    def test_empty_graph_still_yields_valid_ir(self):
        """When the graph is empty, IR still validates."""
        diagram_type, ir = build_ir("_nonexistent", "language")

        assert diagram_type == "architecture"
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert len(ir.get("components", [])) >= 1  # At least the empty component

    def test_ir_structure_is_valid_for_archify(self):
        """The IR has the required structure for archify (schema version, type, meta)."""
        diagram_type, ir = build_ir("prism", "language")

        # Schema validation (not rendering)
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert "meta" in ir and "title" in ir["meta"]
        assert "layout" in ir
        assert ir["layout"]["mode"] == "grid"
        assert "components" in ir and len(ir["components"]) > 0

        # Component structure
        for comp in ir["components"]:
            assert "id" in comp and "type" in comp and "label" in comp
            assert "row" in comp and "col" in comp
            assert comp["type"] in ["frontend", "backend", "database", "cloud", "security", "messagebus", "external"]

    def test_ir_has_proper_layout(self):
        """Components have valid row/col for grid layout."""
        diagram_type, ir = build_ir("prism", "language")

        layout = ir.get("layout", {})
        assert layout.get("mode") == "grid"
        assert layout.get("cols") >= 1

        for comp in ir.get("components", []):
            if "row" in comp:
                assert isinstance(comp["row"], int)
                assert comp["row"] >= 0
            if "col" in comp:
                assert isinstance(comp["col"], int)
                assert comp["col"] >= 0

    def test_connections_reference_existing_components(self):
        """Every connection endpoint must exist as a component."""
        diagram_type, ir = build_ir("prism", "language")

        comp_ids = {c["id"] for c in ir.get("components", [])}
        for conn in ir.get("connections", []):
            assert conn["from"] in comp_ids, f"from {conn['from']} not in components"
            assert conn["to"] in comp_ids, f"to {conn['to']} not in components"

    def test_boundaries_wrap_existing_components(self):
        """Every boundary must wrap existing components."""
        diagram_type, ir = build_ir("prism", "language")

        comp_ids = {c["id"] for c in ir.get("components", [])}
        for boundary in ir.get("boundaries", []):
            for wrapped_id in boundary.get("wraps", []):
                assert wrapped_id in comp_ids, f"boundary wraps {wrapped_id} not in components"

    def test_no_self_loops(self):
        """No connection should loop to itself."""
        diagram_type, ir = build_ir("prism", "language")

        for conn in ir.get("connections", []):
            assert conn["from"] != conn["to"], f"self-loop: {conn['from']} -> {conn['to']}"

    def test_labels_are_clipped(self):
        """Labels respect archify size limits."""
        diagram_type, ir = build_ir("prism", "language")

        for comp in ir.get("components", []):
            label = comp.get("label", "")
            sublabel = comp.get("sublabel", "")
            assert len(label) <= 28, f"label too long: {label!r}"
            assert len(sublabel) <= 40, f"sublabel too long: {sublabel!r}"

        for conn in ir.get("connections", []):
            label = conn.get("label", "")
            assert len(label) <= 28, f"connection label too long: {label!r}"

    def test_cards_present_rules_summary(self):
        """When rules exist, cards surface passing/failing counts."""
        diagram_type, ir = build_ir("prism", "language")

        cards = ir.get("cards", [])
        assert len(cards) >= 1, "at least one card should exist"

        for card in cards:
            assert "dot" in card
            assert "title" in card
            assert "items" in card
            assert isinstance(card["items"], list)

    def test_component_count_within_limits(self):
        """Component and connection counts respect archify limits."""
        diagram_type, ir = build_ir("prism", "language")

        assert len(ir.get("components", [])) <= 40, "too many components"
        assert len(ir.get("connections", [])) <= 80, "too many connections"
