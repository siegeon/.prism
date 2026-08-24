"""GET /api/tasks/{id}/tests must attribute a test file by OWNERSHIP, not by
mention (task e0149f1f).

Regression: the gate panel for task 5a6837a0 listed 33 tests over three files
of which only 12 were its own. The discovery predicate was a naive substring
scan over the whole file, so any test that merely CITED another task in prose
("see task <id>", "Regression: task <id>") was attributed to the cited task.
A gate panel could therefore show the owner ANOTHER ticket's tests as if they
were this ticket's.

The rule these tests pin: a file belongs to the task named FIRST in its module
docstring. A later citation anywhere — docstring or body — never transfers
ownership.
"""
OWNER = "e0149f1f-07c0-4921-a191-e4af7f1b6b39"
OTHER = "5a6837a0-a6cf-4bc3-9eed-0b2a43217d98"


def file_owns_task(source: str, task_id: str) -> bool:
    """Import at CALL time so the missing symbol fails each test as a
    FAILURE (rc=1, what the red seat anchors on), not a collection error."""
    from prism_service.api import tasks as _t
    return _t.file_owns_task(source, task_id)


def test_a_body_citation_does_not_steal_the_file():
    src = ('"""Pin the gate tooth (task %s)."""\n'
           "# see task %s for the incident\n"
           "def test_x():\n    assert True\n" % (OWNER, OTHER))
    assert file_owns_task(src, OWNER) is True
    assert file_owns_task(src, OTHER) is False


def test_a_docstring_citation_does_not_steal_the_file():
    """The real shape: the owner is named first, the incident cited after."""
    src = ('"""Pin the gate tooth (task %s).\n\n'
           'Regression: task %s closed on a foreign tree.\n"""\n'
           "def test_x():\n    assert True\n" % (OWNER, OTHER))
    assert file_owns_task(src, OWNER) is True
    assert file_owns_task(src, OTHER) is False


def test_short_id_form_is_honoured():
    src = ('"""Pin the gate tooth (task %s)."""\n'
           "def test_x():\n    assert True\n" % OWNER[:8])
    assert file_owns_task(src, OWNER) is True


def test_a_file_naming_no_task_is_owned_by_nobody():
    src = '"""Just a test."""\ndef test_x():\n    assert True\n'
    assert file_owns_task(src, OWNER) is False


def test_an_unparseable_file_falls_back_to_mention():
    """Never silently LOSE a task's pins: if we cannot parse a module
    docstring, fall back to the old substring behaviour."""
    src = "def test_x(  :\n  syntax error task %s\n" % OWNER
    assert file_owns_task(src, OWNER) is True


def test_a_file_with_no_docstring_falls_back_to_mention():
    src = "# task %s\ndef test_x():\n    assert True\n" % OWNER
    assert file_owns_task(src, OWNER) is True


# ---------------------------------------------------------------------------
# Live regression, real files (task 3baadd19, 2026-08-24): a docstring that
# credits a task for MOTIVATING a general fix -- "(task <id> qa discovery)"
# -- reads as a non-possessive FIRST mention, so file_owns_task attributed
# these files to 3baadd19's own Evidence tab even though neither file pins
# 3baadd19's own oracle (they pin general conductor_service.py/advance_task
# fixes). Owner caught it live from the Evidence tab's own "pinned tests"
# count: "i dont think you tied the evidence to the run that computed the
# evidence... soo you kinda messed up there." Fixed by rephrasing the
# credit possessively ("task <id>'s own QA pass"); pinned here against the
# REAL on-disk files so the phrasing can't silently drift back.
# ---------------------------------------------------------------------------

_TASK_3BAADD19 = "3baadd19-78af-42b8-a78e-47a4b6f51fc0"


def _service_root():
    from pathlib import Path
    return Path(__file__).resolve().parent.parent.parent


def test_blocked_reason_fix_file_is_not_falsely_attributed_to_3baadd19():
    path = (_service_root() / "tests" / "unit"
            / "test_stale_blocked_reason_clears_once_shipped.py")
    src = path.read_text(encoding="utf-8")
    assert file_owns_task(src, _TASK_3BAADD19) is False, (
        "this file pins a general adjudicate_green_gate fix, not "
        "3baadd19's own oracle -- it must not show up as 'this task's "
        "pinned tests' on 3baadd19's Evidence tab")


def test_demo_evidence_advance_fix_file_is_not_falsely_attributed_to_3baadd19():
    path = (_service_root() / "tests" / "unit"
            / "test_verify_green_state_requires_demo_evidence.py")
    src = path.read_text(encoding="utf-8")
    assert file_owns_task(src, _TASK_3BAADD19) is False, (
        "this file pins a general advance_task fix, not 3baadd19's own "
        "oracle -- it must not show up as 'this task's pinned tests' on "
        "3baadd19's Evidence tab")


def test_acceptance_manifest_guard_file_is_not_falsely_attributed_to_3baadd19():
    """The SAME bug recurred in the very fix for it: the acceptance-
    manifest guard file's own first docstring mention of "task 3baadd19"
    was non-possessive too, so shipping the manifest immediately
    re-inflated the Evidence tab's pinned-test count with test
    INFRASTRUCTURE (assertions about the manifest JSON's own
    well-formedness), not anything that pins 3baadd19's own oracle."""
    path = (_service_root() / "tests" / "acceptance"
            / "test_green_gate_evidence_ui_acceptance_manifest.py")
    src = path.read_text(encoding="utf-8")
    assert file_owns_task(src, _TASK_3BAADD19) is False, (
        "this file is test infrastructure (manifest well-formedness "
        "checks), not 3baadd19's own oracle pins -- it must not show up "
        "as 'this task's pinned tests' on 3baadd19's Evidence tab")
