"""The Evidence tab's collapsible sections must use controlled React state,
not native <details>/<summary> (owner QA finding, live remote-assist
session, 2026-08-24).

THE BUG: on a genuinely fresh page load (confirmed across full route
unmount/remount, not a stale-state or screenshot-timing artifact), the
three native <details> blocks on TaskDetailPage.tsx's Evidence tab
("Acceptance criteria", "how a pass could still be wrong", "audit detail
(machine text)") rendered their body content visibly EXPANDED despite
carrying no `open` attribute -- garbled/overlapping with the "Decision
packet" box below them. DecisionPacket.tsx's own `Row` component, right
next to this content on the same page, uses controlled `useState` +
conditional rendering and collapses correctly. Root cause in the live
tab's native <details> handling wasn't pinned down; the fix sidesteps it
with the pattern already proven correct elsewhere on the same page.

The PRISM SPA has NO JS test runner, so this is pinned by asserting the
ACTUAL source -- same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_TASK_DETAIL = _HERE.parent.parent.parent / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"


def _read() -> str:
    assert _TASK_DETAIL.exists(), f"expected source missing: {_TASK_DETAIL}"
    return _TASK_DETAIL.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment (like this file's
    own explanatory header, which names the retired <details> tag by
    name) can never satisfy a source assertion."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def test_no_native_details_element_remains_in_the_file():
    src = _strip_comments(_read())
    assert "<details" not in src, (
        "TaskDetailPage.tsx must not use native <details> -- it rendered "
        "expanded on a fresh load in the owner's live tab; use the "
        "Disclosure component instead"
    )
    assert "<summary" not in src


def test_disclosure_component_defaults_closed_via_real_state():
    src = _read()
    idx = src.index("function Disclosure(")
    end = src.index("\nfunction Stagger(", idx)
    body = src[idx:end]
    assert "useState(false)" in body, (
        "Disclosure must default to closed via a real useState(false), not "
        "an uncontrolled native element"
    )
    assert "{open && children}" in body


def test_all_three_evidence_disclosures_use_the_component():
    src = _read()
    assert src.count("<Disclosure") == 3, (
        f"expected exactly 3 <Disclosure> usages (Acceptance criteria, "
        f"how a pass could still be wrong, audit detail), got "
        f"{src.count('<Disclosure')}"
    )
    for label in ("Acceptance criteria", "how a pass could still be wrong", "audit detail (machine text)"):
        assert label in src, f"expected the {label!r} disclosure to survive the migration"
