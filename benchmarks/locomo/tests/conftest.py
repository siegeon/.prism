"""Ensure the LoCoMo harness `run` module resolves to benchmarks/locomo/run.py.

Several benchmark dirs ship a top-level ``run.py`` (longmemeval, locomo,
convomem). Python caches the first ``import run`` in ``sys.modules``, so when
pytest collects multiple harness suites in one session a later suite would get
the wrong module. This autouse fixture evicts the cached ``run`` and pins this
package's dir on ``sys.path`` before each test re-imports it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_LOCOMO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolate_run_module():
    sys.modules.pop("run", None)
    p = str(_LOCOMO)
    if p in sys.path:
        sys.path.remove(p)
    sys.path.insert(0, p)
    yield
    sys.modules.pop("run", None)
