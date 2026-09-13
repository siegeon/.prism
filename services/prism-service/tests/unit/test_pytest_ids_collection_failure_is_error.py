"""A pytest run that never collected any tests is INCONCLUSIVE, not FAILED
(task d0b392b3).

Live regression: after ship_worker reaped d0b392b3's own worktree, the
adjudicator sweep re-minted its oracle against a workspace that resolved to
the wrong directory. pytest exited rc=4 ("file or directory not found" / "no
tests ran") -- it never even ran the pinned assertions -- and
`_run_pytest_ids` reported that identically to a genuine red test:
``status=ST_FAILED``. `_oracle_receipt_refusal` then said "latest receipt
FAILED: ...", and `_evaluate_green_gate_rewind`'s "FAILED" in text check
rewound an ALREADY-SHIPPED task back into implement_tasks over a collection
error that said nothing about the merged code.

Fix: a pytest run that collected zero tests (rc==4, the usage-error exit
code; or rc==5, "no tests ran") is reported as ``ST_ERROR`` — the runner
could not judge, exactly like a browser oracle with no runner wired — never
``ST_FAILED``. The reason names the cwd and ids actually tried so a human
(or the next sweep) can tell a stale/wrong workspace from a real red test.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import oracle_spec as osp


def _mk_project(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_real.py").write_text(
        "def test_pass():\n    assert True\n\n"
        "def test_fail():\n    assert False\n",
        encoding="utf-8")
    return tmp_path


def test_a_missing_file_is_error_not_failed(tmp_path):
    """The exact live shape: the pinned path does not exist at the tree the
    oracle actually ran against (a stale/reaped/wrong workspace)."""
    root = _mk_project(tmp_path)
    spec = osp.OracleSpec(adapter=osp.ADAPTER_PYTEST,
                          target="tests/test_does_not_exist.py")
    obs, artifacts, passed, status, reason = osp._run_pytest_ids(
        spec, {"workspace": str(root)})

    assert passed is False
    assert status == osp.ST_ERROR, (
        f"a collection failure must report ST_ERROR (inconclusive), never "
        f"ST_FAILED (a real red test) -- got status={status!r}")
    assert "FAILED" not in reason.upper() or "collect" in reason.lower(), (
        "the reason must not read like a genuine test failure")
    assert "test_does_not_exist.py" in reason
    assert str(root) in reason or "workspace" in reason.lower()


def test_a_genuine_red_test_still_reports_failed(tmp_path):
    """Regression guard: a real assertion failure must still be ST_FAILED,
    not swallowed into the new inconclusive bucket."""
    root = _mk_project(tmp_path)
    spec = osp.OracleSpec(adapter=osp.ADAPTER_PYTEST,
                          target="tests/test_real.py::test_fail")
    obs, artifacts, passed, status, reason = osp._run_pytest_ids(
        spec, {"workspace": str(root)})

    assert passed is False
    assert status == osp.ST_FAILED
    assert "rc=1" in reason


def test_a_genuine_pass_is_unaffected(tmp_path):
    root = _mk_project(tmp_path)
    spec = osp.OracleSpec(adapter=osp.ADAPTER_PYTEST,
                          target="tests/test_real.py::test_pass")
    obs, artifacts, passed, status, reason = osp._run_pytest_ids(
        spec, {"workspace": str(root)})

    assert passed is True
    assert status == osp.ST_PASSED


def test_oracle_receipt_refusal_does_not_call_a_collection_error_failed():
    """`_oracle_receipt_refusal`'s prose must distinguish ST_ERROR from
    ST_FAILED the same way it already distinguishes ST_MANUAL -- otherwise
    `_evaluate_green_gate_rewind`'s "FAILED" in text.upper() check cannot
    tell a real red test from a run that never collected anything."""
    from types import SimpleNamespace

    from prism_service.services.conductor_service import ConductorService

    class _Receipt:
        status = osp.ST_ERROR
        passed = False
        adapter = osp.ADAPTER_PYTEST
        reason = ("pytest_ids: could not collect tests/test_x.py at "
                  "cwd=/some/gone/workspace (rc=4)")
        tree_sha = "deadbeef"
        spec_hash = "spec1"
        policy_hash = ""

    svc = ConductorService.__new__(ConductorService)
    svc._task_svc = SimpleNamespace()
    svc._project_name = "default"

    import prism_service.services.oracle_spec as osp_mod
    orig_latest = osp_mod.latest_receipt
    orig_fresh = osp_mod.fresh_passing_receipt
    orig_tree = osp_mod.current_tree_sha
    try:
        osp_mod.latest_receipt = lambda *a, **k: _Receipt()
        osp_mod.fresh_passing_receipt = lambda *a, **k: None
        osp_mod.current_tree_sha = lambda *a, **k: "currenttree"
        task = SimpleNamespace(id="t1", verify=["tests/test_x.py"],
                               oracle="", proof_type="test")
        refusal, receipt = svc._oracle_receipt_refusal(
            task, override=False, reason="")
    finally:
        osp_mod.latest_receipt = orig_latest
        osp_mod.fresh_passing_receipt = orig_fresh
        osp_mod.current_tree_sha = orig_tree

    assert "FAILED" not in refusal.upper(), (
        f"an ST_ERROR (inconclusive) receipt must not be worded as a "
        f"failure -- got {refusal!r}")
