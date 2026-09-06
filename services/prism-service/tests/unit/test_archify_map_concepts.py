"""Tests for the PRISM concept map builder."""

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from prism_service.services.archify_maps import build_ir


class TestConceptMapBuilder:
    """Build and validate the concept map IR."""

    def test_build_concepts_map(self):
        """Build a valid concept map from the default project."""
        diagram_type, ir = build_ir("prism", "concepts")
        assert diagram_type == "architecture"
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert ir["meta"]["title"]
        assert "visual_preset" in ir["meta"]
        assert ir["layout"]["mode"] == "grid"

        # Must have components and layout info
        components = ir.get("components", [])
        assert components, "concepts map must have components"
        assert all("id" in c and "type" in c and "label" in c for c in components)

        # All component ids must be slugged archify ids
        for comp in components:
            assert comp["id"][0].isalpha() or comp["id"].startswith("n-")
            assert "row" in comp and "col" in comp

        # Validate connections
        connections = ir.get("connections", [])
        kept_ids = {c["id"] for c in components}
        for conn in connections:
            assert conn["from"] in kept_ids, f"from {conn['from']} not in components"
            assert conn["to"] in kept_ids, f"to {conn['to']} not in components"
            assert conn["from"] != conn["to"], "no self-loops"

    def test_concept_map_validates(self):
        """The rendered map passes archify validation."""
        diagram_type, ir = build_ir("prism", "concepts")

        # Write IR to temp file and validate
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(ir, f)
            ir_path = f.name

        try:
            # Check for archify CLI
            cli_path = Path(__file__).parent.parent.parent / "vendor" / "archify" / "bin" / "archify.mjs"
            if not cli_path.exists():
                pytest.skip("archify CLI not found; skipping validation")

            result = subprocess.run(
                ["node", str(cli_path), "validate", "architecture", ir_path, "--json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = json.loads(result.stdout)
            assert output.get("ok"), f"validation failed: {output.get('diagnostics')}"
        finally:
            Path(ir_path).unlink()

    def test_empty_store_degrades(self):
        """When memory is empty, return a valid empty diagram."""
        # This test would need a mocked empty project; for now just check
        # the empty_diagram shape is valid if called directly.
        from prism_service.services.archify_maps.concepts import _empty_diagram
        ir = _empty_diagram("test")
        assert ir["schema_version"] == 1
        assert ir["diagram_type"] == "architecture"
        assert ir["components"]
        assert any(c["id"] == "empty" for c in ir["components"])

    def test_concept_ids_are_slugged(self):
        """Component ids must be valid archify ids (slugged from concept ids)."""
        diagram_type, ir = build_ir("prism", "concepts")
        for comp in ir.get("components", []):
            cid = comp["id"]
            assert cid, "component id must not be empty"
            # Must match ^[a-zA-Z][a-zA-Z0-9_-]*$ or n-<rest>
            import re
            assert re.match(r"^[a-zA-Z][a-zA-Z0-9_-]*$", cid), f"id {cid} is not a valid slug"
