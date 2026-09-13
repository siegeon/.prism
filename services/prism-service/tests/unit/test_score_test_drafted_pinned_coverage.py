"""A drafted test must define EVERY test function the task pins for its file.

WHAT WAS WRONG (task bb3d1f6a, observed live). The task pinned TWO pytest
ids in `task.verify`, both in the same file. The write-failing-tests node
drafted a file that defined only ONE of the two functions. `test_drafted`
passed the draft, write-test-file wrote it, commit-tests-only committed it,
and pytest then died at COLLECTION with rc=4 ("no tests ran" for the id that
was never written) where red_gate requires rc==1. Every fact needed to
refuse the draft was already on the task row and was never read.

THE FIX. `score_test_drafted` learns an optional evidence key `pinned_ids`.
When the pinned ids name functions in the SAME file as the draft, every one
of those names must be defined by the draft or the verdict refuses and NAMES
the missing function(s), so the driving agent can self-diagnose instead of
discovering it at collection time. `_score_rubric` threads the task's
`verify` list in.

CONSERVATIVE BY RULE. No pinned ids, an unresolvable task, a pinned entry
with no `::`, or a pinned path that does not match the draft's path all
require nothing -- a checker that refuses good tests is worse than no
checker.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_FILE = "services/prism-service/tests/unit/test_ste_prose.py"
_ID_ONE = _FILE + "::test_prose_keeps_the_words_the_author_wrote"
_ID_TWO = _FILE + "::test_a_semicolon_and_a_contraction_still_go"

_ONLY_ONE_DEFINED = (
    "def test_prose_keeps_the_words_the_author_wrote():\n"
    "    assert 1 == 1\n"
)
_BOTH_DEFINED = (
    "def test_prose_keeps_the_words_the_author_wrote():\n"
    "    assert 1 == 1\n"
    "\n"
    "def test_a_semicolon_and_a_contraction_still_go():\n"
    "    assert 2 == 2\n"
)


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics().get("test_drafted", {})


# ── the live defect: a second pinned id the draft never defines ────────────

def test_a_draft_missing_a_second_pinned_id_is_refused():
    """The exact bb3d1f6a shape. rc=4 at collection must become a refusal
    HERE, with the missing function named."""
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE,
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is False, (
        "a draft that defines 1 of 2 pinned ids must be refused -- pytest "
        "exits 4 at collection on the missing one and red_gate wants rc==1")
    assert "test_a_semicolon_and_a_contraction_still_go" in result["reason"], (
        "the refusal must NAME the missing function so the driving agent "
        f"can self-diagnose: {result['reason']!r}")


def test_a_draft_defining_every_pinned_id_passes():
    result = _gov().score_test_drafted(
        {"test_code": _BOTH_DEFINED, "test_file_path": _FILE,
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is True, result["reason"]


def test_the_pass_reason_states_how_many_pinned_ids_were_covered():
    result = _gov().score_test_drafted(
        {"test_code": _BOTH_DEFINED, "test_file_path": _FILE,
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is True
    assert "2" in result["reason"] and "pinned" in result["reason"], (
        "the run log must show the coverage check actually ran: "
        f"{result['reason']!r}")


# ── conservative: absent / empty / unmatched pinned ids require nothing ────

def test_no_pinned_ids_key_behaves_exactly_as_today():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE},
        _rubric())
    assert result["ok"] is True, result["reason"]


def test_an_empty_pinned_ids_list_requires_nothing():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE,
         "pinned_ids": []},
        _rubric())
    assert result["ok"] is True, result["reason"]


def test_a_pinned_id_in_another_file_requires_nothing():
    """A path mismatch is a different concern and must not refuse here."""
    other = "services/prism-service/tests/unit/test_somewhere_else.py"
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE,
         "pinned_ids": [other + "::test_not_in_this_draft"]},
        _rubric())
    assert result["ok"] is True, result["reason"]
    assert "no pinned" in result["reason"], (
        "the pass reason must say no pinned id named this file: "
        f"{result['reason']!r}")


def test_a_pinned_entry_with_no_double_colon_pins_a_file_only():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE,
         "pinned_ids": [_FILE]},
        _rubric())
    assert result["ok"] is True, result["reason"]


def test_a_pinned_ids_value_that_is_not_a_list_requires_nothing():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE,
         "pinned_ids": "not-a-list"},
        _rubric())
    assert result["ok"] is True, result["reason"]


# ── id shapes: parametrisation suffix, class scope, path forms ─────────────

def test_a_parametrised_pinned_id_matches_the_bare_function():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE,
         "pinned_ids": [_ID_ONE + "[case-1]"]},
        _rubric())
    assert result["ok"] is True, result["reason"]


def test_a_class_scoped_pinned_id_takes_the_last_segment():
    code = ("class TestProse:\n"
            "    def test_prose_keeps_the_words_the_author_wrote(self):\n"
            "        assert 1 == 1\n")
    result = _gov().score_test_drafted(
        {"test_code": code, "test_file_path": _FILE,
         "pinned_ids": [_FILE + "::TestProse"
                        "::test_prose_keeps_the_words_the_author_wrote"]},
        _rubric())
    assert result["ok"] is True, result["reason"]


def test_a_workspace_relative_pinned_path_matches_a_shorter_draft_path():
    """task.verify is workspace-root-relative; the draft path may be
    repo-relative. The suffix must match in EITHER direction."""
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED,
         "test_file_path": "tests/unit/test_ste_prose.py",
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is False, (
        "the workspace-relative pinned path must still be recognised as "
        "the draft's own file")
    assert "test_a_semicolon_and_a_contraction_still_go" in result["reason"]


def test_a_windows_separator_draft_path_still_matches():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED,
         "test_file_path": r"tests\unit\test_ste_prose.py",
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is False, result["reason"]
    assert "test_a_semicolon_and_a_contraction_still_go" in result["reason"]


def test_a_basename_only_draft_path_matches_on_the_basename():
    result = _gov().score_test_drafted(
        {"test_code": _ONLY_ONE_DEFINED,
         "test_file_path": "test_ste_prose.py",
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is False, result["reason"]
    assert "test_a_semicolon_and_a_contraction_still_go" in result["reason"]


# ── ordering: the earlier checks still speak first ─────────────────────────

def test_a_syntax_error_still_reports_the_parse_failure_not_coverage():
    result = _gov().score_test_drafted(
        {"test_code": "def test_foo( :", "test_file_path": _FILE,
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is False
    assert "parse as Python" in result["reason"], result["reason"]


def test_a_draft_with_no_test_function_still_reports_that_first():
    result = _gov().score_test_drafted(
        {"test_code": "def helper():\n    assert True\n",
         "test_file_path": _FILE,
         "pinned_ids": [_ID_ONE, _ID_TWO]},
        _rubric())
    assert result["ok"] is False
    assert "no function starting with test_" in result["reason"], (
        result["reason"])


# ── threading: _score_rubric reads the task's verify list ──────────────────

def _fake_project(task):
    return types.SimpleNamespace(
        brain_svc=None, memory_svc=None, workflow_svc=None, governance=None,
        task_svc=types.SimpleNamespace(get=lambda _id: task))


def test_score_rubric_threads_the_tasks_verify_into_the_scorer(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = types.SimpleNamespace(verify=[_ID_ONE, _ID_TWO])
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: _fake_project(task))

    verdict = workflows_api._score_rubric(
        "test_drafted",
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE},
        "prism", task_id="bb3d1f6a")

    assert verdict["ok"] is False, (
        "_score_rubric must pass task.verify to the scorer -- otherwise the "
        "coverage tooth can never fire on a real drive")
    assert "test_a_semicolon_and_a_contraction_still_go" in verdict["reason"]


def test_score_rubric_passes_through_when_the_task_has_an_empty_verify(
        monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = types.SimpleNamespace(verify=[])
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: _fake_project(task))

    verdict = workflows_api._score_rubric(
        "test_drafted",
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE},
        "prism", task_id="bb3d1f6a")
    assert verdict["ok"] is True, verdict["reason"]


def test_score_rubric_passes_through_when_the_task_cannot_be_resolved(
        monkeypatch):
    """task_svc is None, or the id is unknown: never raise, never refuse."""
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: types.SimpleNamespace(
                            brain_svc=None, memory_svc=None, workflow_svc=None,
                            governance=None, task_svc=None))

    verdict = workflows_api._score_rubric(
        "test_drafted",
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE},
        "prism", task_id="bb3d1f6a")
    assert verdict["ok"] is True, verdict["reason"]


def test_score_rubric_with_no_task_id_requires_no_coverage(monkeypatch):
    from prism_service.api import workflows as workflows_api

    task = types.SimpleNamespace(verify=[_ID_ONE, _ID_TWO])
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: _fake_project(task))

    verdict = workflows_api._score_rubric(
        "test_drafted",
        {"test_code": _ONLY_ONE_DEFINED, "test_file_path": _FILE},
        "prism")
    assert verdict["ok"] is True, verdict["reason"]


# ── end to end: the node's own Validate stage refuses the draft ────────────

def test_reason_loop_refuses_a_draft_that_misses_a_pinned_id(
        tmp_path, monkeypatch):
    from prism_service.api import workflows as workflows_api
    from prism_service.inference import claude_cli

    task = types.SimpleNamespace(verify=[_ID_ONE, _ID_TWO], workflow_step="")
    monkeypatch.setattr(workflows_api, "get_project",
                        lambda p: _fake_project(task))

    class _FakeContextBuilder:
        def __init__(self, **kw):
            pass

        def build(self, persona, story_file):
            return {"conventions": [], "role_card": {"id": persona}}

    monkeypatch.setattr(workflows_api, "ContextBuilder", _FakeContextBuilder)
    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))

    def _fake_invoke(prompt, *, work_dir, plugin_dir, model, max_budget_usd,
                     max_turns, project, purpose, json_schema, **kw):
        return claude_cli.ClaudeCliResult(
            output_path=tmp_path / "run.jsonl", exit_code=0,
            structured_output={"test_code": _ONLY_ONE_DEFINED,
                               "test_file_path": _FILE},
            usage={"cost_usd": 0.0}, run_id="run-1",
        )

    monkeypatch.setattr(claude_cli, "invoke", _fake_invoke)

    resp = workflows_api.workflow_step_reason_loop(
        workflows_api.ReasonLoopRequest(
            persona="qa", prompt="Draft a failing test.",
            json_schema={"type": "object"}, rubric="test_drafted",
            task_id="bb3d1f6a",
        ),
        project="prism",
    )

    assert resp.validation["ok"] is False, (
        "the node must refuse a draft that misses one of its own pinned ids")
    assert resp.stop_chain is True, (
        "a refused coverage verdict must stop the chain before "
        "write-test-file commits the draft as the red anchor")
    assert "test_a_semicolon_and_a_contraction_still_go" in (
        resp.validation["reason"])
