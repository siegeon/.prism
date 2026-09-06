"""Tests for vendored archify renderer."""

import shutil
import subprocess
import pytest
from pathlib import Path

from prism_service.vendor.archify_paths import ARCHIFY_BIN, node_executable


def test_archify_bin_exists():
    """Assert ARCHIFY_BIN path exists."""
    assert ARCHIFY_BIN.exists(), f"archify binary not found at {ARCHIFY_BIN}"


def test_archify_validate_example():
    """Test that archify can validate a vendored example JSON."""
    # Skip if node is not available
    if not shutil.which("node"):
        pytest.skip("node executable not found in PATH")

    node = node_executable()
    example_file = ARCHIFY_BIN.parent.parent / "examples" / "web-app.architecture.json"

    assert example_file.exists(), f"example file not found at {example_file}"

    # Run archify validate on the example
    result = subprocess.run(
        [node, str(ARCHIFY_BIN), "validate", "architecture", str(example_file), "--json"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, f"archify validate failed: {result.stderr}"
