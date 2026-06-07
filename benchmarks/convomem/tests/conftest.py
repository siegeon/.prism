"""Ensure the ConvoMem harness `run` module resolves to benchmarks/convomem/run.py.

See benchmarks/locomo/tests/conftest.py — same sys.modules['run'] collision
guard, pinned to this package's directory.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_CONVOMEM = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_run_module():
    sys.modules.pop("run", None)
    p = str(_CONVOMEM)
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)
    yield
    sys.modules.pop("run", None)
