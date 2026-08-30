"""Reachability sees a call made through the project-context container.

Task 1edee95c parked at green_gate on 2026-08-30 with:

    Unreachable entry points:
    - BrainService.expertise_coverage

The method WAS wired. `api/brain.py:54` calls `ctx.brain_svc.expertise_coverage()`
inside the `/api/brain/health` route — precisely the production wiring that
slice existed to build.

The miss is in signal (a) of `_has_production_reference`: an explicit call only
counts when the CALLING file imports the DEFINING module. `api/brain.py` imports
`get_project` from `prism_service.project_context` and reaches every service
through the container (`ctx.brain_svc`, `ctx.memory_svc`, `ctx.task_svc`). It
never imports `brain_service`, and never should.

That container is PRISM's dominant architecture, so the gap is not a one-off:
every new service method called the normal way reads as unreachable. A tooth
that refuses correct wiring gets switched off, and this tooth guards the single
most repeated defect in this repo — four mechanisms shipped in one day with no
caller at all (premise_gather, align_language, ClaimService, memory indexing).

The import gate stays for everything else. It is what stops a same-named
stdlib call (`atexit.register`) in a file that never imports the defining
module from false-positiving.
"""

from __future__ import annotations

import subprocess

import pytest

from prism_service.services import reachability_check as rc


@pytest.fixture()
def repo(tmp_path):
    """A miniature repo shaped like PRISM: services/ + api/ + a container.

    It must be a REAL git repo: `_candidate_files` scans with `git ls-files`,
    so a plain directory yields nothing to search and the check answers
    permissively — which silently passes every negative case.
    """
    subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
    root = tmp_path / "services" / "prism-service" / "prism_service"
    (root / "services").mkdir(parents=True)
    (root / "api").mkdir(parents=True)
    (root / "services" / "widget_service.py").write_text(
        "class WidgetService:\n"
        "    def widget_coverage(self) -> int:\n"
        "        return 7\n"
    )
    return tmp_path


def _api(repo, body: str) -> None:
    """Write the calling file AND make git aware of it."""
    p = (repo / "services" / "prism-service" / "prism_service"
         / "api" / "widgets.py")
    p.write_text(body)
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True)


def test_the_fixture_can_actually_refuse(repo):
    """Positive control for the harness itself.

    A directory that is not a git repo makes `_candidate_files` return
    nothing, and the check then answers True for everything — so every
    negative test below would pass without proving anything. This asserts
    the fixture is capable of a False.
    """
    _api(repo, "SOMETHING_ELSE = 1\n")
    assert not rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)


DEFINING = ("services/prism-service/prism_service/services/widget_service.py")


def test_a_call_through_the_container_is_reachable(repo):
    """The exact shape that parked task 1edee95c."""
    _api(repo,
         "from prism_service.project_context import get_project\n"
         "\n"
         "def health(project: str) -> dict:\n"
         "    ctx = get_project(project)\n"
         "    return {'indexed': ctx.widget_svc.widget_coverage()}\n")

    assert rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2), (
        "a call through the project-context container is real production wiring")


def test_the_container_import_may_be_the_module_form(repo):
    """`import project_context` counts as well as `from ... import get_project`."""
    _api(repo,
         "from prism_service import project_context\n"
         "\n"
         "def health(project: str) -> dict:\n"
         "    ctx = project_context.get_project(project)\n"
         "    return {'n': ctx.widget_svc.widget_coverage()}\n")

    assert rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)


def test_a_truly_unwired_symbol_is_still_refused(repo):
    """The tooth must keep catching the defect it exists for.

    Four mechanisms shipped on 2026-08-30 with no caller. If this case ever
    passes, the tooth is decoration.
    """
    _api(repo,
         "from prism_service.project_context import get_project\n"
         "\n"
         "def health(project: str) -> dict:\n"
         "    ctx = get_project(project)\n"
         "    return {'n': ctx.widget_svc.something_else()}\n")

    assert not rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)


def test_a_same_named_call_without_the_container_import_stays_refused(repo):
    """The import gate's original purpose survives.

    A file that imports neither the defining module nor the container must
    not satisfy the check merely by calling a same-named function — that is
    the `atexit.register` false positive the gate was built to stop.
    """
    _api(repo,
         "import atexit\n"
         "\n"
         "def wire() -> None:\n"
         "    atexit.widget_coverage()\n")

    assert not rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)


def test_the_container_path_requires_an_attribute_call(repo):
    """A bare mention of the name is not a call."""
    _api(repo,
         "from prism_service.project_context import get_project\n"
         "\n"
         "NOTES = 'widget_coverage is planned'\n")

    assert not rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)


def test_the_live_case_resolves(repo):
    """Regression named for the task it unblocked.

    BrainService.expertise_coverage, called at api/brain.py:54 as
    ctx.brain_svc.expertise_coverage().
    """
    _api(repo,
         "from prism_service.project_context import get_project\n"
         "from fastapi import Query\n"
         "\n"
         "def health(project: str = Query('default')) -> dict:\n"
         "    ctx = get_project(project)\n"
         "    indexed = ctx.widget_svc.widget_coverage()\n"
         "    return {'indexed': indexed}\n")

    assert rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)


def test_an_arbitrary_attribute_call_is_not_container_wiring(repo):
    """The receiver must be a SERVICE attribute, not any attribute.

    REGRESSION: a first cut accepted any `.symbol(` in a file importing
    get_project. That made `WorkItemSync.register` — the original defect
    this whole tooth was built to catch, pinned by
    test_green_gate_requires_reachability — read as reachable, because some
    unrelated object elsewhere is called `.register(`. Loosening a tooth
    until it stops catching its own founding case is worse than no tooth.
    """
    _api(repo,
         "from prism_service.project_context import get_project\n"
         "\n"
         "def wire(project: str) -> None:\n"
         "    ctx = get_project(project)\n"
         "    some_unrelated_thing.widget_coverage()\n")

    assert not rc._has_production_reference(
        "widget_coverage", DEFINING, repo, defining_lineno=2)
