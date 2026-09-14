"""write_failing_tests's `loop` step must never be asked to emit a whole
test FILE inside JSON -- live drives of task bb3d1f6a show a small model
reliably breaking on exactly that ask: an unparseable file, a module that
does not exist, and a draft that defines only 1 of 2 pinned test
functions (pytest then exits 4, and red_gate's rc==1 requirement can
never pass).

THE FIX. The model returns ONLY the per-test assertion BODY, keyed by the
pinned name it belongs to -- `test_bodies`, a JSON-encoded list of
`{"name": ..., "body": ...}` entries -- plus `expected_failure_reason`.
A codified function, `_assemble_test_draft`, builds the actual file from
the test-scaffold's AUTHORITATIVE parts (the pinned path, the exact `def`
lines, the verified imports) and those bodies. A missing or unpinned name
is a REFUSAL naming the name, never a silent drop or invention. The
assembled file is then scored by the EXISTING, unmodified
`arc_governance.score_test_drafted` rubric -- assembly never bypasses it.
"""

from __future__ import annotations

import ast
import json
import types

import pytest

_TASK_ID = "bb3d1f6a-c3ff-488b-a754-010a7705907f"
_PINNED_FILE = ("services/prism-service/tests/unit/"
               "test_creating_a_ticket_swaps_no_words.py")
_NAME_A = "test_prose_keeps_the_words_the_author_wrote"
_NAME_B = "test_a_semicolon_and_a_contraction_still_go"


def _mk_task(**over):
    from prism_service.models.task import Task

    base = dict(
        id=_TASK_ID,
        title="One node adjudicates the vocabulary",
        description="`load_lexicon()` does NOT reach `lexicon.align`.",
        oracle="the vocabulary node reports non-canonical terms",
        status="in_progress",
        verify=[f"{_PINNED_FILE}::{_NAME_A}", f"{_PINNED_FILE}::{_NAME_B}"],
    )
    base.update(over)
    return Task(**base)


class _FakeTaskSvc:
    def __init__(self, task):
        self._task = task

    def get(self, task_id):
        return self._task if task_id == self._task.id else None


class _FakeBrainSvc:
    def __init__(self, rows_by_name=None):
        self._rows_by_name = rows_by_name or {}

    def find_symbol(self, name, kind=None, limit=10):
        return list(self._rows_by_name.get(name, []))


_ALIGN_ROW = {
    "source_file": "prism_service/services/lexicon.py",
    "content": (
        "def align(text: str) -> tuple[str, list[dict]]:\n"
        "    \"\"\"Replace every whole-word synonym.\"\"\"\n"
        "    if not text:\n"
        "        return text, []\n"
    ),
    "entity_name": "align",
}


def _wf_with_project(task, monkeypatch, rows_by_name=None):
    from prism_service.api import workflows as wf

    ctx = types.SimpleNamespace(
        task_svc=_FakeTaskSvc(task), brain_svc=_FakeBrainSvc(rows_by_name))
    monkeypatch.setattr(wf, "get_project", lambda p: ctx)
    return wf


def _bodies(*pairs) -> str:
    """[(name, body), ...] -> the JSON string the model would return."""
    return json.dumps([{"name": n, "body": b} for n, b in pairs])


# ----------------------------------------------------------------------
# The worked example: bb3d1f6a's two real pinned names.
# ----------------------------------------------------------------------

def test_assembles_a_file_defining_every_pinned_name(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch, {"align": [_ALIGN_ROW]})
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: True if dotted == "prism_service.services.lexicon" else None)

    fields = {"test_bodies": _bodies(
        (_NAME_A, "text, marks = align('a and b')\n"
                  "assert marks == [], marks"),
        (_NAME_B, "text, marks = align(\"a; isn't\")\n"
                  "assert marks, 'expected a mark'"),
    ), "expected_failure_reason": "align() does not exist yet"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is True, result
    assert result["test_file_path"] == _PINNED_FILE
    assert f"def {_NAME_A}():" in result["test_code"]
    assert f"def {_NAME_B}():" in result["test_code"]
    assert "from prism_service.services.lexicon import align" in result["test_code"]
    # Must be real, parseable Python -- not just string concatenation.
    ast.parse(result["test_code"])

    from prism_service.services import arc_governance as gov
    verdict = gov.score_test_drafted(
        {"test_code": result["test_code"],
         "test_file_path": result["test_file_path"],
         "pinned_ids": [f"{_PINNED_FILE}::{_NAME_A}",
                        f"{_PINNED_FILE}::{_NAME_B}"]},
        gov.load_rubrics().get("test_drafted", {}))
    assert verdict["ok"] is True, verdict


# ----------------------------------------------------------------------
# Refusals -- named, never silent.
# ----------------------------------------------------------------------

def test_a_missing_pinned_body_refuses_naming_it(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": _bodies((_NAME_A, "assert True")),
              "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False
    assert _NAME_B in result["reason"]


def test_an_unpinned_body_refuses_naming_it(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": _bodies(
        (_NAME_A, "assert True"), (_NAME_B, "assert True"),
        ("test_something_nobody_pinned", "assert True"),
    ), "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False
    assert "test_something_nobody_pinned" in result["reason"]


def test_an_empty_body_for_a_pinned_name_is_a_refusal_not_an_empty_function(
        monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": _bodies((_NAME_A, "   "), (_NAME_B, "assert True")),
              "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False
    assert _NAME_A in result["reason"]


def test_malformed_test_bodies_json_refuses(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": "not valid json {{{",
              "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False
    assert "reason" in result


def test_a_non_list_test_bodies_refuses(monkeypatch):
    """SUPERSEDED IN PART (2026-09-14): a name -> body OBJECT is now a
    legitimate container shape and is normalised, so this no longer
    refuses for being a non-list. `{"not": "a list"}` still refuses --
    because "not" is not one of the pinned names. A shape that is neither
    a list nor an object (a bare string, a number) is covered by
    test_a_shape_that_is_neither_list_nor_object_says_what_it_got.
    """
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": json.dumps({"not": "a list"}),
              "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False
    assert "not" in result["reason"], result


# ----------------------------------------------------------------------
# The path is the scaffold's, never the model's.
# ----------------------------------------------------------------------

def test_the_path_comes_from_the_scaffold_an_empty_model_path_never_refuses(
        monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {
        "test_bodies": _bodies((_NAME_A, "assert True"), (_NAME_B, "assert True")),
        "expected_failure_reason": "x",
        # Noise an old-shaped model output might still carry -- must be
        # ignored entirely, never cause "test_file_path is empty".
        "test_file_path": "", "test_code": "",
    }

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is True, result
    assert result["test_file_path"] == _PINNED_FILE
    assert result["test_file_path"] != ""


def test_no_pinned_file_resolvable_refuses_honestly(monkeypatch):
    wf = _wf_with_project(_mk_task(verify=[]), monkeypatch)
    fields = {"test_bodies": _bodies(("test_anything", "assert True")),
              "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False


# ----------------------------------------------------------------------
# Backward compatible: no test_bodies key at all -- pass through untouched
# (an old cached prompt, or a non-write_failing_tests rubric caller).
# ----------------------------------------------------------------------

def test_no_test_bodies_key_is_a_pass_through(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_code": "def test_x():\n    assert True\n",
              "test_file_path": "tests/unit/test_x.py"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is True
    assert "test_code" not in result
    assert "test_file_path" not in result


# ----------------------------------------------------------------------
# Wired into _score_rubric: assembly happens BEFORE scoring, and mutates
# `fields` in place so reason-loop's export chain picks up the assembled
# code under the SAME test_code/test_file_path names write-test-file
# already interpolates as ${testCode}/${testFilePath}.
# ----------------------------------------------------------------------

def test_score_rubric_assembles_before_scoring_and_mutates_fields(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch, {"align": [_ALIGN_ROW]})
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: True if dotted == "prism_service.services.lexicon" else None)

    fields = {"test_bodies": _bodies(
        (_NAME_A, "assert True"), (_NAME_B, "assert True"),
    ), "expected_failure_reason": "x"}

    verdict = wf._score_rubric("test_drafted", fields, "prism", task_id=_TASK_ID)

    assert verdict["ok"] is True, verdict
    assert fields["test_file_path"] == _PINNED_FILE
    assert f"def {_NAME_A}():" in fields["test_code"]
    assert f"def {_NAME_B}():" in fields["test_code"]


def test_score_rubric_surfaces_the_assembler_refusal(monkeypatch):
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": _bodies((_NAME_A, "assert True")),
              "expected_failure_reason": "x"}

    verdict = wf._score_rubric("test_drafted", fields, "prism", task_id=_TASK_ID)

    assert verdict["ok"] is False
    assert _NAME_B in verdict["reason"]


# ----------------------------------------------------------------------
# End-to-end through /steps/reason-loop: the write step's ${testCode}/
# ${testFilePath} contract must still be fed, from the ASSEMBLED file.
# ----------------------------------------------------------------------

def test_reason_loop_exports_the_assembled_code_for_the_write_step(
        tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli
    from prism_service.services.task_runner import _exported_variables

    ctx = types.SimpleNamespace(
        task_svc=_FakeTaskSvc(_mk_task()),
        brain_svc=_FakeBrainSvc({"align": [_ALIGN_ROW]}),
        memory_svc=None, workflow_svc=None, governance=None)
    monkeypatch.setattr(workflows_api, "get_project", lambda p: ctx)
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda dotted: True if dotted == "prism_service.services.lexicon" else None)

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass

        def build(self, persona, story_file):
            return {"conventions": [], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    model_bodies = _bodies(
        (_NAME_A, "assert True"), (_NAME_B, "assert True"))

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"test_bodies": model_bodies,
                               "expected_failure_reason": "not implemented yet"},
            usage={"cost_usd": 0.0}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="qa", prompt="Draft a failing test.",
            json_schema={"type": "object"}, rubric="test_drafted",
            task_id=_TASK_ID),
        project="prism")

    assert resp.validation["ok"] is True, resp.validation
    assert getattr(resp, "stop_chain", False) is False

    exported = _exported_variables(resp)
    assert exported.get("testFilePath") == _PINNED_FILE
    assert f"def {_NAME_A}():" in exported.get("testCode", "")
    assert f"def {_NAME_B}():" in exported.get("testCode", "")
    assert "${" not in exported.get("testCode", "")


# ----------------------------------------------------------------------
# Liberal in the CONTAINER shape (live defect, 2026-09-14)
# ----------------------------------------------------------------------

def test_a_name_to_body_object_assembles_the_same_file(monkeypatch):
    """THE MEASURED DEFECT. The schema types test_bodies as a STRING
    holding JSON, so a compliant answer is JSON nested inside JSON. Haiku
    answered twice with a shape the assembler rejected, and the drive
    stalled with "test_bodies is not a JSON list". A name -> body mapping
    carries the same information, so it must assemble identically."""
    wf = _wf_with_project(_mk_task(), monkeypatch, {"align": [_ALIGN_ROW]})
    monkeypatch.setattr(
        "prism_service.services.arc_governance._submodule_path_resolvable",
        lambda d: True if d == "prism_service.services.lexicon" else None)

    fields = {"test_bodies": json.dumps({
        _NAME_A: "text, marks = align('a and b')\nassert marks == [], marks",
        _NAME_B: "text, marks = align('a; b')\nassert marks, 'expected'",
    }), "expected_failure_reason": "align() does not exist yet"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is True, result
    assert f"def {_NAME_A}():" in result["test_code"]
    assert f"def {_NAME_B}():" in result["test_code"]
    ast.parse(result["test_code"])


def test_a_shape_that_is_neither_list_nor_object_says_what_it_got(monkeypatch):
    """A refusal that does not name the received shape tells the model
    nothing to change, and the recall block hands that text straight to
    the next attempt."""
    wf = _wf_with_project(_mk_task(), monkeypatch)
    fields = {"test_bodies": "just a sentence, not JSON at all",
              "expected_failure_reason": "x"}

    result = wf._assemble_test_draft("prism", _TASK_ID, fields)

    assert result["ok"] is False
    assert "just a sentence" in result["reason"], result
