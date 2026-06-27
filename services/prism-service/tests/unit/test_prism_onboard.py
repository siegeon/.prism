"""RED scaffold — prism_onboard bootstrap + max-fan-out playbook (issue #172).

A fresh coding agent landing on a new PRISM project has no single
"bootstrap me" entry: it must hand-seed principles (so plan_gate is
satisfiable), guess the .mcp.json url + ports, and the orchestration
playbook does not yet teach MAXIMIZING fan-out across the full toolkit.

Pins, RED-first (FAIL before prism_onboard / the extended guide / README):

  * AC-1 — prism_onboard is an INTERACTIVE tool; dispatching it on a fresh
    project SEEDS principles (load_principles > 0 after) and returns a
    bootstrap payload carrying the /mcp/?project= url + a prism_guide pointer.
  * AC-2 — the prism_guide text covers MAX FAN-OUT + the full toolkit:
    parallel subtasks/fan-out, distinct-actor (no self-override) gate
    verification, principles_seed, epic roll-up, memory write-back, and the
    OKF/Understand wiki.
  * AC-3 — README reflects the epic/subtask fan-out + the MCP toolkit.
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


@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "onboard-172"
    pc._contexts.clear()


def _call(tool, args, project_id):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(handle_tool(tool, args, project_id=project_id))[0].text


# ── AC-1: prism_onboard tool registered + bootstrap dispatch ────────────

def test_prism_onboard_registered_interactive():
    from prism_service.mcp.tools import TOOLS, INTERACTIVE_TOOL_NAMES
    names = {t.name for t in TOOLS}
    assert "prism_onboard" in names, "prism_onboard MCP tool not registered"
    assert "prism_onboard" in INTERACTIVE_TOOL_NAMES


def test_prism_onboard_seeds_and_returns_bootstrap(project):
    from prism_service.project_context import get_project
    from prism_service.services.arc_governance import load_principles

    out = json.loads(_call("prism_onboard", {}, project))
    # Seeds default principles so plan_gate is immediately satisfiable.
    rules = load_principles(get_project(project).memory_svc)
    assert len(rules) > 0, "prism_onboard must seed default principles"
    assert out.get("seeded", 0) > 0, out

    # Bootstrap payload: mcp url (/mcp/?project=<project>), ports, version,
    # and a "call prism_guide first" pointer.
    blob = json.dumps(out).lower()
    assert f"project={project}".lower() in blob, out
    assert "/mcp/" in blob, out
    assert "prism_guide" in blob, out
    assert str(out.get("prism_version") or "")
    assert out.get("mcp_port") and out.get("web_port"), out


# ── AC-2: prism_guide teaches MAX FAN-OUT over the full toolkit ─────────

def test_prism_guide_covers_max_fan_out_toolkit():
    from prism_service.mcp.tools import _prism_guide
    text = _prism_guide(None).lower()
    # parallel subtasks / fan-out
    assert "fan out" in text or "fan-out" in text
    assert "parallel" in text
    # distinct-actor, no self-override gate verification
    assert "distinct" in text and "self-override" in text
    # principles_seed so plan_gate passes
    assert "principles_seed" in text
    # epic child roll-up to the parent green_gate
    assert "roll-up" in text or "roll up" in text
    # write the WHY to memory at green_gate
    assert "memory_store" in text or "write the why" in text
    # browse knowledge via the OKF / Understand wiki
    assert "okf" in text and "understand" in text
    assert "brain_understand" in text
    # prism_onboard as the bootstrap entry
    assert "prism_onboard" in text


# ── AC-3: README reflects the full toolkit + fan-out ───────────────────

def test_readme_covers_fanout_and_toolkit():
    readme = (_SERVICE_ROOT.parent.parent / "README.md").read_text(
        encoding="utf-8")
    low = readme.lower()
    assert "epic" in low and "subtask" in low
    assert "fan out" in low or "fan-out" in low
    assert "prism_onboard" in low
    assert "okf_index" in low or "okf_get" in low
