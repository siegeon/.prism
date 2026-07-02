"""UI acceptance test for: Learning agent-runs empty-state must cross-link to
the Internal agent page (task 262bc81a-2312-417c-93c3-6b5b295ab5e6).

The /learning "Agent runs · timeline" empty-state reads /api/agent-runs (the
implement-workflow ingest ledger, empty on this instance) and used to print a
bare "No agent runs yet" — misleading now that real pi/local inference runs
DO exist and are surfaced on /internal-agent (a separate /api/pi-runs ledger).

The web app ships no JS test runner, so — exactly as
test_internal_agent_header_title_ui.py and the other *_ui.py tests do — the
acceptance criteria are pinned by asserting over the TSX SOURCE.

FAILS at scaffold time because LearningPage.tsx's agentRuns empty-state carries
no react-router Link to "/internal-agent" (AC-1/AC-2).
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_LEARNING = _WEB / "pages" / "LearningPage.tsx"
_APP = _WEB / "App.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


def _empty_state_branch(src: str) -> str:
    """The `agentRuns.length === 0 ? ( ... ) : (` empty-state region — the
    branch rendered when the agent-runs ledger is empty."""
    start = src.find("agentRuns.length === 0")
    assert start != -1, (
        "LearningPage.tsx must keep the agentRuns empty-state ternary")
    # Up to the alternative branch marker that closes the empty-state.
    end = src.find(") : (", start)
    assert end != -1, "agentRuns empty-state ternary must have an alt branch"
    return src[start:end]


# ---- precondition seam: the route the cross-link targets is real -----------

def test_internal_agent_route_exists():
    app = _read(_APP)
    assert '"/internal-agent"' in app, \
        "App.tsx must route /internal-agent (InternalAgentPage)"


# ---- AC-2: the cross-link is a real react-router Link, not a bare anchor ----

def test_learning_imports_react_router_link():
    src = _read(_LEARNING)
    assert re.search(
        r'import\s*\{[^}]*\bLink\b[^}]*\}\s*from\s*"react-router-dom"', src), (
        'AC-2: LearningPage.tsx must import Link from "react-router-dom" so '
        "the cross-link navigates in-app (no full page reload)"
    )


# ---- AC-1: the empty-state cross-links to /internal-agent ------------------

def test_empty_state_crosslinks_internal_agent():
    branch = _empty_state_branch(_read(_LEARNING))
    assert '<Link' in branch and 'to="/internal-agent"' in branch, (
        'AC-1: the agentRuns empty-state must render <Link to="/internal-agent"> '
        "so a reader with zero workflow runs is pointed at the populated "
        "internal-agent ledger instead of concluding nothing ran"
    )
    assert re.search(r"internal", branch, re.I), (
        "AC-1: the empty-state copy must name the Internal agent surface"
    )
