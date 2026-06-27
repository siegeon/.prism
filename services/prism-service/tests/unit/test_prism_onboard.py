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


# ── README value-prop install story + honest benchmark section ──────────


def _readme():
    return (_SERVICE_ROOT.parent.parent / "README.md").read_text(
        encoding="utf-8")


def test_readme_leads_with_all_you_need_install_story():
    """AC-1 — the README LEADS with the 'all you need' value prop: the
    install triple (pipx install / prism start / call prism_guide) plus the
    one-call prism_onboard bootstrap."""
    low = _readme().lower()
    assert "all you need" in low
    assert "pipx install prism-service" in low
    assert "prism start" in low
    assert "prism_guide" in low
    assert "prism_onboard" in low


def test_readme_has_honest_benchmark_section():
    """AC-2 — a Benchmarks section carries the REAL LongMemEval R@5 figures
    AND the honest caveat that PRISM does not yet claim public-best."""
    readme = _readme()
    low = readme.lower()
    assert "benchmark" in low
    # LongMemEval R@5 trajectory anchors (potion baseline -> multi-granular).
    assert "0.524" in readme
    assert "0.940" in readme
    # Honest caveat: not yet proven vs the best public coding agents.
    assert "swe-bench" in low
    assert "proof campaign" in low or "not yet" in low


def test_readme_is_progressively_disclosed():
    """AC-3 — the README applies PRISM's own progressive-disclosure
    principle: deep detail is tucked into collapsible <details> blocks so the
    top of the page stays scannable. Assert at least three of them."""
    readme = _readme()
    assert readme.count("<details>") >= 3, "README must use >=3 <details> blocks"
    assert readme.count("<details>") == readme.count("</details>")


def test_readme_benchmarks_are_comparative_and_honest():
    """AC-2 (extended) — benchmarks are COMPARATIVE (a baseline lift + an
    industry-context cue) AND honest (the recall@5-vs-QA-accuracy metric
    caveat that keeps cross-system numbers directional only)."""
    readme = _readme()
    low = readme.lower()
    # Comparative: the lift trajectory off a baseline.
    assert "baseline" in low
    assert "0.524" in readme and "0.634" in readme
    assert "0.94" in readme or "0.98" in readme
    # Industry context exists but is explicitly DIRECTIONAL, not a leaderboard.
    assert "longmemeval" in low
    assert "directional" in low
    # Honest metric caveat: recall@5 != end-to-end QA accuracy.
    assert "recall@5" in low
    assert "qa accuracy" in low or "qa-accuracy" in low


# ── README v6.7.4 polish: clean quickstart + cross-system table ─────────


def test_readme_quickstart_is_cleanly_numbered():
    """AC-1 — the quickstart reads as a clean numbered list 1->2->3->4 (no
    a/b sub-steps), and still names the install + bootstrap commands."""
    readme = _readme()
    low = readme.lower()
    for step in ("1.", "2.", "3.", "4."):
        assert step in readme, f"quickstart missing step {step}"
    # No a/b sub-step numbering anymore.
    assert "3a" not in low and "3b" not in low
    assert "pipx install prism-service" in low
    assert "prism start" in low
    assert "prism_onboard" in low
    assert "prism_guide" in low


def test_readme_has_cross_system_comparison_table():
    """AC-2 — a cross-system comparison table on LongMemEval naming PRISM plus
    at least three other systems, with a Metric column header."""
    readme = _readme()
    low = readme.lower()
    assert "| metric |" in low or "| system | metric |" in low or "metric |" in low
    assert "prism" in low
    others = [s for s in ("mempalace", "mastra", "hindsight", "letta",
                          "zep", "mem0") if s in low]
    assert len(others) >= 3, f"expected >=3 other systems, found {others}"


def test_readme_has_no_honest_word():
    """AC-3 — the word 'honest'/'honestly' appears nowhere in the README."""
    assert "honest" not in _readme().lower()


def test_readme_keeps_longmemeval_figures_and_caveat():
    """AC-4 — the README retains the LongMemEval anchor figures (0.524 and
    0.94) and the not-yet-public-best caveat."""
    readme = _readme()
    low = readme.lower()
    assert "0.524" in readme
    assert "0.94" in readme
    assert "swe-bench" in low
    assert "not yet" in low or "proof campaign" in low
