"""Exempt keeps the reason the user typed (task e1888b31, parent 4824c299).

On the Queue the user types a reason in the Why field, ticks focus
records, and clicks Exempt. POST /api/signals/{id}/decide passes
body.reason to rule_decisions.decide (api/signals.py:182), but the
exempt branch drops it: _record_exempt takes no reason and writes only
the sorted IRI list (rule_decisions.py:130-136). The Rules tab cannot
show later why a file is exempt.

Pins (each AC's -k selector matches exactly one test):

  AC-1 stores_reason                       -- decisions.json holds
       exempt_reasons[iri] == {"at": iso, "reason": <literal text>}
       next to the unchanged sorted exempt list.
  AC-2 exempt_focus_unchanged              -- exempt_focus() still
       returns the IRI set (shape guard; green today, stays green).
  AC-3 report_returns_exempted_with_reason -- decorated_report() returns
       an "exempted" list with iri/label/reason/at; focus drops them.
  AC-4 second_exempt_replaces_only_its_iri -- per-IRI overwrite.
  AC-5 legacy_entry_without_reasons        -- old-shape decisions.json
       loads and reports reason == "" without raising.
  AC-6 reason_lives_in_decisions_not_signal -- the Signal row grows no
       field; decisions.json alone answers "why".

Every assertion compares the LITERAL stored text, never the function
under test applied to its own input.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

RULE = "no-artifacts-in-the-root"
REASON = "temp docs, owner approved"
SECOND_REASON = "second reason"


def _project_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed_two_loose_docs(pid: str) -> None:
    """README.md and CLAUDE.md loose in the project root, so
    no-artifacts-in-the-root fires on two focus records -- the shape
    the owner's journey (step 06) exempted. Same fixture posture as
    test_firing_rules_become_decisions._seed_two_firing_rules."""
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_project

    get_project(pid)  # creates the project data dir
    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1', 'README.md')")
    conn.execute("INSERT INTO docs VALUES ('d2', 'CLAUDE.md')")
    conn.commit()
    conn.close()


def _rebuild(pid: str) -> None:
    from prism_service.services import ontology_prototype_projection as proj

    proj.rebuild(pid)


def _rule_signal(pid: str):
    from prism_service.services.signal_store import SignalStore

    return next(s for s in SignalStore(pid).list(limit=500)
                if s.channel == "ontology" and s.channel_ref == f"rule:{RULE}")


def _rule_row(report: dict) -> dict:
    return next(r for r in report["rules"] if r["name"] == RULE)


def _two_focus_iris(pid: str) -> tuple[str, str]:
    """The two focus IRIs the rule fires on, ordered README then CLAUDE
    by label so the test names them like the owner's journey did."""
    from prism_service.services import rule_decisions

    row = _rule_row(rule_decisions.decorated_report(pid))
    assert row["violations"] == 2, row
    by_label = {f["label"]: f["iri"] for f in row["focus"]}
    assert set(by_label) == {"README.md", "CLAUDE.md"}, by_label
    return by_label["README.md"], by_label["CLAUDE.md"]


def _decisions(pid: str) -> dict:
    from prism_service.services import rule_decisions

    return json.loads(rule_decisions._decisions_path(pid).read_text(encoding="utf-8"))


def _exempt_both(pid: str) -> tuple[str, str]:
    """Seed, rebuild, then exempt both IRIs with REASON -- the exact call
    api/signals.py makes for the owner's Exempt click."""
    from prism_service.services import rule_decisions

    _seed_two_loose_docs(pid)
    _rebuild(pid)
    iri_readme, iri_claude = _two_focus_iris(pid)
    result = rule_decisions.decide(pid, _rule_signal(pid), "exempt", REASON,
                                    [iri_readme, iri_claude])
    assert result["action"] == "exempt"
    return iri_readme, iri_claude


# ---------------------------------------------------------------------------
# AC-1
# ---------------------------------------------------------------------------

def test_exempt_stores_reason_per_iri_in_decisions_json():
    pid = _project_id("exempt-reason")
    iri_readme, iri_claude = _exempt_both(pid)

    entry = _decisions(pid)[RULE]
    assert entry["exempt"] == sorted([iri_readme, iri_claude])
    reasons = entry["exempt_reasons"]
    assert reasons[iri_readme]["reason"] == "temp docs, owner approved"
    assert reasons[iri_claude]["reason"] == "temp docs, owner approved"
    for iri in (iri_readme, iri_claude):
        assert datetime.fromisoformat(reasons[iri]["at"])


# ---------------------------------------------------------------------------
# AC-2 (shape guard -- green today and must stay green)
# ---------------------------------------------------------------------------

def test_exempt_focus_unchanged_after_reason_lands():
    from prism_service.services import rule_decisions

    pid = _project_id("exempt-reason")
    iri_readme, iri_claude = _exempt_both(pid)

    assert rule_decisions.exempt_focus(pid, RULE) == {iri_readme, iri_claude}


# ---------------------------------------------------------------------------
# AC-3
# ---------------------------------------------------------------------------

def test_report_returns_exempted_with_reason():
    from prism_service.services import rule_decisions

    pid = _project_id("exempt-reason")
    iri_readme, iri_claude = _exempt_both(pid)

    row = _rule_row(rule_decisions.decorated_report(pid))
    assert all(f["iri"] not in (iri_readme, iri_claude) for f in row["focus"])
    assert row["violations"] == 0

    exempted = {e["iri"]: e for e in row["exempted"]}
    assert set(exempted) == {iri_readme, iri_claude}
    assert exempted[iri_readme]["label"] == "README.md"
    assert exempted[iri_claude]["label"] == "CLAUDE.md"
    for iri in (iri_readme, iri_claude):
        assert exempted[iri]["reason"] == "temp docs, owner approved"
        assert exempted[iri]["at"]
        assert datetime.fromisoformat(exempted[iri]["at"])


# ---------------------------------------------------------------------------
# AC-4
# ---------------------------------------------------------------------------

def test_second_exempt_replaces_only_its_iri():
    from prism_service.services import rule_decisions

    pid = _project_id("exempt-reason")
    iri_readme, iri_claude = _exempt_both(pid)
    first = _decisions(pid)[RULE]["exempt_reasons"]
    claude_at_before = first[iri_claude]["at"]
    readme_at_before = first[iri_readme]["at"]

    rule_decisions.decide(pid, _rule_signal(pid), "exempt", SECOND_REASON,
                          [iri_readme])

    entry = _decisions(pid)[RULE]
    assert entry["exempt"] == sorted([iri_readme, iri_claude])
    reasons = entry["exempt_reasons"]
    assert reasons[iri_readme]["reason"] == "second reason"
    assert reasons[iri_readme]["at"] >= readme_at_before
    assert reasons[iri_claude]["reason"] == "temp docs, owner approved"
    assert reasons[iri_claude]["at"] == claude_at_before


# ---------------------------------------------------------------------------
# AC-5
# ---------------------------------------------------------------------------

def test_legacy_entry_without_reasons_loads_and_reports():
    from prism_service.services import rule_decisions

    pid = _project_id("exempt-legacy")
    _seed_two_loose_docs(pid)
    _rebuild(pid)
    iri_readme, iri_claude = _two_focus_iris(pid)

    path = rule_decisions._decisions_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({RULE: {"exempt": [iri_readme]}}), encoding="utf-8")

    row = _rule_row(rule_decisions.decorated_report(pid))
    assert row["exempted"] == [
        {"iri": iri_readme, "label": "README.md", "reason": "", "at": ""},
    ]
    assert [f["iri"] for f in row["focus"]] == [iri_claude]
    assert row["violations"] == 1


# ---------------------------------------------------------------------------
# AC-6
# ---------------------------------------------------------------------------

def test_reason_lives_in_decisions_not_signal():
    from prism_service.services.signal_store import SignalStore

    pid = _project_id("exempt-reason")
    iri_readme, _iri_claude = _exempt_both(pid)

    signal = SignalStore(pid).get(_rule_signal(pid).id)
    assert not hasattr(signal, "decision")
    assert "reason" in _decisions(pid)[RULE]["exempt_reasons"][iri_readme]
