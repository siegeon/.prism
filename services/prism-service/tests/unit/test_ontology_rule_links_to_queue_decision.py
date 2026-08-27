"""A rule that needs a decision links to its Queue decision (task 07c2f746).

The PRISM SPA has NO JS test runner, so the UI acceptance criteria are pinned
by reading the ACTUAL TSX source. Comments are stripped BEFORE any match so
that a prose note can never satisfy an assertion; the Decide link is parsed
out of the ``flagged &&`` branch of ``RuleRow``, never from a fixed window.

AC-1/AC-2/AC-4: RuleRow renders ``<Link data-rule-decide>Decide</Link>`` to
``/queue?rule=<name>`` inside the flagged branch only.
AC-3: QueuePage reads ``?rule=``, anchors each card as ``signal-<id>`` and
scrolls to the matching open ontology signal.
AC-5: QueuePage renders ``No open decision for rule`` with a Link back to
``/ontology?tab=rules`` when nothing matches.
"""

from __future__ import annotations

import re
from pathlib import Path

_WEB = Path(__file__).resolve().parents[2] / "prism_service" / "web" / "src" / "pages"
_ONTOLOGY = _WEB / "OntologyPage.tsx"
_QUEUE = _WEB / "QueuePage.tsx"


def _source(path: Path) -> str:
    """Read a TSX file with every comment removed."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def _rule_row() -> str:
    src = _source(_ONTOLOGY)
    m = re.search(r"function RuleRow\(.*?(?=\nfunction )", src, flags=re.S)
    assert m, "RuleRow component not found in OntologyPage.tsx"
    return m.group(0)


def _flagged_branch(body: str) -> str:
    """The JSX expression guarded by ``flagged &&`` inside RuleRow."""
    m = re.search(r"\{\s*flagged\s*&&\s*\(?\s*(<Link\b.*?</Link>)", body, flags=re.S)
    assert m, "RuleRow has no `{flagged && <Link ...>}` branch"
    return m.group(1)


# ── AC-1, AC-2, AC-4: the Decide link on the Rules tab ───────────────────

def test_rule_row_renders_a_decide_link_in_the_flagged_branch():
    link = _flagged_branch(_rule_row())
    assert "data-rule-decide" in link
    assert re.search(r">\s*Decide\s*</Link>", link), link


def test_decide_link_points_at_the_queue_rule_parameter():
    link = _flagged_branch(_rule_row())
    assert "/queue?rule=" in link
    assert "rule.name" in link, "the link must carry the rule's own name"


def test_quiet_rule_row_has_no_decide_link_outside_the_flagged_branch():
    body = _rule_row()
    link = _flagged_branch(body)
    assert body.count("data-rule-decide") == link.count("data-rule-decide") == 1


# ── AC-3: the Queue scrolls to the open signal for that rule ─────────────

def test_queue_page_imports_use_search_params():
    src = _source(_QUEUE)
    m = re.search(r'import \{([^}]*)\} from "react-router-dom"', src)
    assert m and "useSearchParams" in m.group(1), "QueuePage must import useSearchParams"


def test_queue_page_reads_the_rule_parameter_and_scrolls_to_it():
    src = _source(_QUEUE)
    assert re.search(r'searchParams\.get\(\s*"rule"\s*\)', src)
    assert "scrollIntoView" in src


def test_signal_row_anchors_each_card_by_signal_id_and_rule():
    src = _source(_QUEUE)
    m = re.search(r"function SignalRow\(.*?(?=\nfunction )", src, flags=re.S)
    assert m, "SignalRow component not found in QueuePage.tsx"
    row = m.group(0)
    assert re.search(r"id=\{`signal-\$\{signal\.id\}`\}", row), "card root needs id=signal-<id>"
    assert "data-signal-rule" in row


# ── AC-5: the Queue says when no open decision matches ───────────────────

def test_queue_page_shows_a_missing_decision_notice_with_a_way_back():
    src = _source(_QUEUE)
    m = re.search(r"<div[^>]*data-rule-missing[^>]*>(.*?)</div>", src, flags=re.S)
    assert m, "QueuePage has no data-rule-missing notice"
    notice = m.group(1)
    assert "No open decision for rule" in notice
    assert re.search(r'<Link[^>]*to="/ontology\?tab=rules"', notice), notice
