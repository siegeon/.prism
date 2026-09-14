"""oracle_spec.red_refusal_kind classifies a NON-RED red-oracle receipt so a
rewind seat can tell a defective draft from something the runner genuinely
could not judge (task bb3d1f6a).

`status` alone cannot make this distinction: run_red_oracle funnels BOTH "the
pinned suite PASSES at the anchor" (rc==0) and "pytest never even collected
the pinned ids" (rc=4/5 -- a missing test id, an import error in the draft)
into the SAME status=ST_FAILED (_red_worktree_run's final "else" branch
discards the sub-runner's own ST_ERROR classification because a red
demonstration only cares about rc==1). Only the `pytest_pass` observation's
rc value tells them apart.

  AC-1  rc==0 (suite genuinely passes) -> "passed"
  AC-2  rc in (2, 4, 5) (could not collect) -> "collection_error"
  AC-3  status=ST_ERROR (git worktree/subprocess failure) -> "inconclusive",
        regardless of what observations happen to be attached
  AC-4  no receipt at all -> "inconclusive"
  AC-5  no pytest_pass observation on a FAILED-status receipt -> "inconclusive"
        (never mistaken for a collection error with no evidence of one)
"""
from __future__ import annotations

from prism_service.services import oracle_spec as osp


def _receipt(status, rc=None, reason=""):
    obs = [] if rc is None else [
        {"name": "pytest_pass", "observed": rc, "passed": rc == 0}]
    return osp.EvidenceReceipt(
        task_id="t1", job_id="j1", spec_hash="s1", tree_sha="a" * 40,
        adapter=osp.ADAPTER_PYTEST, passed=False, status=status,
        reason=reason, observations=obs)


def test_a_genuine_pass_at_the_anchor_is_passed():
    r = _receipt(osp.ST_FAILED, rc=0,
                reason="NOT red: the spec's tests PASS at the red-step "
                       "commit a1b2c3 (pytest_ids: t.py -> rc=0)")
    assert osp.red_refusal_kind(r) == "passed"


def test_a_collection_error_rc4_is_a_collection_error():
    """The exact live shape from task bb3d1f6a: a pinned id could not even
    be collected (a missing test id / an import error in the draft)."""
    r = _receipt(osp.ST_FAILED, rc=4,
                reason="red not demonstrated at 086f4101 (rc=4, wanted "
                       "rc==1 test failures): pytest_ids: could not "
                       "collect tests/unit/test_x.py")
    assert osp.red_refusal_kind(r) == "collection_error"


def test_a_collection_error_rc5_is_also_a_collection_error():
    r = _receipt(osp.ST_FAILED, rc=5, reason="no tests ran")
    assert osp.red_refusal_kind(r) == "collection_error"


def test_a_runner_failure_is_inconclusive_never_a_verdict_on_the_draft():
    """status=ST_ERROR means the runner itself could not judge (a git
    worktree add failure, an unrunnable environment) -- never a verdict on
    the draft, so it must never read as a rewindable cause."""
    r = _receipt(osp.ST_ERROR, rc=4,
                reason="red oracle: could not check out red-step commit")
    assert osp.red_refusal_kind(r) == "inconclusive"


def test_no_receipt_at_all_is_inconclusive():
    assert osp.red_refusal_kind(None) == "inconclusive"


def test_a_failed_receipt_with_no_pytest_pass_observation_is_inconclusive():
    """A FAILED status with nothing to read rc from (e.g. a runner that
    could not even execute pytest) must not be guessed into a collection
    error it has no evidence for."""
    r = _receipt(osp.ST_FAILED, rc=None, reason="pytest_ids: could not run")
    assert osp.red_refusal_kind(r) == "inconclusive"


def test_rc_one_never_reaches_this_function_but_is_handled_safely():
    """rc==1 is ST_RED in production (a real demonstration) and would never
    be routed here refused -- but a defensive caller should not crash or
    silently rewind on it if it ever is."""
    r = _receipt(osp.ST_FAILED, rc=1, reason="unexpected shape")
    assert osp.red_refusal_kind(r) == "inconclusive"
