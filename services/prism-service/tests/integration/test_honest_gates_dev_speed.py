"""Honest gates survive dev-speed code change — red→green pins for task
68e5c699-8ccc-400b-9aa2-eb5df0650a36.

Three seams under test (the umbrella defect from the 16777a76 epic drives):

  AC-3  OracleSpec.from_task derives a REAL ``pytest_ids`` spec from
        task.verify[] pytest entries when the task is test-proofed — today
        it derives only http_probe (URL) or the manual browser floor, so a
        test-proofed task can never be machine-evidenced and its green_gate
        is override-only.
  AC-2  Mint and check resolve the policy pin through ONE code path
        (``control_plane.task_pin``): a receipt minted under the CURRENT
        pin is fresh by construction, and dev-speed churn (an unrelated,
        non-policy commit) does not move the pin.
  AC-4  The policy tooth is NOT loosened by any of this: a passing receipt
        minted under a DIFFERENT pinned policy_hash still refuses with the
        stale-policy message (the task's pre-declared likely_misfire).

AC-1 (flow mints at verify_green_state) is already pinned end-to-end by
test_conductor_work_honest_green.py for http oracles; AC-3 extends that
reach to pytest-proofed tasks through the same mint.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def _tiny_policy_repo(tmp_path):
    """A git repo whose baseline commit carries the full POLICY_FILES set —
    the smallest world in which the control plane can pin a policy."""
    from prism_service.services import control_plane as cp
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    for pf in cp.POLICY_FILES:
        p = repo / pf
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("policy-v1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                         capture_output=True, text=True).stdout.strip()
    return repo, sha


@pytest.fixture()
def pinned_world(tmp_path, monkeypatch):
    """A workspace-anchored pin world shared by mint AND check: both the
    control plane's and the conductor's workspace lookups resolve to the
    same tiny policy repo, env pin override cleared, data dir isolated."""
    import prism_service.services.task_workspace as tw
    from prism_service.services import control_plane as cp

    repo, sha = _tiny_policy_repo(tmp_path)
    ws = {"path": str(repo), "baseline": sha, "repo_root": str(repo)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws)
    monkeypatch.setattr(cp, "_workspace_for", lambda tid: ws)
    monkeypatch.delenv("PRISM_CONTROL_REF", raising=False)
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    return repo, sha


# ---------------------------------------------------------------------------
# AC-3 — from_task derives pytest_ids from verify[]
# ---------------------------------------------------------------------------


def test_from_task_derives_pytest_ids_for_test_proof():
    from prism_service.services.oracle_spec import ADAPTER_PYTEST, OracleSpec
    t = SimpleNamespace(oracle="pinned tests pass on the trusted runner",
                        likely_misfire="", proof_type="test",
                        verify=["pytest tests/unit/test_x.py::test_y -q"])
    spec = OracleSpec.from_task(t)
    assert spec.adapter == ADAPTER_PYTEST, spec.adapter
    assert "tests/unit/test_x.py::test_y" in spec.target
    assert spec.derived is True
    # deterministic: the same task fields derive the same spec_hash
    assert spec.spec_hash() == OracleSpec.from_task(t).spec_hash()


def test_from_task_pytest_wins_over_url_when_test_proofed():
    from prism_service.services.oracle_spec import ADAPTER_PYTEST, OracleSpec
    t = SimpleNamespace(oracle="green on http://127.0.0.1:8888/tasks",
                        likely_misfire="", proof_type="test",
                        verify=["pytest tests/unit/test_x.py::test_y"])
    assert OracleSpec.from_task(t).adapter == ADAPTER_PYTEST


def test_from_task_url_keeps_http_probe_without_pytest_material():
    from prism_service.services.oracle_spec import ADAPTER_HTTP, OracleSpec
    t = SimpleNamespace(oracle="serves http://127.0.0.1:8888/tasks",
                        likely_misfire="", proof_type="", verify=[])
    assert OracleSpec.from_task(t).adapter == ADAPTER_HTTP


def test_from_task_no_material_keeps_manual_floor():
    from prism_service.services.oracle_spec import ADAPTER_BROWSER, OracleSpec
    t = SimpleNamespace(oracle="the customer can read the page",
                        likely_misfire="", proof_type="", verify=[])
    assert OracleSpec.from_task(t).adapter == ADAPTER_BROWSER


# ---------------------------------------------------------------------------
# AC-2 — ONE pin resolution path, fresh by construction
# ---------------------------------------------------------------------------


def test_task_pin_is_baseline_anchored_and_churn_immune(pinned_world):
    from prism_service.services import control_plane as cp
    repo, sha = pinned_world
    pin = cp.task_pin("task-1")
    assert pin["control_ref"] == sha
    assert pin["policy_hash"].startswith("sha256:")
    # dev-speed churn: an unrelated NON-POLICY commit moves HEAD but the
    # baseline-anchored pin must not move.
    (repo / "unrelated.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unrelated dev churn")
    pin2 = cp.task_pin("task-1")
    assert pin2["policy_hash"] == pin["policy_hash"]
    assert pin2["control_ref"] == sha


def test_receipt_minted_now_is_fresh_now(pinned_world, tmp_path, monkeypatch):
    """Mint (run_oracle) then check (_oracle_receipt_refusal) in the SAME
    process: no refusal — the receipt is fresh by construction because both
    sides resolve the pin through control_plane.task_pin."""
    from prism_service.services import oracle_spec as osp
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    repo, sha = pinned_world

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="fresh mint", oracle="pinned tests green",
                        proof_type="test")
    task_svc.update(t.id, verify=["pytest tests/unit/test_ok.py::test_ok"])
    task = task_svc.get(t.id)

    # The pytest adapter itself is not under test here — stub it passing so
    # the receipt's PIN provenance is the only variable.
    monkeypatch.setitem(
        osp._ADAPTERS, osp.ADAPTER_PYTEST,
        lambda spec, ctx: ([], [], True, osp.ST_PASSED, "stub pass"))

    spec = osp.OracleSpec.from_task(task)
    assert spec.adapter == osp.ADAPTER_PYTEST  # needs the AC-3 rung
    receipt = osp.run_oracle(
        spec, task, ctx={"project": "testproj", "workspace": str(repo)})
    assert receipt.passed is True
    assert receipt.policy_hash.startswith("sha256:")

    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    refusal, fresh = cond._oracle_receipt_refusal(
        task, override=False, reason="")
    assert refusal == "", refusal
    assert fresh is not None and fresh.job_id == receipt.job_id


# ---------------------------------------------------------------------------
# AC-4 — the policy tooth is NOT loosened (pre-declared likely_misfire)
# ---------------------------------------------------------------------------


def test_stale_policy_receipt_still_refused(pinned_world, tmp_path,
                                            monkeypatch):
    from prism_service.services import oracle_spec as osp
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService
    repo, sha = pinned_world

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="stale pin", oracle="pinned tests green",
                        proof_type="test")
    task_svc.update(t.id, verify=["pytest tests/unit/test_ok.py::test_ok"])
    task = task_svc.get(t.id)

    spec = osp.OracleSpec.from_task(task)
    tree = osp.current_tree_sha(str(repo))
    # A PASSING receipt at the right tree+spec but minted under a DIFFERENT
    # pinned policy — must refuse with the stale-policy message.
    osp.append_receipt("testproj", osp.EvidenceReceipt(
        task_id=task.id, job_id="stale-pin-1", spec_hash=spec.spec_hash(),
        tree_sha=tree, adapter=spec.adapter, passed=True, status="passed",
        policy_hash="sha256:" + "0" * 64))

    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    refusal, fresh = cond._oracle_receipt_refusal(
        task, override=False, reason="")
    assert fresh is None
    assert "minted under pinned policy" in refusal, refusal
