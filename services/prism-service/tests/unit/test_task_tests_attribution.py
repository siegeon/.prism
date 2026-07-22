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
