"""v6.0.19 — regression: `.venvs` (plural) must be on the ingest skip list.

The prism-dev skill creates the dev venv at `E:\\.prism\\.venvs\\dev\\…`
(plural, at the repo root). The previous skip list only had `.venv`
(singular), so a folder-mode prism project pointed at the repo root
ingested ~1900 site-packages .py files. graphify's own filter then
dropped them as site-packages-ish, leaving the staging dir effectively
empty and the rebuild button as a silent no-op.

Also covers `.dev-data` (the matching dev data dir) and the existing
worktree / claude / venv-singular entries so the set doesn't regress.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_skip_set_includes_venvs_and_dev_data():
    from prism_service.services.source_service import _INGEST_SKIP_DIRS
    for name in (".venv", ".venvs", ".dev-data", ".claude", "worktrees",
                 "node_modules", ".pytest_cache"):
        assert name in _INGEST_SKIP_DIRS, f"{name!r} missing from skip set"


def test_skip_predicate_skips_dev_venv_paths(tmp_path):
    """`any(part in _INGEST_SKIP_DIRS for part in path.parts)` is the
    same predicate ingest_source_to_brain uses — exercise it directly
    on the exact paths that poisoned dev."""
    from prism_service.services.source_service import _INGEST_SKIP_DIRS

    poisoned = [
        Path("E:/.prism/.venvs/dev/Lib/site-packages/torch/__init__.py"),
        Path("E:/.prism/.dev-data/projects/prism/source/foo.py"),
        Path(".claude/worktrees/branch/services/foo.py"),
    ]
    for p in poisoned:
        assert any(part in _INGEST_SKIP_DIRS for part in p.parts), p

    kept = [
        Path("services/prism-service/prism_service/__version__.py"),
        Path("services/prism-service/prism_service/api/learning.py"),
        Path("README.md"),
    ]
    for p in kept:
        assert not any(part in _INGEST_SKIP_DIRS for part in p.parts), p
