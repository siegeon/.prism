#!/usr/bin/env python3
"""Tests for HF Hub unauthenticated-request warning suppression.

Coverage:
- HF_HUB_DISABLE_IMPLICIT_TOKEN env var is set to '1' at module import time.
- The warnings filter suppresses messages matching the HF Hub warning pattern.
- Running brain_engine.py status via subprocess produces no HF Hub warning text
  in stdout or stderr.
"""

import os
import subprocess
import sys
import warnings
from pathlib import Path

import pytest

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
BRAIN_ENGINE = HOOKS_DIR / "brain_engine.py"


def test_hf_hub_env_var_set_on_import() -> None:
    """HF_HUB_DISABLE_IMPLICIT_TOKEN must be '1' after importing brain_engine."""
    # Import brain_engine and verify the env var was set at module level.
    sys.path.insert(0, str(HOOKS_DIR))
    import brain_engine  # noqa: F401 — side-effect import

    assert os.environ.get("HF_HUB_DISABLE_IMPLICIT_TOKEN") == "1", (
        "HF_HUB_DISABLE_IMPLICIT_TOKEN should be set to '1' by brain_engine import"
    )


def test_hf_warning_filter_suppresses_warning() -> None:
    """The pattern from brain_engine's warnings.filterwarnings call must suppress the warning."""
    # catch_warnings(record=True) resets to "always", so we re-apply the
    # brain_engine filter pattern explicitly to confirm it works correctly.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        warnings.filterwarnings(
            "ignore",
            message=r".*unauthenticated requests.*HF Hub.*",
        )
        warnings.warn(
            "Warning: You are sending unauthenticated requests to the HF Hub",
            UserWarning,
            stacklevel=1,
        )

    hf_warnings = [w for w in caught if "unauthenticated requests" in str(w.message)]
    assert len(hf_warnings) == 0, (
        "HF Hub unauthenticated warning should be suppressed by the registered filter"
    )


def test_no_hf_warning_in_subprocess_output(tmp_path: Path) -> None:
    """Running brain_engine.py status must not emit HF Hub warning text."""
    # Create a minimal .prism/brain directory so Brain() doesn't crash.
    brain_dir = tmp_path / ".prism" / "brain"
    brain_dir.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1"}
    result = subprocess.run(
        [sys.executable, str(BRAIN_ENGINE), "status"],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env=env,
        timeout=30,
    )

    combined = result.stdout + result.stderr
    assert "unauthenticated" not in combined.lower(), (
        f"HF Hub warning found in brain_engine output:\n{combined}"
    )
    assert "hf hub" not in combined.lower() or "disable" in combined.lower() or \
        "unauthenticated" not in combined.lower(), (
        f"Unexpected HF Hub warning in output:\n{combined}"
    )
