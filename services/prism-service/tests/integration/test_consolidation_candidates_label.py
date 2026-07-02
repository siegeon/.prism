"""RED scaffold — Consolidation SESSIONS-SCANNED mislabel (task eca12a5f).

STRESS-LOOP FINDING: the /consolidation signal strip renders
"SESSIONS SCANNED 8", but rollup.sessions_scanned is len(rows) over
consolidation_candidates (get_signal_rollup) — a COUNT OF REFLECTION
CANDIDATES, not sessions. /sessions shows 2 imported session outcomes.
A reader cross-checking the two pages sees 8 vs 2 for "sessions" and
reads a contradiction, even though the scopes legitimately differ.

Fix (label-only, front-end): the strip labels the count for what it
actually is — reflection candidates — so no false cross-page
session contradiction remains.

Source-structure test — scans the real ConsolidationPage render path on
disk. FAILS today: the rollup.sessions_scanned Kpi is labelled
"Sessions scanned".
"""
from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_CONSOL = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
           / "ConsolidationPage.tsx")


def test_rollup_count_labelled_candidates_not_sessions():
    """The Kpi bound to rollup.sessions_scanned must label it as candidates
    (its true scope), not 'Sessions scanned'."""
    src = _CONSOL.read_text(encoding="utf-8")
    # Find the Kpi whose value is rollup.sessions_scanned and read its label.
    m = re.search(r'label="([^"]*)"\s+value=\{rollup\.sessions_scanned\}', src)
    assert m, ("could not find the Kpi bound to rollup.sessions_scanned — "
               "did the value expression change?")
    label = m.group(1).lower()
    assert "session" not in label, (
        f"rollup.sessions_scanned counts consolidation_candidates, not "
        f"sessions — label {m.group(1)!r} must not say 'session' or it "
        f"contradicts the /sessions page count"
    )
    assert "candidate" in label, (
        f"label {m.group(1)!r} should name what it counts (candidates)"
    )
