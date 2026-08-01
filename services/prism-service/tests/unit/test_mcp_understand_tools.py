"""Unit tests for the understand_* MCP tool surface (T9).

Drives `understand_tools.dispatch(...)` directly with mocked engine
state. Covers:

  * Tool registration: the four surviving tools, none in interactive
  * Refresh path (no_source / queued / complete)
  * Drain → store_result → cache + queue completion round trip

Task 4899173a retired the bootstrap / configure / get_* half of this surface
(superseded by the okf_* Understand wiki), so the dispatch tests for those
are gone; their absence is pinned by
test_retired_understand_tools_are_gone.py.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from prism_service import config
from prism_service.engines import understand_engine as ue
from prism_service.mcp import understand_tools as ut
from prism_service.services import source_service as ss
from prism_service.services import understand_artifact_store as store


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    ss._LOCKS.clear()
    return tmp_path / "projects"


@pytest.fixture
def cloned_proj_with_sha(tmp_path: Path, isolated_projects_root) -> str:
    up = tmp_path / "u"; up.mkdir()
    _git(up, "init", "-q", "--initial-branch=main")
    _git(up, "config", "user.email", "x@x"); _git(up, "config", "user.name", "x")
    (up / "main.py").write_text("hi\n", encoding="utf-8")
    _git(up, "add", "-A"); _git(up, "commit", "-q", "-m", "first")
    bare = tmp_path / "u.git"
    _git(up, "clone", "-q", "--bare", str(up), str(bare))

    ss.ensure_cloned("proj-a", str(bare))
    return ss.current_sha("proj-a")


def _call(name: str, **args) -> dict:
    """Drive dispatch and return the parsed envelope (data + meta)."""
    args.setdefault("project", "proj-a")
    out = ut.dispatch(name, args, default_project="proj-a")
    assert out is not None
    return json.loads(out[0].text)


def test_all_tools_registered():
    # Task 4899173a retired the other six (bootstrap, configure, get_tour,
    # get_layers, get_domains, get_onboarding) — the v5.1 analysis queue's
    # cold-start and artifact-read half, SUPERSEDED BY the okf_* Understand
    # wiki (okf_index / okf_get / okf_graph). The four below are load-bearing
    # and stay. Absence of the six is pinned by
    # test_retired_understand_tools_are_gone.py.
    expected = {
        "understand_refresh", "understand_status", "understand_drain_queue",
        "understand_store_result",
    }
    assert ut.UNDERSTAND_TOOL_NAMES == expected


def test_understand_tools_excluded_from_interactive_but_still_registered():
    """Legacy understand_* tools are demoted out of the default interactive
    surface (superseded by okf_*; kept for the tool-surface-reduction
    objective) yet remain registered, so they stay reachable via
    tool_profile=all and the automation profile's stop-hook entries."""
    from prism_service.mcp import tools as t
    assert ut.UNDERSTAND_TOOL_NAMES.isdisjoint(t.INTERACTIVE_TOOL_NAMES)
    registered = {tool.name for tool in t.TOOLS}
    assert ut.UNDERSTAND_TOOL_NAMES <= registered
    # automation profile still serves the two the stop hook calls.
    assert {"understand_refresh", "understand_status"} <= t.AUTOMATION_TOOL_NAMES


def test_dispatch_returns_none_for_foreign_tool():
    assert ut.dispatch("brain_search", {}, "proj-a") is None


def test_refresh_no_source(isolated_projects_root):
    env = _call("understand_refresh")
    assert env["data"]["status"] == "no_source"


def test_refresh_queued_on_cold_start(cloned_proj_with_sha):
    env = _call("understand_refresh")
    assert env["data"]["status"] == "queued"
    assert env["data"]["enqueued"] == 4
    assert env["meta"]["sha"] == cloned_proj_with_sha


def test_drain_then_store_result_round_trip(cloned_proj_with_sha):
    sha = cloned_proj_with_sha
    _call("understand_refresh")
    drained = _call("understand_drain_queue", max_jobs=2)
    assert drained["data"]["count"] == 2
    job = drained["data"]["jobs"][0]

    env = _call(
        "understand_store_result",
        job_id=job["job_id"],
        analyzer=job["analyzer"],
        target_sha=sha,
        payload={"steps": []} if job["analyzer"] != "onboarding_writer" else "# md\n",
        tokens_used=42,
    )
    assert env["data"]["stored"] is True
    assert store.get("proj-a", sha, job["analyzer"]) is not None


def test_status_reports_queue_and_sha(cloned_proj_with_sha):
    _call("understand_refresh")
    env = _call("understand_status")
    assert env["data"]["queue"]["pending"] == 4
    assert env["data"]["current_sha"] == cloned_proj_with_sha


# RETIRED (task 4899173a): the bootstrap / configure / get_tour / get_layers /
# get_domains / get_onboarding dispatch tests were removed together with the
# tools they exercised. SUPERSEDED BY the okf_* Understand wiki (okf_index /
# okf_get / okf_graph); their absence is now pinned by
# test_retired_understand_tools_are_gone.py. The refresh / status /
# drain_queue / store_result tests above are unaffected and still run.
