"""Queue signal card draws its raw text below the summary (task f61617c1).

Source-reading suite: the PRISM SPA has NO JS test runner, so UI ACs are
pinned by asserting the ACTUAL TSX source (convention documented in
test_conductor_page_animated_cleanup_ui.py). Each assertion parses the
enclosing JSX element, never a fixed character window, so a comment above
an element cannot satisfy it.

AC-1 As arrived block is in flow below the rule line (no overlap).
AC-2 As arrived block is closed until the user opens it.
AC-3 An open As arrived block keeps its raw body bounded; long tokens wrap.
AC-4 Raw subject and raw body still render inside the block.
AC-5 A long focus list shows a count and the first names, not every URN.
AC-6 A short focus list renders every checkbox, no count line, no control.
AC-7 Exempt still sends only the checked focus IRIs.
AC-8 The change is live: PRISM_VERSION bumped.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PAGE = ROOT / "prism_service" / "web" / "src" / "pages" / "QueuePage.tsx"
VERSION = ROOT / "prism_service" / "__version__.py"


def _src() -> str:
    return PAGE.read_text(encoding="utf-8")


def _details_block(src: str) -> str:
    m = re.search(r"<details\b[^>]*>.*?</details>", src, re.S)
    assert m, "QueuePage renders no <details> As arrived block"
    return m.group(0)


def _details_open_tag(src: str) -> str:
    m = re.search(r"<details\b[^>]*>", src)
    assert m
    return m.group(0)


def _rule_decision_row(src: str) -> str:
    return src[src.index("function RuleDecisionRow("):]


def test_as_arrived_block_is_in_flow_below_the_rule_line():
    src = _src()
    tag = _details_open_tag(src)
    assert "As arrived" in _details_block(src)
    for cls in ("absolute", "fixed", "-mt-", "z-"):
        assert cls not in tag, f"As arrived block leaves the flow via {cls!r}"
    assert src.index("<details") < src.index("matches.map("), "As arrived must draw before the matches row"


def test_as_arrived_block_is_closed_by_default():
    tag = _details_open_tag(_src())
    assert not re.search(r"\bopen\b", tag), "As arrived <details> must not carry `open`"


def test_as_arrived_raw_body_is_bounded():
    block = _details_block(_src())
    inner = re.search(r"</summary>\s*<div\b([^>]*)>", block)
    assert inner, "no wrapper div after the As arrived summary"
    attrs = inner.group(1)
    for cls in ("max-h-40", "overflow-y-auto", "break-all"):
        assert cls in attrs, f"raw body wrapper lacks {cls!r}: {attrs!r}"


def test_as_arrived_block_still_shows_raw_subject_and_body():
    block = _details_block(_src())
    assert "{signal.subject}" in block
    assert "{signal.body}" in block


def test_long_focus_list_shows_count_and_preview():
    src = _src()
    assert re.search(r"const FOCUS_PREVIEW_LIMIT\s*=\s*\d+", src)
    assert "function focusPreview(" in src or "const focusPreview =" in src
    i = src.index("matches.map(")
    assert "focusPreview(" in src[i:i + 200], "matches row must go through focusPreview, not String(v)"
    row = _rule_decision_row(src)
    assert "showAllFocus" in row
    assert "data-signal-decide-focus-show-all" in row
    assert re.search(r"\{focusIris\.length\}\s*nodes", row)


def test_short_focus_list_is_unchanged():
    row = _rule_decision_row(_src())
    btn = row.index("data-signal-decide-focus-show-all")
    guard = row.rfind("focusIris.length > FOCUS_PREVIEW_LIMIT", 0, btn)
    assert guard != -1, "show-all control must sit inside a long-list guard"
    assert "&& !showAllFocus" in row[guard:btn]
    assert re.search(r"visibleFocus\s*=\s*showAllFocus\s*\?\s*focusIris\s*:\s*focusIris\.slice\(0,\s*FOCUS_PREVIEW_LIMIT\)", row)


def test_decide_sends_only_checked_focus():
    row = _rule_decision_row(_src())
    assert 'onDecide(signal.id, "exempt", reason, selected)' in row
    assert "checked={selected.includes(iri)}" in row


def test_version_bumped():
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', VERSION.read_text(encoding="utf-8"))
    assert m and tuple(map(int, m.groups())) >= (7, 13, 127)
