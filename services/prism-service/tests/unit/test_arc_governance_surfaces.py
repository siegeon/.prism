"""RED scaffold — governance surfaces (task 8579d49e, d2/e1/e2/c2/f1).

No JS test runner in the web app, so UI-FIRST criteria are pinned by
asserting the SOURCE (same pattern as test_adaptive_policy_ui.py):

  * d2 — the project Understand page surfaces a violations card and a
    ViolationsView component renders count summary + click-to-expand
    (progressive disclosure);
  * e1 — the trimmed mermaid-syntax skill sits at the BYOS-injected
    project-skill path with MIT attribution + diagram-type coverage;
  * e2 — prototype.js's synthesis prompt points at the skill;
  * c2 — implement.js injects Brain-stored principles into dev context;
  * f1 — PRISM_VERSION carries the 6.4 minor bump.

FAILS today: no ViolationsView.tsx, no mermaid-syntax skill, no
"principle"/"mermaid-syntax" mention in the workflows, version 6.3.x.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_REPO = _SERVICE_ROOT.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


# ── d2: Understand is the consolidated OKF concept wiki (v6.7.0) ────────
# The v6.7.0 4->2 knowledge consolidation replaced the old Understand
# architecture-analysis TABS (tour/layers/domains/violations) with one
# OKF concept wiki (graph + detail panel). The architecture-VIOLATIONS
# governance card was retired alongside the deprecated understand_*
# analysis surface; the ViolationsView component is kept (next test) for
# the tracked follow-up that rehomes governance near /conductor. So the
# Understand page is now the concept wiki, NOT a violations card host.

def test_understand_page_is_the_okf_concept_wiki():
    src = (_WEB / "pages" / "UnderstandPage.tsx").read_text(encoding="utf-8")
    assert "/api/okf/graph" in src, (
        "Understand must be the OKF concept wiki (concept graph)")
    assert "violations" not in src.lower(), (
        "the legacy architecture-violations card was retired from the "
        "consolidated wiki (v6.7.0); governance rehoming is a follow-up")


def test_violations_view_renders_count_and_expands():
    vv = _WEB / "components" / "understand" / "ViolationsView.tsx"
    assert vv.exists(), (
        "ViolationsView component missing (sibling of LayersView)")
    src = vv.read_text(encoding="utf-8")
    assert "count" in src, "card must lead with a count summary"
    assert "useState" in src, (
        "progressive disclosure: click-to-expand needs local open state")


# ── e1: mermaid-syntax skill at the BYOS-injected path ─────────────────

_SKILL = _REPO / ".claude" / "skills" / "mermaid-syntax" / "SKILL.md"


def test_mermaid_skill_placed_for_byos_injection():
    assert _SKILL.exists(), (
        ".claude/skills/mermaid-syntax/SKILL.md missing — BYOS injection "
        "cannot reach the prototype/implement workflow agents")


def test_mermaid_skill_carries_mit_attribution_and_diagrams():
    src = _SKILL.read_text(encoding="utf-8")
    assert "MIT" in src, "MIT attribution frontmatter required"
    for kind in ("sequenceDiagram", "classDiagram", "flowchart",
                 "stateDiagram", "erDiagram"):
        assert kind in src, f"diagram type {kind} not covered"
    assert "c4" in src.lower(), "c4-layout-science section required"
    assert "Hermes" in src, "Hermes theming guidance required"


# ── e2/c2: workflow seams consult the new governance data ──────────────

def test_prototype_synthesis_prompt_references_mermaid_skill():
    src = (_REPO / ".claude" / "workflows" / "prototype.js").read_text(
        encoding="utf-8")
    assert "mermaid-syntax" in src, (
        "prototype.js synthesis prompt must point at the mermaid-syntax "
        "skill for plan_diagram")


def test_implement_workflow_injects_principles_into_dev_context():
    src = (_REPO / ".claude" / "workflows" / "implement.js").read_text(
        encoding="utf-8")
    assert "principle" in src.lower(), (
        "implement_tasks must inject Brain-stored principles into the "
        "dev agent's context")


def test_version_carries_the_6_4_minor_bump():
    from prism_service.__version__ import PRISM_VERSION
    major, minor = (int(x) for x in PRISM_VERSION.split(".")[:2])
    assert (major, minor) >= (6, 4), f"PRISM_VERSION={PRISM_VERSION}"
