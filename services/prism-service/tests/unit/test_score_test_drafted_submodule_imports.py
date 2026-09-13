"""score_test_drafted resolves the FULL dotted module path, not only its top.

The live defect (task bb3d1f6a): the drafted test imported
`prism_service.lexicon`, which does not exist — the real module is
`prism_service.services.lexicon`. The checker resolved only the top-level
name `prism_service`, which is real, so the draft was accepted and pytest
then died at collection with rc=4 where red_gate demands rc==1.

Pins:
  (1) the real bb3d1f6a draft is REFUSED and the reason names the full
      dotted module `prism_service.lexicon`;
  (2) a real submodule (`prism_service.ontology`) still PASSES;
  (3) `import os.path` still PASSES;
  (4) a submodule of a package that is NOT already imported in this
      process PASSES conservatively — resolving it would execute that
      package's __init__, and this checker never executes new code;
  (5) refusing a draft does not import the module it names.
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


# The draft committed for task bb3d1f6a, verbatim.
_BB3D1F6A_DRAFT = (
    "def test_prose_keeps_the_words_the_author_wrote():\n"
    "    from prism_service.ontology import Term\n"
    "    from prism_service.lexicon import load_lexicon\n"
    '    assert load_lexicon("This is a test.") == "This is a test."\n'
)


# ── (1) The real bb3d1f6a draft is refused, by its full dotted name ─────

def test_the_real_bb3d1f6a_draft_is_refused():
    g = _gov()
    result = g.score_test_drafted(
        {"test_code": _BB3D1F6A_DRAFT,
         "test_file_path": "tests/unit/test_prose_keeps_the_words.py"},
        _rubrics().get("test_drafted", {}),
    )
    assert result["ok"] is False, f"Expected refusal but got: {result}"
    assert "prism_service.lexicon" in result["reason"], result["reason"]


def test_the_refusal_reason_does_not_stop_at_the_top_level_name():
    """The reason must name the module that does not exist, not the package
    that does — a driver told only "prism_service" cannot self-diagnose."""
    g = _gov()
    result = g.score_test_drafted(
        {"test_code": (
            "from prism_service.lexicon import load_lexicon\n\n"
            "def test_x():\n"
            "    assert load_lexicon('a') == 'a'\n"),
         "test_file_path": "tests/unit/test_x.py"},
        _rubrics().get("test_drafted", {}),
    )
    assert result["ok"] is False
    assert "unresolvable" in result["reason"].lower()
    assert "prism_service.lexicon" in result["reason"]


# ── (2)(3) Real submodules still pass ──────────────────────────────────

def test_a_real_submodule_of_this_package_passes():
    g = _gov()
    result = g.score_test_drafted(
        {"test_code": (
            "from prism_service.ontology import Term\n\n"
            "def test_term_exists():\n"
            "    assert Term is not None\n"),
         "test_file_path": "tests/unit/test_term.py"},
        _rubrics().get("test_drafted", {}),
    )
    assert result["ok"] is True, f"Expected pass but got: {result}"


# ── (4) An un-imported package's submodule passes conservatively ───────

def _a_resolvable_package_not_yet_imported() -> str:
    """A stdlib package that exists on disk but is absent from sys.modules."""
    import importlib.util
    for name in ("wsgiref", "turtledemo", "xmlrpc", "concurrent", "html",
                 "sqlite3", "unittest", "curses"):
        if name in sys.modules:
            continue
        try:
            if importlib.util.find_spec(name) is not None:
                return name
        except Exception:
            continue
    return ""


def test_submodule_of_an_unimported_package_passes():
    """Resolving `pkg.sub` imports `pkg`, so the checker must not try when
    `pkg` is absent from sys.modules — it passes instead."""
    import pytest
    pkg = _a_resolvable_package_not_yet_imported()
    if not pkg:
        pytest.skip("every candidate stdlib package is already imported")
    g = _gov()
    result = g.score_test_drafted(
        {"test_code": (
            f"import {pkg}.definitely_not_a_real_submodule_xyz\n\n"
            "def test_x():\n"
            "    assert True\n"),
         "test_file_path": "tests/unit/test_x.py"},
        _rubrics().get("test_drafted", {}),
    )
    assert result["ok"] is True, f"Expected pass but got: {result}"
    assert pkg not in sys.modules, f"the checker imported {pkg}"


def test_import_os_path_passes():
    g = _gov()
    result = g.score_test_drafted(
        {"test_code": (
            "import os.path\n\n"
            "def test_join():\n"
            "    assert os.path.join('a', 'b')\n"),
         "test_file_path": "tests/unit/test_join.py"},
        _rubrics().get("test_drafted", {}),
    )
    assert result["ok"] is True, f"Expected pass but got: {result}"


# ── (5) The refused module is never imported ───────────────────────────

def test_refusing_a_draft_does_not_import_the_named_module():
    g = _gov()
    sys.modules.pop("prism_service.lexicon", None)
    g.score_test_drafted(
        {"test_code": _BB3D1F6A_DRAFT,
         "test_file_path": "tests/unit/test_prose_keeps_the_words.py"},
        _rubrics().get("test_drafted", {}),
    )
    assert "prism_service.lexicon" not in sys.modules
