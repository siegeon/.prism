"""Python symbols in the code graph carry their kind (task 93ca4274, epic
61821448 "the ontology holds the whole code graph").

Bug: graphify's generic tree-sitter extractor (graphify/extract.py,
``_extract_generic``) emits every Python def/class/module/method with
``file_type="code"`` -- it carries no separate type/kind hint of its own,
confirmed by running graphify against a scratch package and reading
graphify-out/graph.json directly. graph_service._import_graph_json then
did ``kind = file_type or "node"``, so EVERY real Python symbol landed in
graph.db's entities table as kind "code", and services/ontology_graph.py's
_CODE_KIND_CLASS (function/method/class/module -> o:Function/o:Method/
o:Class/o:Module) never matched -- those counts read 0 on the Ontology
page and Explore's class pill could never say Function.

The fix reads structure graphify already committed at parse time (never a
name-shape guess like CamelCase):
  - the file's own self-node (label == its own basename)          -> module
  - the TARGET of a "method" edge from its class                  -> method
  - the TARGET of a "contains" edge from the file, label "name()" -> function
  - the TARGET of a "contains" edge from the file, bare label     -> class
  - anything else (a non-Python "code" node, a "rationale" row)   -> untouched

These fixtures are graphify's REAL node/edge shape for exactly this case,
verified 2026-08-26 by running `python -m graphify update` against a
scratch package (module + class-with-method + module-level function +
CamelCase function) and reading graphify-out/graph.json byte for byte.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _graph_json_fixture() -> dict:
    """graphify's real shape for a module with a class+method, a
    module-level function, and a CamelCase module-level function --
    plus one rationale row and one non-Python "code" node, both of
    which must stay UNTOUCHED by the Python-only derivation."""
    return {
        "nodes": [
            # The module's own self-node: label == its own basename.
            {"id": "pkg_py", "label": "pkg.py", "file_type": "code",
             "source_file": "pkg.py", "source_location": "L1"},
            # A class: bare label, target of a "contains" edge.
            {"id": "pkg_widget", "label": "Widget", "file_type": "code",
             "source_file": "pkg.py", "source_location": "L4"},
            # A method: ".name()" label, target of a "method" edge.
            {"id": "pkg_widget_build", "label": ".build()", "file_type": "code",
             "source_file": "pkg.py", "source_location": "L5"},
            # A module-level function: "name()" label, target of "contains".
            {"id": "pkg_make_widget", "label": "make_widget()", "file_type": "code",
             "source_file": "pkg.py", "source_location": "L9"},
            # A CamelCase module-level function -- must stay a function,
            # never misread as a class from its capitalization.
            {"id": "pkg_camelcasehelper", "label": "CamelCaseHelper()",
             "file_type": "code", "source_file": "pkg.py",
             "source_location": "L13"},
            # A rationale row -- untouched by this task (task f9e0745e).
            {"id": "pkg_rationale_1", "label": "why pkg exists",
             "file_type": "rationale", "source_file": "pkg.py",
             "source_location": "L1"},
            # A non-Python "code" node -- the Python-only derivation must
            # never touch it; it stays the generic fallback "code".
            {"id": "app_js", "label": "app.js", "file_type": "code",
             "source_file": "app.js", "source_location": "L1"},
        ],
        "links": [
            {"source": "pkg_py", "target": "pkg_widget", "relation": "contains"},
            {"source": "pkg_py", "target": "pkg_make_widget", "relation": "contains"},
            {"source": "pkg_py", "target": "pkg_camelcasehelper", "relation": "contains"},
            {"source": "pkg_widget", "target": "pkg_widget_build", "relation": "method"},
            {"source": "pkg_py", "target": "pkg_rationale_1", "relation": "rationale_for"},
        ],
    }


def _fresh_result() -> dict:
    return {"imported_entities": 0, "imported_relationships": 0}


def _import_fixture(pid: str) -> None:
    from prism_service.config import project_data_dir
    from prism_service.services.graph_service import GraphService

    data_dir = project_data_dir(pid)
    data_dir.mkdir(parents=True, exist_ok=True)
    svc = GraphService(
        project_data_dir=str(data_dir),
        graph_db_path=str(data_dir / "graph.db"),
    )
    out = svc._import_graph_json(_graph_json_fixture(), _fresh_result(), None)
    assert out["imported_entities"] == 7, out


def _kinds_by_id(pid: str) -> dict[str, str]:
    from prism_service.config import project_data_dir

    conn = sqlite3.connect(str(project_data_dir(pid) / "graph.db"))
    try:
        rows = conn.execute(
            "SELECT graphify_id, kind FROM entities"
        ).fetchall()
        return {gid: kind for gid, kind in rows}
    finally:
        conn.close()


@pytest.fixture
def project():
    from prism_service.project_context import get_project

    pid = f"py-kind-{uuid.uuid4().hex[:8]}"
    get_project(pid)
    _import_fixture(pid)
    return pid


def test_module_self_node_becomes_module(project):
    assert _kinds_by_id(project)["pkg_py"] == "module"


def test_class_becomes_class(project):
    assert _kinds_by_id(project)["pkg_widget"] == "class"


def test_method_becomes_method(project):
    assert _kinds_by_id(project)["pkg_widget_build"] == "method"


def test_module_level_function_becomes_function(project):
    assert _kinds_by_id(project)["pkg_make_widget"] == "function"


def test_camelcase_function_stays_a_function_not_a_class(project):
    """The exact misfire this task's likely_misfire names: a CamelCase
    top-level def must never be classified as a class from its shape."""
    assert _kinds_by_id(project)["pkg_camelcasehelper"] == "function"


def test_rationale_row_is_untouched(project):
    assert _kinds_by_id(project)["pkg_rationale_1"] == "rationale"


def test_non_python_code_node_is_untouched(project):
    """A JS/TS/... "code" node has no source_file ending in .py --
    the Python-only derivation must leave it at the generic fallback."""
    assert _kinds_by_id(project)["app_js"] == "code"


def test_ontology_counts_function_class_method_module(project):
    """The ontology projection of this imported graph counts Function 2,
    Class 1, Method 1, Module 1 -- and the untouched "code" row (app_js)
    still counts as the generic o:Code fallback, never lost."""
    from prism_service.services.ontology_graph import OntologyGraph

    og = OntologyGraph(project)
    og.rebuild()

    assert og._count("Function") == 2
    assert og._count("Class") == 1
    assert og._count("Method") == 1
    assert og._count("Module") == 1
    assert og._count("Code") == 1
