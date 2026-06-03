"""RED scaffold — version bump pin for the register_claude_source slice
(task b6650506).

This change is user-visible (a new MCP verb, new API endpoints, a new
Settings card), so PRISM_VERSION must patch-bump past the base 6.3.15.
The SPA footer is how the user verifies the build, per the standing
patch-bump-per-iteration mandate.

FAILS today: PRISM_VERSION is still 6.3.15.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.__version__ import PRISM_VERSION


def _tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def test_version_bumped_past_base():
    assert _tuple(PRISM_VERSION) > (6, 3, 15), (
        f"PRISM_VERSION must patch-bump past 6.3.15 for this user-visible "
        f"slice; got {PRISM_VERSION}"
    )
