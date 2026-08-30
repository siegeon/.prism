"""The red-anchor scanner must read the whole commit message.

OBSERVED LIVE on task fc471aed (2026-08-30). Its tests-only commit carried
the `[task:<id8>]` trailer as its own paragraph in the commit BODY, which is
what this repo's documented git-commit convention produces. `_red_tests_commit`
searched `git log --format=%H%x09%s` -- the SUBJECT LINE only -- so the commit
was invisible to the self-heal.

Consequence: `_red_step_sha` found no replacement candidate and, per its own
documented fallback (dropping a rejected row would make the machine seat
abstain and route red_gate to a human, which it must never do), kept returning
a stale unreachable sha. The only visible symptom was readiness quoting that
old sha's rc=2 verdict forever -- a message pointing nowhere near the cause.
The driver lost ~20 minutes and only found it by reading the scanner's source.

Any driver following the documented convention strands its own red anchor the
same way, silently. Reading the whole message makes BOTH conventions work.
"""

from __future__ import annotations

import subprocess
import uuid


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True,
                          text=True, timeout=20)


def _repo(tmp_path, message: str):
    """A repo whose single tests-only commit carries `message`."""
    r = tmp_path / f"r{uuid.uuid4().hex[:6]}"
    (r / "tests").mkdir(parents=True)
    _git(r, "init", "-q", ".")
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    # A BASE COMMIT FIRST. _commit_is_tests_only uses `git diff-tree`, which
    # emits NOTHING for a root commit (there is no parent to diff against),
    # so a single-commit fixture reports "not tests-only" no matter what it
    # contains. Real repos always have a parent; the fixture must too.
    (r / "README").write_text("base\n")
    _git(r, "add", "README")
    _git(r, "commit", "-q", "-m", "base")
    (r / "tests" / "test_thing.py").write_text("def test_x():\n    assert False\n")
    _git(r, "add", "tests/test_thing.py")
    _git(r, "commit", "-q", "-m", message)
    return str(r)


def _svc(tmp_path, repo):
    from prism_service.services.conductor_service import ConductorService
    svc = ConductorService.__new__(ConductorService)
    svc._project_root = repo
    svc._workspace_and_head = lambda _tid: ("", "")
    return svc


TASK = "fc471aed-de66-4acb-a802-6114438df74a"


def test_a_trailer_in_the_commit_body_is_found(tmp_path):
    """The exact shape this repo's git-commit convention produces."""
    body_style = (
        "test(meter): pin that impossible token values are refused\n"
        "\n"
        "The ingest path stored counts larger than any context window.\n"
        "\n"
        f"[task:{TASK}]\n"
    )
    repo = _repo(tmp_path, body_style)
    svc = _svc(tmp_path, repo)

    sha, found_repo = svc._red_tests_commit(TASK)

    assert sha, (
        "a tests-only commit whose [task:] trailer sits in the BODY must be "
        "found; scanning only %s made the documented commit convention "
        "silently strand every red anchor")
    assert found_repo == repo


def test_a_trailer_on_the_subject_line_still_works(tmp_path):
    """THE GUARD: the other convention must keep working."""
    subject_style = f"test(meter): pin impossible token values [task:{TASK}]\n"
    repo = _repo(tmp_path, subject_style)
    svc = _svc(tmp_path, repo)

    sha, _ = svc._red_tests_commit(TASK)

    assert sha, "a trailer on the subject line must still resolve"


def test_an_unrelated_commit_is_not_claimed(tmp_path):
    """THE OTHER GUARD: reading the whole message must not make the scanner
    greedy. A commit that merely MENTIONS another task is not this task's
    red anchor."""
    other = "test(other): something else [task:99999999-0000-0000-0000-000000000000]\n"
    repo = _repo(tmp_path, other)
    svc = _svc(tmp_path, repo)

    sha, _ = svc._red_tests_commit(TASK)

    assert not sha, (
        f"the scanner must not claim another task's commit -- got {sha!r}")
