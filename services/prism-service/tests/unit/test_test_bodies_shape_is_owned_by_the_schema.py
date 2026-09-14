"""test_bodies' shape is owned by the schema, not by haiku's escaping
(task bb3d1f6a, 2026-09-14).

Three consecutive live passes after 7.13.365 returned test_bodies as a
JSON-ENCODED STRING that failed json.loads (an unescaped docstring quote at
char 306), with `name` copied from the pinned pytest id (`path::test_x`)
and a body that restated the fixed `def` line. Each pass was refused with
a bare "not a JSON list" and the next attempt learned nothing. Meanwhile
the single engine slot starved every other task.

Pins:
- AC-1  the node schema declares test_bodies as an ARRAY of {name, body}
        objects, and the compose text says bare name / no def line.
- AC-2  a `path::name` entry resolves to the bare pinned name.
- AC-3  a body that restates `def name():` is unwrapped and dedented.
- AC-4  a {name: body} dict is accepted as the same information.
- AC-5  an invalid encoded string is refused WITH the parse error and the
        head of what arrived (refusal-recall feeds this to the next pass).
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism_service.api import workflows as wf

_NODE = (Path(__file__).resolve().parents[4]
         / ".prism" / "behaviors" / "conductor" / "write-failing-tests-loop.json")
_PINNED = "test_prose_keeps_the_words_the_author_wrote"


@pytest.fixture
def scaffold(monkeypatch):
    resp = SimpleNamespace(pinned_file="tests/unit/test_x.py",
                           required_test_names=[_PINNED],
                           resolved_imports=[])
    monkeypatch.setattr(wf, "workflow_step_test_scaffold",
                        lambda body, project: resp)
    return resp


# AC-1 ------------------------------------------------------------------
def test_the_node_schema_is_an_array_of_name_body_objects():
    node = json.loads(_NODE.read_text(encoding="utf-8"))
    loop = next(s for s in node["steps"] if s["id"] == "loop")
    body = json.loads(loop["body"].replace("${prompt}", "P")
                      .replace("${taskId}", "I"))
    tb = body["json_schema"]["properties"]["test_bodies"]
    assert tb["type"] == "array"
    assert tb["items"]["required"] == ["name", "body"]
    assert node["version"] >= 14


def test_the_compose_text_asks_for_bare_names_and_no_def_line():
    out = wf.workflow_step_red_prompt_compose(
        wf.RedPromptComposeRequest(task_id="t", task_hint="hint"),
        project="prism")
    prompt = out["prompt"] if isinstance(out, dict) else out.prompt
    assert "no def line" in prompt
    assert "no path, no ::" in prompt
    assert "JSON-encoded string" not in prompt


# AC-2 / AC-3 -------------------------------------------------------------
def test_a_pytest_id_name_and_a_restated_def_are_normalised(scaffold):
    fields = {"test_bodies": [{
        "name": f"services/prism-service/tests/unit/test_x.py::{_PINNED}",
        "body": (f"def {_PINNED}():\n"
                 "    from prism_service.services.lexicon import align\n"
                 "    assert align('skill') == 'skill'\n"),
    }]}
    out = wf._assemble_test_draft("prism", "bb3d1f6a", fields)
    assert out["ok"] is True, out
    code = out["test_code"]
    assert code.count(f"def {_PINNED}():") == 1, code
    assert "    assert align('skill') == 'skill'" in code
    assert "        assert" not in code, "no double indentation"


def test_bare_test_name_edge_cases():
    assert wf._bare_test_name("a/b.py::test_k") == "test_k"
    assert wf._bare_test_name("test_k()") == "test_k"
    assert wf._bare_test_name("test_k") == "test_k"
    assert wf._bare_test_name("not a name!") == ""


# AC-4 ------------------------------------------------------------------
def test_a_name_to_body_dict_is_accepted(scaffold):
    fields = {"test_bodies": {_PINNED: "assert 1 == 2\n"}}
    out = wf._assemble_test_draft("prism", "bb3d1f6a", fields)
    assert out["ok"] is True, out
    assert "assert 1 == 2" in out["test_code"]


# AC-5 ------------------------------------------------------------------
def test_an_invalid_encoded_string_is_refused_with_what_arrived(scaffold):
    live_shape = ('[{"name":"tests/unit/test_x.py::' + _PINNED +
                  '","body":"    """Demonstrates AC-1"""\\n    assert 1"}]')
    out = wf._assemble_test_draft("prism", "bb3d1f6a",
                                  {"test_bodies": live_shape})
    assert out["ok"] is False
    reason = out["reason"]
    assert reason.startswith("test_drafted: test_bodies is a string that is not valid JSON")
    assert "Expecting" in reason, reason
    assert "starts with" in reason and "tests/unit/test_x.py" in reason
