"""UI acceptance test for: Internal agent page gets a proper header title
(task 8f92751f-1235-43a4-b392-7d5d6026eabb).

The web app ships no JS test runner, so — exactly as
test_tasks_timeline_rich_markdown_ui.py and the other *_ui.py tests do —
the acceptance criteria are pinned by asserting over the TSX SOURCE.

/internal-agent (App.tsx Route -> InternalAgentPage) renders with the
PageHeader fallback title "PRISM" because PageHeader.tsx's route-title map
(TITLES) has no "/internal-agent" entry. The fix follows the existing
longest-prefix-match pattern (resolveTitle): one entry in the TITLES map,
no bespoke branch.

FAILS at scaffold time because PageHeader.tsx's TITLES map carries no
"/internal-agent" key (AC-1/AC-2).
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_PAGE_HEADER = _WEB / "components" / "PageHeader.tsx"
_APP = _WEB / "App.tsx"


def _read(p: Path) -> str:
    assert p.exists(), f"expected source file missing: {p}"
    return p.read_text(encoding="utf-8")


def _titles_block(src: str) -> str:
    """The TITLES literal consumed by resolveTitle's longest-prefix lookup."""
    m = re.search(r"const TITLES[^=]*=\s*\{(.*?)\};", src, re.DOTALL)
    assert m, "PageHeader.tsx must keep the TITLES route-title map"
    return m.group(1)


# ---- AC-1: TITLES maps /internal-agent -> "Internal agent" -----------------

def test_internal_agent_route_exists():
    """Precondition seam: the route this title serves is real."""
    app = _read(_APP)
    assert '"/internal-agent"' in app, \
        "App.tsx must route /internal-agent (InternalAgentPage)"


def test_titles_map_carries_internal_agent_entry():
    src = _read(_PAGE_HEADER)
    block = _titles_block(src)
    assert re.search(r'"/internal-agent"\s*:\s*"Internal agent"', block), (
        'AC-1: TITLES in PageHeader.tsx must map "/internal-agent" to '
        '"Internal agent" so the header does not fall back to "PRISM"'
    )


# ---- AC-2: the title flows through the longest-prefix map, no bespoke branch

def test_mapping_lives_in_titles_map_only():
    src = _read(_PAGE_HEADER)
    block = _titles_block(src)
    # resolveTitle stays the single consumer of TITLES.
    assert "function resolveTitle" in src and "TITLES[" in src, \
        "AC-2: resolveTitle's longest-prefix lookup over TITLES must remain"
    # Every mention of internal-agent sits inside the TITLES literal —
    # no new conditional special-casing the route outside the map.
    outside = src.replace(block, "")
    assert "internal-agent" not in outside, (
        "AC-2: no bespoke internal-agent branch outside the TITLES map — "
        "the entry must ride the existing longest-prefix-match pattern"
    )
