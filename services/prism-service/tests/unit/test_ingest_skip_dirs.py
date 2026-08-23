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
                 "node_modules", ".pytest_cache", "web_dist", "web_dist_next"):
        assert name in _INGEST_SKIP_DIRS, f"{name!r} missing from skip set"


def test_skip_predicate_skips_web_dist_next(tmp_path):
    """brain-bloat incident, 2026-08-22 — `web_dist` was renamed
    `web_dist_next` and the skip list was never updated, so ingest walked
    the built (hashed-filename) Vite bundle straight into Brain as code:
    ~20GB across projects. Pin both build-output dirs as skipped."""
    from prism_service.services.source_service import _INGEST_SKIP_DIRS

    poisoned = [
        Path("services/prism-service/prism_service/web_dist_next/"
             "assets/index-BpjKQqBl.js"),
        Path("services/prism-service/prism_service/web_dist/"
             "assets/index-abc123.js"),
    ]
    for p in poisoned:
        assert any(part in _INGEST_SKIP_DIRS for part in p.parts), p


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


def test_is_ingest_excluded_matches_the_shared_skip_set():
    """is_ingest_excluded() (brain-bloat incident follow-up, 2026-08-22)
    is the ONE shared check every doc-adding caller must use -- pin it
    against both a build artifact and a real source file, and accept a
    bare string path (not just a Path), since brain_index_doc's `path`
    argument arrives as a plain str."""
    from prism_service.services.source_service import is_ingest_excluded

    assert is_ingest_excluded(
        "services/prism-service/prism_service/web_dist_next/assets/index-BpjKQqBl.js")
    assert is_ingest_excluded("E:/.prism/.venvs/dev/Lib/site-packages/torch/__init__.py")
    assert not is_ingest_excluded("services/prism-service/prism_service/api/learning.py")


def test_brain_index_doc_mcp_tool_refuses_an_excluded_path():
    """The walker (ingest_source_to_brain) got the web_dist_next skip-list
    fix, but brain_index_doc -- the MCP tool an agent calls directly on
    any path it read -- had NO equivalent guard, so an agent indexing a
    build artifact by hand re-introduced the exact bloat live while that
    fix was being validated (2026-08-22). Source-reading pin: the tool's
    dispatch branch must call is_ingest_excluded before brain_svc.index_doc."""
    src = (_SERVICE_ROOT / "prism_service" / "mcp" / "tools.py").read_text(encoding="utf-8")
    i = src.index('if name == "brain_index_doc":')
    end = src.index("if name ==", i + 1)
    branch = src[i:end]
    assert "is_ingest_excluded" in branch, (
        "brain_index_doc must reuse the shared ingest skip-check, or an "
        "agent calling it directly bypasses the walker's exclusion list")
    idx_call = branch.index("brain_svc.index_doc(")
    guard_call = branch.index("is_ingest_excluded(")
    assert guard_call < idx_call, (
        "the exclusion check must run BEFORE index_doc, not after")
