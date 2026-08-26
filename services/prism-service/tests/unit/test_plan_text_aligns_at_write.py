"""Conductor-authored plan text aligns at write (task dc676e24).

plan_doc used to be CHECK-ONLY (task 36283d72): TaskService._apply_ste
ran ste.check(task.plan_doc) and reported findings, but never rewrote
the field, the same posture plan_diagram still holds today. This file
pins the new behaviour: plan_doc now runs through the same
normalize + lexicon.align pass the other flavored fields get
(TaskService._align_plan_doc), while a fenced code block (incl.
```mermaid), a markdown table row, a heading, and an AC-<n>/oracle/
citation bullet line stay byte-identical -- those are exactly the
shapes services.arc_governance's story_complete / plan_coverage rubric
parsers read (arc_governance.py is read-only for this task; not
edited here).

No test computes its expected string by calling the function under
test on its own input (lesson: "a test that asserts stored ==
normalize(input) proves nothing", 5de57583, 2026-08-26) -- each
literal here is hand-derived from ste.py's documented substitution
tables and lexicon's model-lexicon.ttl "ticket" -> "Task" mapping.
"""

from __future__ import annotations

import json as _json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import ste  # noqa: E402

# Three real compliant story/plan fixtures already pinned by the rubric
# red suite (task 8579d49e) -- reused here, not retyped, so a drift in
# either file surfaces immediately as an import error rather than as a
# silently-stale copy.
from tests.unit.test_arc_governance_rubric_gates import (  # noqa: E402
    COMPLIANT_STORY,
    PLAN_OK,
    PRINCIPLES,
    STORY_NESTED_ORACLE_BULLETS,
)


def _mk_service(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


def _gov():
    from prism_service.services import arc_governance

    return arc_governance


# ----------------------------------------------------------------------
# (1) prose aligns outside a fence; the fence survives byte-identical
# ----------------------------------------------------------------------


def test_semicolon_and_synonym_align_outside_fence_and_fence_survives(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="plan align check")

    fence = "```\ndon't touch this; keep it.\n```"
    plan_doc = "See the ticket for details; it can wait.\n\n" + fence

    updated = svc.update(task.id, plan_doc=plan_doc)

    # "; it" -> ". It" (semicolon rule), "ticket" -> "Task" (lexicon
    # align, model-lexicon.ttl altLabel "ticket" -> canonical "Task").
    expected = "See the Task for details. It can wait.\n\n" + fence
    assert updated.plan_doc == expected
    assert fence in updated.plan_doc, "fence must survive byte-identical"


# ----------------------------------------------------------------------
# (2) a ```mermaid block survives
# ----------------------------------------------------------------------


def test_mermaid_block_survives_byte_identical(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="mermaid check")

    fence = "```mermaid\nflowchart TD\n  a --> b\n```"
    plan_doc = f"Explain the flow below; see the diagram.\n\n{fence}\n\nThat covers it."

    updated = svc.update(task.id, plan_doc=plan_doc)

    assert fence in updated.plan_doc, updated.plan_doc


# ----------------------------------------------------------------------
# (3) a markdown table survives
# ----------------------------------------------------------------------


def test_table_row_survives_byte_identical(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="table check")

    # The semicolon inside the table cell would normally trip the
    # semicolon rule -- proves table rows are protected as WHOLE lines,
    # not just structurally recognised.
    table = "| col1 | col2 |\n| --- | --- |\n| a; b | c |"
    plan_doc = f"Compare the options; see the table below.\n\n{table}\n"

    updated = svc.update(task.id, plan_doc=plan_doc)

    assert table in updated.plan_doc, updated.plan_doc


# ----------------------------------------------------------------------
# (4) a heading survives
# ----------------------------------------------------------------------


def test_heading_survives_byte_identical(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="heading check")

    # "Robust" (marketing word), "Utilize" (filler) and a semicolon --
    # all three would be rewritten if this line were not protected.
    heading = "# Robust Plan; Utilize This"
    plan_doc = f"{heading}\n\nDo this in order to finish."

    updated = svc.update(task.id, plan_doc=plan_doc)

    assert updated.plan_doc.splitlines()[0] == heading, updated.plan_doc


# ----------------------------------------------------------------------
# (5) rubric safety: aligned text scores identically to the original
# ----------------------------------------------------------------------


def test_rubric_equivalence_compliant_story(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="rubric equivalence: compliant story",
                       plan_doc=COMPLIANT_STORY)
    aligned = task.plan_doc

    gov = _gov()
    rubric = gov.load_rubrics()["story_complete"]
    ac_section_name = rubric.get("ac_section", "acceptance criteria")

    orig_ac_section = gov._find_section(
        gov._sections(COMPLIANT_STORY), ac_section_name) or ""
    aligned_ac_section = gov._find_section(
        gov._sections(aligned), ac_section_name) or ""
    orig_acs = gov._ac_lines(orig_ac_section)
    aligned_acs = gov._ac_lines(aligned_ac_section)
    assert [i for i, _ in aligned_acs] == [i for i, _ in orig_acs]
    assert aligned_acs == orig_acs, (orig_acs, aligned_acs)

    orig_verdict = gov.score_story_complete({"story_md": COMPLIANT_STORY}, rubric)
    aligned_verdict = gov.score_story_complete({"story_md": aligned}, rubric)
    assert orig_verdict["ok"] is True, orig_verdict
    assert aligned_verdict == orig_verdict


def test_rubric_equivalence_nested_oracle_bullets(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="rubric equivalence: nested oracle bullets",
                       plan_doc=STORY_NESTED_ORACLE_BULLETS)
    aligned = task.plan_doc

    gov = _gov()
    rubric = gov.load_rubrics()["story_complete"]
    ac_section_name = rubric.get("ac_section", "acceptance criteria")

    orig_ac_section = gov._find_section(
        gov._sections(STORY_NESTED_ORACLE_BULLETS), ac_section_name) or ""
    aligned_ac_section = gov._find_section(
        gov._sections(aligned), ac_section_name) or ""
    orig_acs = gov._ac_lines(orig_ac_section)
    aligned_acs = gov._ac_lines(aligned_ac_section)
    assert [i for i, _ in aligned_acs] == ["AC-1", "AC-2"]
    assert aligned_acs == orig_acs, (orig_acs, aligned_acs)

    orig_verdict = gov.score_story_complete(
        {"story_md": STORY_NESTED_ORACLE_BULLETS}, rubric)
    aligned_verdict = gov.score_story_complete({"story_md": aligned}, rubric)
    assert orig_verdict["ok"] is True, orig_verdict
    assert aligned_verdict == orig_verdict


def test_rubric_equivalence_plan_coverage(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="rubric equivalence: plan coverage",
                       plan_doc=PLAN_OK["plan_doc"])
    aligned_plan = task.plan_doc

    gov = _gov()
    rubric = gov.load_rubrics()["plan_coverage"]

    orig_ev = dict(PLAN_OK)
    aligned_ev = dict(PLAN_OK, plan_doc=aligned_plan)

    orig_verdict = gov.score_plan_coverage(orig_ev, rubric, PRINCIPLES)
    aligned_verdict = gov.score_plan_coverage(aligned_ev, rubric, PRINCIPLES)
    assert orig_verdict["ok"] is True, orig_verdict
    assert aligned_verdict == orig_verdict


# ----------------------------------------------------------------------
# (6) the ste_normalise history row carries `before` for plan_doc
# ----------------------------------------------------------------------


def test_ste_normalise_history_row_carries_before_for_plan_doc(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="plan doc history check")

    original = "See the ticket for details; it can wait."
    updated = svc.update(task.id, plan_doc=original)
    assert updated.plan_doc != original, "fixture must actually align"

    rows = svc.history(task.id)
    ste_rows = [h for h in rows if h.action == "ste_normalise"]
    assert ste_rows, [h.action for h in rows]

    details = ste_rows[-1].details
    assert "plan_doc" in details, details
    before = _json.loads(details.split("before=", 1)[1])
    assert before.get("plan_doc") == original, before


# ----------------------------------------------------------------------
# (7) PATCH /api/tasks/{id} accepts premise_notes-only and aligns it
# ----------------------------------------------------------------------


@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    from prism_service import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "test-plan-text-aligns"
    pc._contexts.clear()


def _api_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def test_patch_premise_notes_only_returns_200_and_aligns(project):
    client = _api_client()
    create = client.post(
        "/api/tasks", params={"project": project},
        json={"title": "premise notes patch check"})
    assert create.status_code in (200, 201), create.text
    tid = create.json()["task"]["id"]

    # Found live 2026-08-26: PATCH with premise_notes only returned 400
    # "no fields to update" -- TaskUpdate (api/tasks.py) had no field for
    # it at all, though TaskService.update already accepted it.
    text = "We don't need this in order to proceed; it's fine."
    r = client.patch(
        f"/api/tasks/{tid}", params={"project": project},
        json={"premise_notes": text})
    assert r.status_code == 200, r.text
    body = r.json()

    expected, expected_rules = ste.normalize(text, mode="flavored")
    assert body["task"]["premise_notes"] == expected
    style = body["style"]
    assert set(style["fixed"].get("premise_notes", [])) == set(expected_rules)
