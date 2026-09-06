"""Paths and utilities for the vendored archify renderer."""

import os
import shutil
from pathlib import Path

# Vendored archify directory
ARCHIFY_DIR = Path(__file__).parent / "archify"

# Path to the archify CLI binary
ARCHIFY_BIN = ARCHIFY_DIR / "bin" / "archify.mjs"


def node_executable() -> str:
    """
    Return the path to the node executable.

    Checks in order:
    1. PRISM_NODE environment variable
    2. System PATH (via shutil.which)
    3. Falls back to "node" (will fail at runtime if not in PATH)
    """
    # Check environment variable
    if env_node := os.environ.get("PRISM_NODE"):
        return env_node

    # Check system PATH
    if which_node := shutil.which("node"):
        return which_node

    # Fall back to plain "node"
    return "node"
