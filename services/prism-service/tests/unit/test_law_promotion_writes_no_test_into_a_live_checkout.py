"""Promotion writes no test into a live checkout (task 33a01b88).

services/law_promotion.py's `_write_verification_test()` turns a promoted
SHACL rule's fixture proof into a durable pytest file. It picked the
destination by ASCENDING TO THE NEAREST `.git` from its own module
(task_workspace._prism_repo_root), so the destination was "whatever
checkout this process happens to be imported from" rather than "the
source tree of the project whose law this is".

Two consequences, both observed live:

  * tests/unit/test_memory_promotes_to_law.py drives a full promotion, so
    a plain unit-suite run WROTE into the checkout. The bytes differ on
    every run, because the generated TTL embeds the promoting memory's
    `o:derivedFrom` id and services/memory_service.py mints a fresh
    random id for every `store()` call, so the compare-before-write
    guard could never match. One law carried 13 different derivedFrom ids
    across the checkout and its worktrees, all naming memories that do
    not exist.
  * The resulting dirty file failed the green_gate cleanliness tooth.
    Task 84a91b0b and task f61617c1 both burned on it.

An earlier repair (7.13.171) refused a root that is a TASK WORKTREE. That
is why this file scaffolds a FAKE PRIMARY CHECKOUT (`.git` as a
DIRECTORY) and points the ascend at it: inside a task worktree `.git` is
a FILE, so the old guard fires and the defect is invisible. The primary
checkout is exactly the case the old guard does not cover.

THE FIX UNDER TEST: the destination is an EXPLICIT dependency, not a
discovery. `_write_verification_test(..., dest_root=...)` writes only
where it is told, and `_install_rule()` asks the PROJECT where its source
lives (services/source_service.source_dir_for). A project that names no
real checkout gets no file written anywhere; the real `prism` project,
whose source_path is its own checkout, still gets its regression test.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_LAW_TESTS_RELDIR = "services/prism-service/tests/unit/law"


# ── fixtures (same shapes tests/unit/test_memory_promotes_to_law.py uses) ──

@pytest.fixture
def real_project(monkeypatch):
    """A project under the REAL default PROJECTS_DIR. install()'s
    violating-fixture check runs SHACL in an isolated CHILD PROCESS that
    re-imports prism_service.config fresh, so an in-process monkeypatch of
    cfg.PROJECTS_DIR is invisible to it."""
    from prism_service import project_context as pc

    pc._contexts.clear()
    yield f"law-write-guard-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


@pytest.fixture
def no_real_worktree(tmp_path, monkeypatch):
    """flow_start creates a REAL git worktree by default. Stub it, and the
    behaviour file's location, the same way the promotion suite does."""
    from prism_service.api import conductor_flow as flow
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        flow.task_workspace, "ensure_workspace",
        lambda task_id, **kw: {"path": "/fake/ws", "branch": "x",
                               "baseline": "0" * 40, "repo_root": "/fake"},
    )
    monkeypatch.setattr(
        workflows_api, "_behavior_file",
        lambda project, workflow: tmp_path / ".prism" / "behaviors" / f"{workflow}.json",
    )


def _fake_primary_checkout(root: Path) -> Path:
    """A directory that looks like a PRIMARY PRISM checkout: `.git` is a
    DIRECTORY (a linked worktree has it as a FILE, which is what the
    7.13.171 guard keys on), and the law tests directory the generator
    targets already exists."""
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / _LAW_TESTS_RELDIR).mkdir(parents=True, exist_ok=True)
    (root / _LAW_TESTS_RELDIR / "__init__.py").write_text("", encoding="utf-8")
    return root


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


def _principle_memory(project: str, **overrides):
    from prism_service.project_context import get_project

    kwargs = dict(
        domain="architecture-principles",
        name="ARC-WRITE-GUARD",
        description="The API layer must not import the engines layer directly.",
        type="decision",
        classification="foundational",
        evidence={"principle": {
            "from": "prism_service/api",
            "must_not_depend_on": ["prism_service/engines"],
            "why": "the API layer must not import the engines layer directly",
        }},
        importance=8,
    )
    kwargs.update(overrides)
    return get_project(project).memory_svc.store(**kwargs)


def _promote(project: str) -> dict:
    """Drive one memory all the way through the promote_to_law run: draft
    -> review -> a DISTINCT actor approves -> install. Exactly the path
    tests/unit/test_memory_promotes_to_law.py drives, which is the path
    that dirtied the checkout."""
    from prism_service.api import conductor_flow as flow
    from prism_service.services import law_promotion

    memory = _principle_memory(project)
    started = law_promotion.start_promotion(project, memory.id)
    assert started["ok"], started
    run_task_id = started["run_task_id"]
    approved = flow.flow_report(flow.Ident(
        task_id=run_task_id, session_id="owner-review",
        outcome="approved", expected_step="review", override=True),
        project=project)
    assert approved["ok"], approved
    result = law_promotion.install_pending(project, task_id=run_task_id)
    assert result["ok"], result
    assert result["install"]["installed"] is True, result
    return {"memory": memory, "draft": started["draft"], "install": result}


# ── AC-1: a suite run writes NOTHING into the enclosing checkout ──────────

def test_a_promotion_run_writes_nothing_into_the_enclosing_checkout(
    tmp_path, monkeypatch, real_project, no_real_worktree,
):
    """oracle: `git status --short services/prism-service/tests/unit/law/`
    prints nothing after a suite run. Pinned here as the stronger claim
    the ticket's likely_misfire asks for -- not "the bytes happen to
    match" but "not one byte was written anywhere in the checkout"."""
    from prism_service.services import law_promotion

    checkout = _fake_primary_checkout(tmp_path / "checkout")
    monkeypatch.setattr(
        law_promotion.task_workspace, "_prism_repo_root", lambda: checkout)

    before = _snapshot(checkout)
    _promote(real_project)
    after = _snapshot(checkout)

    new_files = sorted(set(after) - set(before))
    assert not new_files, (
        f"a promotion driven from a test wrote {new_files} into the "
        f"enclosing checkout at {checkout}; a suite run must leave the "
        "working tree byte-for-byte untouched")
    assert after == before, (
        "a promotion driven from a test rewrote an existing file in the "
        f"enclosing checkout at {checkout}")


def test_two_promotion_runs_leave_the_checkout_identical(
    tmp_path, monkeypatch, real_project, no_real_worktree,
):
    """The ticket's oracle runs the suite TWICE. Each run stores a fresh
    memory, and memory_service mints a NEW random id per store(), so the
    generated TTL's o:derivedFrom differed every pass and the
    compare-before-write guard never matched."""
    from prism_service import project_context as pc
    from prism_service.services import law_promotion

    checkout = _fake_primary_checkout(tmp_path / "checkout")
    monkeypatch.setattr(
        law_promotion.task_workspace, "_prism_repo_root", lambda: checkout)

    before = _snapshot(checkout)
    _promote(real_project)
    pc._contexts.clear()
    _promote(f"{real_project}-second")
    after = _snapshot(checkout)

    assert after == before, (
        "two promotion runs must leave the enclosing checkout identical; "
        f"changed: {sorted(set(after) ^ set(before)) or 'file contents'}")


# ── AC-2: a REAL promotion still installs its regression test ────────────

def test_a_real_promotion_still_installs_its_regression_test(
    tmp_path, monkeypatch, real_project, no_real_worktree,
):
    """The other half of the ticket: a project that DOES name a real
    source checkout still gets its durable regression test, in that
    checkout, with the o:verifiedBy triple recorded against the rule."""
    from prism_service.config import project_data_dir
    from prism_service.services import ontology_rules, source_service

    checkout = _fake_primary_checkout(tmp_path / "project-source")
    source_service.set_source_path(real_project, str(checkout))

    run = _promote(real_project)
    name = run["draft"]["name"]
    slug = name.replace("-", "_")
    rel = f"{_LAW_TESTS_RELDIR}/test_promoted_{slug}.py"
    fn_name = f"test_{slug}_fires_on_violating_and_stays_quiet_on_compliant"

    dest = checkout / rel
    assert dest.exists(), (
        f"a real promotion must still install its regression test at "
        f"{dest}; the project's own source_path is where its law's tests "
        "belong")
    content = dest.read_text(encoding="utf-8")
    assert "RULE_TTL" in content
    assert "VIOLATING_FIXTURE" in content
    assert "COMPLIANT_FIXTURE" in content
    assert f"def {fn_name}" in content

    shapes = (project_data_dir(real_project) / "ontology" /
              "promoted-shapes.ttl").read_text(encoding="utf-8")
    assert f'o:{name} o:verifiedBy "{rel}::{fn_name}" .' in shapes, shapes

    row = next(r for r in ontology_rules.rule_catalog(real_project)
               if r["name"] == name)
    assert row.get("verified_by") == f"{rel}::{fn_name}", row


# ── AC-3: the destination is an explicit parameter, not a discovery ──────

def test_the_destination_is_an_explicit_parameter(tmp_path, monkeypatch):
    """A test must be able to point the writer at a tmp_path. Pinned
    directly on the writer so no future caller can go back to ascending
    to the nearest .git."""
    from prism_service.services import law_promotion

    elsewhere = _fake_primary_checkout(tmp_path / "never-write-here")
    monkeypatch.setattr(
        law_promotion.task_workspace, "_prism_repo_root", lambda: elsewhere)
    dest_root = _fake_primary_checkout(tmp_path / "told-to-write-here")

    ref = law_promotion._write_verification_test(
        "a-told-destination-rule", "o:x a sh:NodeShape .", "o:a a o:Code .",
        "", dest_root=dest_root)

    assert ref == (f"{_LAW_TESTS_RELDIR}/test_promoted_a_told_destination_rule.py"
                   "::test_a_told_destination_rule_fires_on_violating_and_"
                   "stays_quiet_on_compliant")
    assert (dest_root / _LAW_TESTS_RELDIR /
            "test_promoted_a_told_destination_rule.py").exists()
    assert not (elsewhere / _LAW_TESTS_RELDIR /
                "test_promoted_a_told_destination_rule.py").exists(), (
        "the writer used the ascend-to-.git root it was NOT told to use")


def test_no_destination_writes_nothing_but_still_records_the_link(
    tmp_path, monkeypatch,
):
    """`dest_root=None` means "this promotion has no source tree". The
    rule is still installed and still records which test verifies it --
    the o:verifiedBy ref is a stable, repo-relative string, not a claim
    that a file exists on THIS machine."""
    from prism_service.services import law_promotion

    elsewhere = _fake_primary_checkout(tmp_path / "never-write-here")
    monkeypatch.setattr(
        law_promotion.task_workspace, "_prism_repo_root", lambda: elsewhere)
    before = _snapshot(elsewhere)

    ref = law_promotion._write_verification_test(
        "a-rootless-rule", "o:x a sh:NodeShape .", "o:a a o:Code .", "",
        dest_root=None)

    assert ref.endswith(
        "::test_a_rootless_rule_fires_on_violating_and_stays_quiet_on_compliant")
    assert _snapshot(elsewhere) == before, (
        "with no destination the writer must write nothing at all")
