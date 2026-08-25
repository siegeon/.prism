"""The task page's Approve/Reject buttons and quick-status pills must carry
stable ids -- discovered live (2026-08-25): find()'s returned CSS selector
for the real "Approve" button (`div:nth-of-type(2) > button:nth-of-type(1)`)
was IDENTICAL to the "-> done" quick-status pill's own selector. Since a
plain CSS query returns the FIRST document-order match, and the status
pills render earlier on the page than the gate-decision card, that
selector deterministically resolved to the wrong button -- not a fluke,
a reproducible trap. Same root issue as the earlier #gate-recovery fix
(test_gate_recovery_rewind_ui.py), now closed for the two highest-stakes
controls on the page.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned
by asserting the ACTUAL TSX source (tests/unit/test_conductor_page_
animated_cleanup_ui.py:4-6). Comments are stripped before every assertion
(repo convention, CLAUDE.md Lessons e139295d).
"""

from __future__ import annotations

import re
from pathlib import Path

_TSX = (Path(__file__).resolve().parent.parent.parent / "prism_service"
       / "web" / "src" / "pages" / "TaskDetailPage.tsx")


def _strip_comments(src: str) -> str:
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _read() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _strip_comments(_TSX.read_text(encoding="utf-8"))


def test_approve_button_has_a_stable_id():
    src = _read()
    idx = src.index('id="gate-decide-approve"')
    block = src[idx:idx + 400]
    assert 'onClick={() => gateDecide("approve")}' in block, (
        f"id=gate-decide-approve must sit on the real Approve button: {block}"
    )


def test_reject_button_has_a_stable_id():
    src = _read()
    idx = src.index('id="gate-decide-reject"')
    block = src[idx:idx + 400]
    assert 'onClick={() => gateDecide("reject")}' in block, (
        f"id=gate-decide-reject must sit on the real Reject button: {block}"
    )


def test_status_transition_pills_have_stable_per_target_ids():
    src = _read()
    idx = src.index("id={`status-transition-${target}`}")
    block = src[idx:idx + 300]
    assert "onClick={() => setStatus(target)}" in block, (
        f"the per-target id must sit on the status quick-pill button: {block}"
    )
