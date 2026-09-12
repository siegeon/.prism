"""The test_drafted scorer must not REFUSE legitimate tests.

score_test_drafted's pre-assert-raise guard exists to stop a draft that would
exit pytest with rc 2 or 4 instead of the rc 1 the red gate requires. A guard
like that earns its place only while it stays conservative: a checker that
rejects correct tests is worse than no checker, because the node then cannot
draft the very tests the rubric wants.

These pin the two cases found in review of the first implementation:
  * an async test (ast.AsyncFunctionDef) is a real test function;
  * `with pytest.raises(...): d['nope']` is a CORRECT test -- there the raise
    IS the assertion, so the bare-subscript guard must exempt that block.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics().get("test_drafted", {})


def test_an_async_test_function_counts():
    """ast.FunctionDef alone misses AsyncFunctionDef, so a pytest-asyncio test
    would be refused for defining no test_ function."""
    evidence = {
        "test_code": (
            "import pytest\n\n"
            "@pytest.mark.asyncio\n"
            "async def test_the_daemon_answers():\n"
            "    got = await fetch_version()\n"
            "    assert got == '7.13.305'\n"
        ),
        "test_file_path": "tests/unit/test_async_thing.py",
    }
    result = _gov().score_test_drafted(evidence, _rubric())
    assert result["ok"] is True, result["reason"]


def test_a_raise_inside_pytest_raises_is_not_a_refusal():
    """The raise is the assertion; refusing this rejects a correct test."""
    evidence = {
        "test_code": (
            "import pytest\n\n"
            "def test_missing_key_raises():\n"
            "    d = {}\n"
            "    with pytest.raises(KeyError):\n"
            "        d['nope']\n"
            "    assert d == {}\n"
        ),
        "test_file_path": "tests/unit/test_missing_key.py",
    }
    result = _gov().score_test_drafted(evidence, _rubric())
    assert result["ok"] is True, result["reason"]


def test_a_bare_subscript_outside_pytest_raises_is_still_refused():
    """The exemption is scoped to the raises block only. A bare lookup in an
    ordinary body still trips the guard, so the guard keeps its teeth."""
    evidence = {
        "test_code": (
            "def test_reads_a_missing_key():\n"
            "    d = {}\n"
            "    d['nope']\n"
            "    assert True\n"
        ),
        "test_file_path": "tests/unit/test_missing_key.py",
    }
    result = _gov().score_test_drafted(evidence, _rubric())
    assert result["ok"] is False
    assert "raise before any assert" in result["reason"]
