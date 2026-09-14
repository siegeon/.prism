"""The code graph covers every service file (task 4b15f4bc).

Measured live against the "prism" project's own brain.db (2026-09-13):
only 8 distinct .py files were indexed out of 249 real files under
services/prism-service, and services/lexicon.py (added 2026-08-26) and
services/arc_governance.py (added 2026-06-12 -- long before this Brain
existed) had ZERO docs. Root cause, confirmed at the selection-logic
level below: `Brain.incremental_reindex` -- the ONLY mechanism that runs
on a recurring cadence (services/drift_worker.py) and the ONLY mechanism
`POST /api/brain/reindex` used to call -- candidates on `git diff HEAD`
+ `git ls-files --others` ONLY (brain_engine.py's incremental_reindex).
That is deliberate and correctly tested elsewhere
(test_brain_incremental_reindex_scopes_to_repo_path.py explicitly asserts
a freshly-committed, never-before-seen file produces ZERO reindex
activity) -- so the fix here does NOT touch incremental_reindex. Instead
/api/brain/reindex now calls source_service.ingest_source_to_brain, the
FULL walker (previously only ever run once, at project-configure time,
via bootstrap_after_clone) -- so a project that wants real coverage has
an endpoint that actually provides it.

Pollution half of the same task: two paths that got into Brain despite
being useless for retrieval --
  1. `.playwright-mcp/page-*.yml` browser-page dumps, indexed via a
     direct `brain_index_doc` MCP call because `.playwright-mcp` was
     never in source_service._INGEST_SKIP_DIRS.
  2. Empty-content docs -- a doc with no content can never answer a
     query and only consumes a retrieval slot.

NOT fixed here, deliberately: `__version__.py`. Investigation found this
is NOT a coverage/pollution bug -- task f9e0745e already solved "one
changelog module dominates every search" by capping only its oversized
`::__module__` chunk to 4KB while the sliding-window tier keeps covering
the full file (see test_explore_indexes_source_not_bundles.py's
test_module_chunk_is_capped_to_4kb_and_windows_still_cover_the_file, and
Brain._MODULE_CHUNK_CAP_BYTES). Live measurement confirms it: exactly 1
module doc at 4092 bytes (under the cap) + 459 window docs covering the
878KB file end to end -- working exactly as designed. Excluding the
file outright would delete real, deliberately-preserved content per that
earlier decision, so this file pins the OPPOSITE: that a changelog-style
module is still selected, not silently dropped by a future blanket
filename exclusion.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _committed_repo(tmp_path: Path, name: str) -> Path:
    """A repo whose files are ALL committed before any Brain ever looks
    at it -- the exact shape of services/arc_governance.py: real source
    that predates this project's Brain and has never been dirty since."""
    repo = tmp_path / name
    (repo / "prism_service" / "services").mkdir(parents=True)
    (repo / "prism_service" / "services" / "lexicon.py").write_text(
        "def load_lexicon():\n    return {}\n"
    )
    (repo / "prism_service" / "__version__.py").write_text(
        'PRISM_VERSION = "1.0.0"\n'
        'PRISM_VERSION_NOTES = "\\n".join(\n'
        '    f"entry {i} explains a real fix" for i in range(200)\n'
        ')\n'
    )
    (repo / ".playwright-mcp").mkdir()
    (repo / ".playwright-mcp" / "page-1.yml").write_text(
        "- role: button\n  name: Submit\n"
    )
    (repo / "prism_service" / "blank.py").write_text("")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _project(tmp_path: Path, repo: Path) -> str:
    from prism_service.services import source_service as ss

    pid = f"cov-{uuid.uuid4().hex[:8]}"
    ss.set_source_path(pid, str(repo))
    return pid


# ---------------------------------------------------------------------
# 1. Coverage: a never-touched, fully-committed file is selected by the
#    FULL walker, even though the diff-only mechanism would miss it.
# ---------------------------------------------------------------------

def test_incremental_reindex_alone_misses_a_never_touched_committed_file(tmp_path):
    """Pins the ROOT CAUSE at the selection-logic level: a fully-
    committed file with no working-tree drift is NOT a candidate for
    Brain.incremental_reindex, by design (see
    test_brain_incremental_reindex_scopes_to_repo_path.py). This is the
    reason services/arc_governance.py never got indexed -- it was never
    dirty during a drift sweep."""
    from prism_service.engines.brain_engine import Brain

    repo = _committed_repo(tmp_path, "repo-diag")
    d = tmp_path / "data-diag"
    d.mkdir()
    brain = Brain(
        brain_db=str(d / "brain.db"), graph_db=str(d / "graph.db"),
        scores_db=str(d / "scores.db"), tasks_db=str(d / "tasks.db"),
    )
    n = brain.incremental_reindex(repo_path=str(repo))
    assert n == 0, (
        "a fully-committed repo with no working-tree drift must not be "
        "a candidate for the diff-only mechanism -- if this fails, "
        "incremental_reindex's selection logic changed and the tests "
        "in test_brain_incremental_reindex_scopes_to_repo_path.py need "
        "re-reading before touching this file further"
    )


def test_full_reindex_covers_a_never_touched_committed_file(tmp_path):
    """The actual fix: source_service.ingest_source_to_brain (the full
    walker) selects lexicon.py even though it was committed once and
    never touched again -- unlike incremental_reindex above."""
    from prism_service.services import source_service as ss
    from prism_service.project_context import get_project

    repo = _committed_repo(tmp_path, "repo-full")
    pid = _project(tmp_path, repo)

    result = ss.ingest_source_to_brain(pid)
    assert result["ingested"] >= 1, result

    ctx = get_project(pid)
    conn = ctx.brain_svc._brain._brain
    covered = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT source_file FROM docs WHERE source_file IS NOT NULL"
        ).fetchall()
    }
    assert "prism_service/services/lexicon.py" in covered, covered


def test_reindex_endpoint_calls_the_full_walker_not_diff_only(tmp_path, monkeypatch):
    """The route the owner would actually press (`POST /api/brain/
    reindex`) must drive coverage from the full walker, not silently
    stay wired to the diff-only mechanism it used before this fix."""
    from prism_service.api import brain as brain_api

    calls: list[str] = []

    def _fake_ingest(project: str, **kw):
        calls.append(project)
        return {"ingested": 3, "skipped": 0, "rebuilt": False,
                "rebuild_error": "", "purged": {}}

    monkeypatch.setattr(brain_api.ss, "ingest_source_to_brain", _fake_ingest)
    out = brain_api.reindex(project="whatever-project")
    assert calls == ["whatever-project"], (
        "reindex() must call source_service.ingest_source_to_brain, the "
        "full walker -- not Brain.incremental_reindex, the diff-only path"
    )
    assert out["reindexed"] == 3


# ---------------------------------------------------------------------
# 2. Pollution: .playwright-mcp dumps and empty-content docs are never
#    selected, without dropping any real source.
# ---------------------------------------------------------------------

def test_playwright_mcp_dumps_are_excluded(tmp_path):
    from prism_service.engines.brain_engine import Brain
    from prism_service.services.source_service import is_ingest_excluded

    assert is_ingest_excluded(".playwright-mcp/page-1.yml")
    assert is_ingest_excluded("some/nested/.playwright-mcp/page-2.yml")

    b = Brain(
        brain_db=str(tmp_path / "brain.db"), graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    assert not b._should_index(".playwright-mcp/page-1.yml")
    # a real .yml file elsewhere in the tree must still be indexed
    assert b._should_index("services/prism-service/config.yml")


def test_full_reindex_never_ingests_playwright_mcp_dumps(tmp_path):
    from prism_service.services import source_service as ss
    from prism_service.project_context import get_project

    repo = _committed_repo(tmp_path, "repo-pw")
    pid = _project(tmp_path, repo)
    ss.ingest_source_to_brain(pid)

    ctx = get_project(pid)
    conn = ctx.brain_svc._brain._brain
    covered = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT source_file FROM docs WHERE source_file IS NOT NULL"
        ).fetchall()
    }
    assert not any(".playwright-mcp" in f for f in covered), covered


def test_empty_content_is_never_indexed(tmp_path):
    from prism_service.project_context import get_project

    pid = f"empty-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    doc_id = ctx.brain_svc.index_doc(
        path="prism_service/blank.py", content="", domain="code",
    )
    assert doc_id == "", (
        "index_doc must return an empty sentinel (never a real doc_id) "
        "when content is blank -- empty content can never answer a query"
    )
    conn = ctx.brain_svc._brain._brain
    rows = conn.execute(
        "SELECT COUNT(*) FROM docs WHERE source_file = ?",
        ("prism_service/blank.py",),
    ).fetchone()
    assert rows[0] == 0


def test_whitespace_only_content_is_never_indexed(tmp_path):
    from prism_service.project_context import get_project

    pid = f"empty-ws-{uuid.uuid4().hex[:8]}"
    ctx = get_project(pid)
    doc_id = ctx.brain_svc.index_doc(
        path="prism_service/blank2.py", content="   \n\n  \t\n", domain="code",
    )
    assert doc_id == ""


def test_index_files_skips_a_file_that_reads_as_blank(tmp_path):
    """Brain._index_files (the write path incremental_reindex uses) must
    not create a doc for a candidate file whose current content is
    blank -- distinct code path from BrainService.index_doc above."""
    from prism_service.engines.brain_engine import Brain

    blank = tmp_path / "blank.py"
    blank.write_text("")

    b = Brain(
        brain_db=str(tmp_path / "brain.db"), graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    b._index_files([str(blank)])
    rows = b._brain.execute(
        "SELECT COUNT(*) FROM docs WHERE source_file = ?", (str(blank),),
    ).fetchone()
    assert rows[0] == 0


# ---------------------------------------------------------------------
# 3. Regression guard: the changelog-style module stays selected. Not a
#    bug -- task f9e0745e's cap-the-module-chunk design deliberately
#    keeps it searchable via the sliding-window tier. A future blanket
#    filename exclusion would silently reverse that and lose content.
# ---------------------------------------------------------------------

def test_changelog_style_module_is_not_excluded(tmp_path):
    from prism_service.engines.brain_engine import Brain
    from prism_service.services.source_service import is_ingest_excluded

    assert not is_ingest_excluded(
        "services/prism-service/prism_service/__version__.py"
    )
    b = Brain(
        brain_db=str(tmp_path / "brain.db"), graph_db=str(tmp_path / "graph.db"),
        scores_db=str(tmp_path / "scores.db"),
    )
    assert b._should_index(
        "services/prism-service/prism_service/__version__.py"
    )


def test_full_reindex_still_covers_the_changelog_module(tmp_path):
    from prism_service.services import source_service as ss
    from prism_service.project_context import get_project

    repo = _committed_repo(tmp_path, "repo-changelog")
    pid = _project(tmp_path, repo)
    ss.ingest_source_to_brain(pid)

    ctx = get_project(pid)
    conn = ctx.brain_svc._brain._brain
    covered = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT source_file FROM docs WHERE source_file IS NOT NULL"
        ).fetchall()
    }
    assert "prism_service/__version__.py" in covered, covered
