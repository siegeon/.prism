"""The pytest oracle runs the pinned suite from the directory the PROJECT
runs pytest in — the same one CI uses (task 338f7810, 2026-09-05).

`_run_pytest_ids` ran from the workspace ROOT.
`.github/workflows/pr-checks.yml` runs the suite with
`working-directory: services/prism-service`, the directory holding
pyproject.toml's [tool.pytest.ini_options]. Different rootdir, different
conftest resolution, different verdict.

LIVE REGRESSION: task 338f7810's pinned pair passed 39/39 run the way CI
runs it, and failed 1/39 run the way the oracle ran it. green_gate
therefore refused work CI was happy with, the adjudicator rewound a task
that had ALREADY landed on main as 82ec1e2a, and the receipt reported a
failure nobody could reproduce with the project's own command.

A verdict that does not predict CI is not evidence.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _mk_project(tmp_path: Path) -> Path:
    """A workspace shaped like this repo: a nested service dir carrying
    its own pyproject.toml, with the tests beneath it."""
    svc = tmp_path / "services" / "prism-service"
    (svc / "tests" / "integration").mkdir(parents=True)
    (svc / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n", encoding="utf-8")
    (svc / "tests" / "integration" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")
    return tmp_path


def test_ids_under_a_nested_project_run_from_that_project(tmp_path):
    """The live shape: run where CI runs, with ids relative to it."""
    from prism_service.services.oracle_spec import _pytest_cwd_and_ids

    root = _mk_project(tmp_path)
    cwd, ids = _pytest_cwd_and_ids(
        str(root),
        ["services/prism-service/tests/integration/test_a.py"])

    assert cwd == str(root / "services" / "prism-service"), (
        "the oracle must run from the directory holding the project's "
        f"pytest config, as CI does; got {cwd}")
    assert ids == ["tests/integration/test_a.py"], (
        f"ids must be relative to that directory; got {ids}")


def test_a_node_id_suffix_survives_the_rewrite(tmp_path):
    from prism_service.services.oracle_spec import _pytest_cwd_and_ids

    root = _mk_project(tmp_path)
    _cwd, ids = _pytest_cwd_and_ids(
        str(root),
        ["services/prism-service/tests/integration/test_a.py::test_a"])

    assert ids == ["tests/integration/test_a.py::test_a"]


def test_ids_spanning_two_projects_stay_at_the_workspace_root(tmp_path):
    """No single project owns the run, so nothing is rewritten."""
    from prism_service.services.oracle_spec import _pytest_cwd_and_ids

    root = _mk_project(tmp_path)
    other = root / "services" / "other-service"
    (other / "tests").mkdir(parents=True)
    (other / "pyproject.toml").write_text("[tool.pytest.ini_options]\n",
                                          encoding="utf-8")

    cwd, ids = _pytest_cwd_and_ids(str(root), [
        "services/prism-service/tests/integration/test_a.py",
        "services/other-service/tests/test_b.py",
    ])

    assert cwd == str(root)
    assert ids == ["services/prism-service/tests/integration/test_a.py",
                   "services/other-service/tests/test_b.py"]


def test_no_nested_pyproject_leaves_the_run_unchanged(tmp_path):
    """A flat repo keeps today's behaviour exactly."""
    from prism_service.services.oracle_spec import _pytest_cwd_and_ids

    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "def test_a():\n    assert True\n", encoding="utf-8")

    cwd, ids = _pytest_cwd_and_ids(str(tmp_path), ["tests/test_a.py"])

    assert cwd == str(tmp_path)
    assert ids == ["tests/test_a.py"]


def test_an_absolute_id_is_left_alone(tmp_path):
    from prism_service.services.oracle_spec import _pytest_cwd_and_ids

    root = _mk_project(tmp_path)
    abs_id = str(root / "services" / "prism-service" / "tests"
                 / "integration" / "test_a.py")

    cwd, ids = _pytest_cwd_and_ids(str(root), [abs_id])

    assert cwd == str(root)
    assert ids == [abs_id]
