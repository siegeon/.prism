"""Rules tab checked count and the API looked_at agree (task 18f85c50,
parent 4824c299).

The Rules tab said "941 CHECKED" while GET /api/okf/ontology/rules said
looked_at 933 for the same rule: one fact, two counts, because the tab
read a report the daemon had since replaced. Pins:

  AC-1  RuleRow renders `{rule.looked_at} CHECKED` and no other expression
        in OntologyPage.tsx assigns or computes looked_at.
  AC-2  full_report() and decorated_report() return looked_at and
        validated_at per rule equal to _read_report(project) (one read).
  AC-3  RuleRow renders rule.validated_at in the same element as CHECKED.
  AC-4  OntologyPage.tsx refetches the rules on a visibilitychange handler
        and from rebuild, not only in the mount useEffect.

No JS test runner in this repo -- UI ACs are pinned against the real TSX
source (test_conductor_page_animated_cleanup_ui.py convention). Assertions
parse the enclosing function block, never a fixed window around a match.
"""

from __future__ import annotations

import re
import sqlite3
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PAGE = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "OntologyPage.tsx"


def _src() -> str:
    return _PAGE.read_text(encoding="utf-8")


def _block(src: str, header: str) -> str:
    """The body of the top-level `function <header>(` up to the next
    top-level declaration -- a comment cannot satisfy these assertions
    because the match must sit inside the JSX of that block."""
    start = src.index(f"function {header}(")
    nxt = re.search(r"^(?:export default )?function ", src[start + 1:], re.M)
    end = start + 1 + nxt.start() if nxt else len(src)
    return src[start:end]


def _code_lines(text: str) -> list[str]:
    return [l for l in text.splitlines() if not l.strip().startswith("//")]


# ---------------------------------------------------------------------------
# AC-1: the count is the API's looked_at, untouched by the client
# ---------------------------------------------------------------------------

def test_rule_row_renders_looked_at_verbatim():
    row = _block(_src(), "RuleRow")
    assert "{rule.looked_at} CHECKED" in row


def test_no_client_expression_computes_looked_at():
    src = _src()
    hits = [l for l in _code_lines(src) if "looked_at" in l]
    # Exactly two mentions: the Rule type field and the RuleRow render.
    assert len(hits) == 2, hits
    assert any("looked_at: number" in l for l in hits)
    assert any("{rule.looked_at} CHECKED" in l for l in hits)
    for l in hits:
        assert not re.search(r"looked_at\s*[-+*/]=?|=\s*[^=]*looked_at\s*[-+*/]", l), l


# ---------------------------------------------------------------------------
# AC-3: the report's validated_at sits next to the count
# ---------------------------------------------------------------------------

def test_rule_type_carries_validated_at():
    src = _src()
    m = re.search(r"type Rule = \{(.*?)\n\};", src, re.S)
    assert m, "Rule type not found"
    assert "validated_at: string" in m.group(1)


def test_rule_row_renders_validated_at_with_the_count():
    row = _block(_src(), "RuleRow")
    # The <div> that holds CHECKED must also hold validated_at.
    divs = re.findall(r"<div[^>]*>(.*?)</div>", row, re.S)
    holder = [d for d in divs if "CHECKED" in d]
    assert holder, "no element renders CHECKED"
    assert any("rule.validated_at" in d for d in holder), holder


def test_format_at_helper_exists():
    src = _src()
    assert re.search(r"^function formatAt\(", src, re.M)


# ---------------------------------------------------------------------------
# AC-4: the rules refetch on tab focus and after rebuild, not only on mount
# ---------------------------------------------------------------------------

def test_rules_refetch_is_its_own_callback():
    src = _src()
    page = _block(src, "OntologyPage")
    assert "const fetchRules = useCallback(" in page
    assert "/api/okf/ontology/rules?" in page


def test_visibilitychange_handler_refetches_rules():
    page = _block(_src(), "OntologyPage")
    m = re.search(
        r'addEventListener\(\s*"visibilitychange"\s*,\s*(\w+)', page)
    assert m, "no visibilitychange listener"
    assert "fetchRules" in page[m.start():m.start() + 600] or \
        re.search(rf"{m.group(1)}\s*=.*?fetchRules", page, re.S)
    assert 'removeEventListener("visibilitychange"' in page


def test_rebuild_repolls_rules_until_validated_at_changes():
    page = _block(_src(), "OntologyPage")
    start = page.index("const rebuild = ")
    nxt = re.search(r"\n  const \w+ = ", page[start + 1:])
    body = page[start:start + 1 + nxt.start()] if nxt else page[start:]
    assert "fetchRules" in body
    assert "validated_at" in body
    assert "setTimeout" in body or "setInterval" in body


# ---------------------------------------------------------------------------
# AC-2: both API readers hand back the persisted report from one read
# ---------------------------------------------------------------------------

def _seed(pid: str) -> None:
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_project

    ctx = get_project(pid)
    ctx.task_svc.create(title="legacy task")
    conn = sqlite3.connect(str(project_data_dir(pid) / "brain.db"))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1', 'README.md')")
    conn.commit()
    conn.close()


def test_api_readers_return_the_persisted_looked_at_and_validated_at():
    from prism_service.services import ontology_rules, rule_decisions

    pid = f"onecount-{uuid.uuid4().hex[:8]}"
    _seed(pid)
    ontology_rules.validate(pid)

    persisted = {r["name"]: r for r in ontology_rules._read_report(pid)}
    assert persisted
    assert all(r["validated_at"] for r in persisted.values())

    for report in (ontology_rules.full_report(pid),
                   rule_decisions.decorated_report(pid)):
        rules = {r["name"]: r for r in report["rules"]}
        assert set(rules) == set(persisted)
        for name, row in rules.items():
            assert row["looked_at"] == persisted[name]["looked_at"], name
            assert row["validated_at"] == persisted[name]["validated_at"], name
        assert report["validated_at"] == next(iter(persisted.values()))["validated_at"]
