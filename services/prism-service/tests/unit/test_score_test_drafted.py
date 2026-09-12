"""PURE rubric scorer for write_failing_tests — test_drafted validation.

Pins that score_test_drafted:
  (1) parses test_code as valid Python (SyntaxError -> ok=False);
  (2) requires at least one function starting with test_;
  (3) requires at least one assert statement (via AST walk, not regex);
  (4) validates test_file_path: non-empty, ends with .py, basename starts
      with test_;
  (5) REFUSES a test that would raise before any assert runs (bare .index(
      or bare subscript).
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


def _rubrics():
    return _gov().load_rubrics()


# ── (1) valid Python syntax ────────────────────────────────────────────────

def test_syntax_error_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo( :",  # syntax error
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "parse as Python" in result["reason"]


def test_valid_python_parses():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


# ── (2) at least one test_* function ───────────────────────────────────────

def test_no_test_function_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def foo():\n    assert True",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "no function starting with test_" in result["reason"]


def test_one_test_function_passes():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


def test_multiple_test_functions_pass():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": ("def test_a():\n    assert 1 == 1\n"
                      "def test_b():\n    assert 2 == 2"),
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


# ── (3) at least one assert statement (AST, not regex) ─────────────────────

def test_no_assert_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    x = 1 + 1",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "no assert statement" in result["reason"]


def test_assert_in_comment_not_counted():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    # assert True\n    x = 1",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "no assert statement" in result["reason"]


def test_assert_in_string_not_counted():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": 'def test_foo():\n    s = "assert True"\n    x = 1',
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "no assert statement" in result["reason"]


def test_real_assert_passes():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert 1 + 1 == 2",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


# ── (4) valid file path ────────────────────────────────────────────────────

def test_empty_file_path_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "test_file_path is empty" in result["reason"]


def test_path_not_ending_py_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "tests/test_example.txt",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "must end with .py" in result["reason"]


def test_basename_not_starting_test_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "tests/example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "must start with test_" in result["reason"]


def test_valid_path_unix_style():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "tests/unit/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


def test_valid_path_windows_style():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "def test_foo():\n    assert True",
        "test_file_path": "tests\\unit\\test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


# ── (5) reject tests that would raise before any assert ────────────────────

def test_bare_index_call_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": ("def test_foo():\n"
                      "    items = [1, 2]\n"
                      "    items.index(99)\n"
                      "    assert True"),
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert ".index() call" in result["reason"]


def test_bare_subscript_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": ("def test_foo():\n"
                      "    d = {'a': 1}\n"
                      "    d['nonexistent']\n"
                      "    assert True"),
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "bare subscript" in result["reason"]


def test_index_call_in_assignment_not_rejected():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": ("def test_foo():\n"
                      "    items = [1, 2, 3]\n"
                      "    x = items.index(2)\n"
                      "    assert x == 1"),
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


def test_subscript_in_assignment_not_rejected():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": ("def test_foo():\n"
                      "    d = {'a': 1}\n"
                      "    x = d['a']\n"
                      "    assert x == 1"),
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True


# ── empty test code ───────────────────────────────────────────────────────

def test_empty_test_code_returns_ok_false():
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": "",
        "test_file_path": "tests/test_example.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is False
    assert "test_code is empty" in result["reason"]


# ── realistic passing case from real repo ──────────────────────────────────

def test_realistic_passing_test():
    """Assert a real test pattern from this repo."""
    g = _gov()
    rubric = _rubrics().get("test_drafted", {})
    evidence = {
        "test_code": (
            "def test_rubrics_live_as_yaml_data():\n"
            "    import yaml\n"
            "    hits = [p for p in (_SERVICE_ROOT / 'prism_service').rglob('*.y*ml')\n"
            "            if 'rubric' in p.name.lower()]\n"
            "    assert hits, 'no rubrics YAML data file shipped'\n"
            "    data = yaml.safe_load(hits[0].read_text(encoding='utf-8'))\n"
            "    assert 'story_complete' in data and 'plan_coverage' in data"
        ),
        "test_file_path": "tests/unit/test_arc_governance_rubric_gates.py",
    }
    result = g.score_test_drafted(evidence, rubric)
    assert result["ok"] is True
