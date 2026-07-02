"""prism_guide must be a genuinely concise orientation (task 5f93f67a).

The overview section (and thus the no-section default) embedded the entire
PRISM_VERSION_NOTES changelog via _version_banner(), so a "READ FIRST" call
cost ~50K tokens. The default + overview must be single-digit-K tokens; the
exhaustive release history must remain reachable behind an explicit
section='changelog'.

RED before the fix: default ~201K chars, overview ~184K chars, and there is
no 'changelog' section.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# ~8K tokens at ~4 chars/token — a sane READ-FIRST budget.
BUDGET = 32000


def test_default_orientation_is_concise():  # AC-1
    from prism_service.mcp.tools import _prism_guide
    out = _prism_guide(None)
    assert len(out) < BUDGET, (
        f"default prism_guide is {len(out)} chars (~{len(out)//4} tokens); "
        f"must be under {BUDGET}"
    )
    # Still a real orientation: names the key tools.
    assert "brain_search" in out
    assert "memory_store" in out


def test_overview_is_concise_and_points_to_changelog():  # AC-2
    from prism_service.mcp.tools import _prism_guide
    out = _prism_guide("overview")
    assert len(out) < BUDGET, (
        f"overview is {len(out)} chars (~{len(out)//4} tokens); "
        f"must be under {BUDGET}"
    )
    assert "changelog" in out.lower(), (
        "overview must point readers to the changelog section"
    )


def test_full_history_relocated_behind_changelog_section():  # AC-3
    from prism_service.mcp.tools import _prism_guide, _GUIDE_SECTIONS
    from prism_service.__version__ import PRISM_VERSION_NOTES
    assert "changelog" in _GUIDE_SECTIONS, (
        "the exhaustive release history must live in an explicit "
        "'changelog' section"
    )
    full = _prism_guide("changelog")
    assert len(full) > 50000, (
        "the full release history must remain reachable (not lost)"
    )
    assert PRISM_VERSION_NOTES[:2000] in full
