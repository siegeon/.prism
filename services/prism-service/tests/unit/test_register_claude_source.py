"""RED scaffold — register_claude_source MCP tool + claude_memory
persistence (task b6650506).

Pins the USER-FACING seam, not a dead-code unit contract:

  * register_claude_source is reachable through the MCP DISPATCHER
    (handle_tool / call_tool), not merely defined in TOOLS.
  * it is advertised in the INTERACTIVE tool profile.
  * it resolves resolve_claude_home()/projects/path_to_slug(cwd),
    honoring CLAUDE_CONFIG_DIR over Path.home().
  * it validates the dir exists + contains *.jsonl, returning the
    resolved dir and a jsonl count.
  * it PERSISTS claude_project_dir via the SAME writer the Settings
    editor uses (ue._write_state) — survives a separate read.
  * idempotent: re-registering a new dir updates the stored value.
  * claude_memory.configured_project_dir(project) reads the persisted
    value back so the poller override path actually resolves (today
    the module does not exist -> import fails -> cm=None -> override
    never fires).

All FAIL today: claude_memory.py is absent, register_claude_source is
not in TOOLS / INTERACTIVE_TOOL_NAMES, and nothing reads/writes the
claude_project_dir key.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service import config
from prism_service.engines import understand_engine as ue


@pytest.fixture
def isolated_projects(tmp_path, monkeypatch):
    """Point PROJECTS_DIR at tmp so understand_state.json writes are
    sandboxed; seed a project data dir."""
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    config.project_data_dir("proj-cc")  # seed the dir + skeleton state
    return tmp_path


@pytest.fixture
def claude_home_with_jsonl(tmp_path, monkeypatch):
    """A fake ~/.claude whose projects/<slug> holds two transcripts.
    The slug is deliberately UNRELATED to any PRISM source path so the
    auto-discovery (path_to_slug) miss is exercised."""
    home = tmp_path / "fake_claude"
    slug = "C--Users-someone-elsewhere"
    proj_dir = home / "projects" / slug
    proj_dir.mkdir(parents=True)
    (proj_dir / "a.jsonl").write_text(
        json.dumps({"sessionId": "sess-aaa", "type": "user"}) + "\n",
        encoding="utf-8",
    )
    (proj_dir / "b.jsonl").write_text(
        json.dumps({"sessionId": "sess-bbb", "type": "user"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    return home, slug, str(Path("C:/Users/someone/elsewhere"))


# ---------------------------------------------------------------------------
# Tool profile + dispatcher reachability
# ---------------------------------------------------------------------------

def test_register_claude_source_in_interactive_profile():
    from prism_service.mcp.tools import (
        INTERACTIVE_TOOL_NAMES,
        TOOLS,
        tool_names_for_profile,
    )

    names = {t.name for t in TOOLS}
    assert "register_claude_source" in names, (
        "register_claude_source must be registered in TOOLS"
    )
    assert "register_claude_source" in INTERACTIVE_TOOL_NAMES
    assert "register_claude_source" in tool_names_for_profile("interactive")


def test_register_claude_source_reachable_through_dispatcher(
    isolated_projects, claude_home_with_jsonl
):
    """Drive the REAL MCP entry point (call_tool under the interactive
    profile) — not the bare handler — so a defined-but-unwired tool fails."""
    from prism_service.mcp.request_context import (
        PrismRequestContext,
        use_request_context,
    )
    from prism_service.mcp.server import call_tool

    _home, _slug, cwd = claude_home_with_jsonl
    with use_request_context(
        PrismRequestContext(project_id="proj-cc", tool_profile="interactive")
    ):
        result = asyncio.run(
            call_tool(
                "register_claude_source",
                {"project": "proj-cc", "cwd": cwd},
            )
        )

    # An unwired/profile-blocked tool returns CallToolResult(isError=True);
    # assert we got a real success payload instead.
    content = result.content if hasattr(result, "content") else result
    payload = json.loads(content[0].text)
    assert "error" not in payload, payload
    assert payload.get("jsonl_count") == 2, payload
    assert payload.get("resolved_dir"), payload


def test_register_claude_source_resolves_and_validates(
    isolated_projects, claude_home_with_jsonl
):
    from prism_service.mcp.tools import handle_tool

    home, slug, cwd = claude_home_with_jsonl
    result = asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": cwd},
            project_id="proj-cc",
        )
    )
    payload = json.loads(result[0].text)
    expected = home / "projects" / slug
    assert Path(payload["resolved_dir"]) == expected
    assert payload["jsonl_count"] == 2


def test_register_claude_source_missing_dir_reports_error(
    isolated_projects, monkeypatch, tmp_path
):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty_home"))
    from prism_service.mcp.tools import handle_tool

    result = asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": "C:/nope/missing"},
            project_id="proj-cc",
        )
    )
    payload = json.loads(result[0].text)
    # No transcripts dir / no *.jsonl -> structured error, not a crash.
    assert payload.get("ok") is False or "error" in payload, payload


# ---------------------------------------------------------------------------
# Persistence through the SAME writer + read-back across a separate call
# ---------------------------------------------------------------------------

def test_register_persists_claude_project_dir_via_state_writer(
    isolated_projects, claude_home_with_jsonl
):
    from prism_service.mcp.tools import handle_tool

    home, slug, cwd = claude_home_with_jsonl
    asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": cwd},
            project_id="proj-cc",
        )
    )
    # Fresh read off disk via the canonical reader the Settings editor uses.
    state = ue._read_state("proj-cc")
    assert state.get("claude_project_dir") == str(home / "projects" / slug)


def test_register_persists_source_path_for_live_token_graph(
    isolated_projects, claude_home_with_jsonl
):
    """The agent's MCP registration must ALSO set source_path (= the cwd), so
    the LIVE conductor token / burn graph — which resolves transcripts via
    _project_source_path() -> source_path, NOT claude_project_dir — reads the
    SAME folder the agent registered. Before this fix register set only
    claude_project_dir (the import poller), so the token graph fell back to a
    stale/empty source_path -> empty (#134) or a wrong-session 40/flatline."""
    from prism_service.mcp.tools import handle_tool
    from prism_service.services.claude_transcripts import _project_source_path

    _home, _slug, cwd = claude_home_with_jsonl
    asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": cwd},
            project_id="proj-cc",
        )
    )
    state = ue._read_state("proj-cc")
    assert state.get("source_path") == cwd, \
        "register_claude_source must persist source_path=cwd for the token graph"
    # The live-token-graph resolver reads it back as the project's source path.
    assert _project_source_path("proj-cc") == cwd


def test_configured_project_dir_reads_persisted_value(
    isolated_projects, claude_home_with_jsonl
):
    """The poller's override resolver must read the persisted dir back.
    Today `from prism_service.services import claude_memory` fails, so
    this import alone is a red signal."""
    from prism_service.mcp.tools import handle_tool
    from prism_service.services import claude_memory as cm

    home, slug, cwd = claude_home_with_jsonl
    asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": cwd},
            project_id="proj-cc",
        )
    )
    assert cm.configured_project_dir("proj-cc") == str(home / "projects" / slug)
    # Unconfigured project resolves to None (no false override).
    assert cm.configured_project_dir("proj-never") is None


def test_register_is_idempotent_updates_dir(
    isolated_projects, claude_home_with_jsonl, tmp_path
):
    from prism_service.mcp.tools import handle_tool
    from prism_service.services import claude_memory as cm

    home, slug, cwd = claude_home_with_jsonl
    asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": cwd},
            project_id="proj-cc",
        )
    )
    # Register a DIFFERENT source dir for the same project.
    slug2 = "D--other-repo"
    new_dir = home / "projects" / slug2
    new_dir.mkdir(parents=True)
    (new_dir / "x.jsonl").write_text(
        json.dumps({"sessionId": "sess-x"}) + "\n", encoding="utf-8"
    )
    asyncio.run(
        handle_tool(
            "register_claude_source",
            {"project": "proj-cc", "cwd": "D:/other/repo"},
            project_id="proj-cc",
        )
    )
    assert cm.configured_project_dir("proj-cc") == str(new_dir)
