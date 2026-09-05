"""The plan_subject tooth flags a foreign TASK, not a commit sha
(task a65c66e5, 2026-09-04).

`_plan_subject_problems` matched any bare 8-hex run in the plan's first
line and called it a foreign task id. An abbreviated commit sha has
exactly that shape, and a plan legitimately cites one — the commit it
builds on, or the commit that superseded a step it is dropping.

LIVE REGRESSIONS, three in one evening, each costing a gate round-trip on
a plan whose CONTENT was correct:
  * a2bc8c88's plan opened "cause one marked SUPERSEDED by 0924ac24"
  * ce471e06's plan cited its base commit
  * 338f7810's plan cited 369766ab, and parked at plan_gate for it

A token now counts as a task reference only when the line PRESENTS it as
one: `task <id>`, `task:<id>`, `[task:<id>]`, `#<id>`. The tooth still
refuses a subject that says it is about a different task, which is the
case it exists for.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

OWN = "338f7810-d8f6-4860-8edc-582b1cf8b9f2"
OTHER = "ce471e06-bbbb-4db6-8ac9-2eb1b9f287c8"


def _problems(first_line: str, task_id: str = OWN):
    from prism_service.services.arc_governance import _plan_subject_problems

    return _plan_subject_problems(first_line + "\n\n## Summary\nbody\n",
                                  task_id)


def test_a_commit_sha_in_the_subject_is_not_a_foreign_task():
    """The live 338f7810 shape: the plan cites the commit it builds on."""
    got = _problems("Plan for task 338f7810. Rebased onto 369766ab.")

    assert got == [], f"a commit sha must not read as a foreign task: {got}"


def test_a_superseded_by_commit_line_passes():
    """The live a2bc8c88 shape, verbatim in spirit."""
    got = _problems("Plan for task 338f7810 (cause one marked SUPERSEDED "
                    "by 0924ac24, dev 6cafa3d4).")

    assert got == []


def test_a_subject_naming_another_task_is_still_refused():
    """The tooth's real purpose, kept: a subject about a DIFFERENT task."""
    got = _problems(f"Plan for task {OTHER[:8]} — the flow view.")

    assert got, "a subject naming another task must still be refused"
    assert OTHER[:8] in got[0]


def test_the_bracketed_trailer_form_is_still_caught():
    got = _problems(f"Continues [task:{OTHER[:8]}] from earlier tonight.")

    assert got and OTHER[:8] in got[0]


def test_the_hash_form_is_still_caught():
    got = _problems(f"Follow-up to #{OTHER[:8]} raised at the gate.")

    assert got and OTHER[:8] in got[0]


def test_the_tasks_own_id_is_never_foreign():
    got = _problems(f"Plan for task {OWN[:8]} — claim teardown.")

    assert got == []


def test_the_contaminated_clause_is_untouched():
    """The tooth's second half must keep working — this change narrows
    only the id match."""
    got = _problems("Replacement for the contaminated plan.")

    assert any("contaminated" in g for g in got)
