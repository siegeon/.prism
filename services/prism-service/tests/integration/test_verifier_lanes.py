"""Three-lane honest green signal in a clean isolated env (inverted-flow #5).

The per-task green predicate WAS "the full pytest suite passes" — the wrong
predicate (whole-suite green is neither necessary nor sufficient for THIS
task's outcome) and unreliable (skipped/timed-out tests aggregated as pass;
env-fragile tests false-RED inside the live dev daemon). These tests pin the
replacement:

  * the gate/verifier pytest run executes in a SANITIZED subprocess env
    (no PRISM_DEV_MODE / auto-updater / daemon coupling; PRISM_DATA_DIR at a
    throwaway dir) from the task's worktree — the env-false-red fix;
  * THREE blocking lanes reported distinctly: (a) oracle probe minting an
    EvidenceReceipt, (b) red->green continuity on the EXACT planned ids,
    (c) impact-selected regression DIFFED against a baseline run;
  * skipped / not-run / timeout are kept DISTINCT from pass;
  * ordinary tasks pass on (a)+(b)+(c) WITHOUT whole-suite green; release /
    high-risk tasks additionally require the full matrix;
  * the corrected ignore rule excludes copied DATA repos, not first-party
    benchmarks/.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import types
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import verifier_service as vs  # noqa: E402
from prism_service.services import oracle_spec as osp  # noqa: E402
from prism_service.services.verifier_service import (  # noqa: E402
    VerifierService, Claim, _tier_status, sanitized_run_env,
    parse_pytest_statuses, planned_failing_ids, continuity_lane,
    regression_lane, full_suite_lane, oracle_lane, is_release_task,
    _pytest_ignore_args,
)


# ---------------------------------------------------------------------------
# Harnesses
# ---------------------------------------------------------------------------


def _serve(status: int = 200, body: str = "<html>ok honest page renders</html>"):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *a):  # silence
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv


def _git_repo(tmp: Path) -> tuple[Path, str]:
    repo = tmp / "ws"
    repo.mkdir()
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=e)
    (repo / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True, env=e)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                         capture_output=True, text=True, env=e).stdout.strip()
    return repo, sha


def _task(**kw):
    kw.setdefault("id", str(uuid.uuid4()))
    kw.setdefault("oracle", "")
    kw.setdefault("likely_misfire", "")
    kw.setdefault("tags", [])
    kw.setdefault("completion_proof", "")
    kw.setdefault("description", "")
    kw.setdefault("verify", [])
    kw.setdefault("proof_type", "test")
    return types.SimpleNamespace(**kw)


# ---------------------------------------------------------------------------
# (1) CLEAN ISOLATED ENV — the env-false-red fix
# ---------------------------------------------------------------------------


def test_sanitized_env_strips_dev_mode_and_overrides_updater(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    monkeypatch.setenv("PRISM_AUTO_UPDATE", "on")
    monkeypatch.setenv("PRISM_PIDFILE", "/tmp/whatever.pid")
    env = sanitized_run_env(str(tmp_path))
    assert "PRISM_DEV_MODE" not in env, "PRISM_DEV_MODE must be stripped"
    assert env["PRISM_AUTO_UPDATE"] == "off"
    assert env["PRISM_AUTO_UPDATE_INTERVAL"] == "0"
    assert "PRISM_PIDFILE" not in env, "daemon-coupling vars must be dropped"
    assert env["PRISM_DATA_DIR"] == str(tmp_path)
    # the caller's real environment is not mutated
    assert os.environ.get("PRISM_DEV_MODE") == "1"


def test_sanitized_env_subprocess_lacks_dev_mode(tmp_path, monkeypatch):
    """A REAL subprocess launched with the sanitized env does not inherit
    PRISM_DEV_MODE and sees the auto-update override."""
    monkeypatch.setenv("PRISM_DEV_MODE", "1")
    env = sanitized_run_env(str(tmp_path))
    r = subprocess.run(
        [sys.executable, "-c",
         "import os,sys; "
         "sys.exit(0 if 'PRISM_DEV_MODE' not in os.environ "
         "and os.environ.get('PRISM_AUTO_UPDATE')=='off' else 3)"],
        env=env, capture_output=True, text=True)
    assert r.returncode == 0, (r.stdout, r.stderr)


# ---------------------------------------------------------------------------
# (2) RED->GREEN CONTINUITY
# ---------------------------------------------------------------------------


def test_continuity_all_planned_pass_is_green():
    ln = continuity_lane(["t.py::a", "t.py::b"],
                         {"t.py::a": "passed", "t.py::b": "passed"})
    assert ln.state == "green" and ln.blocking is False


def test_continuity_one_still_failing_is_red():
    ln = continuity_lane(["t.py::a", "t.py::b"],
                         {"t.py::a": "passed", "t.py::b": "failed"})
    assert ln.state == "red" and ln.blocking is True
    assert "t.py::b" in ln.detail


def test_continuity_none_recorded_is_inconclusive_not_autopass():
    ln = continuity_lane([], {})
    assert ln.state == "inconclusive"
    assert ln.blocking is False
    assert ln.state != "green", "no ids must NOT be an automatic pass"


def test_continuity_skipped_planned_id_is_not_a_pass():
    ln = continuity_lane(["t.py::a"], {"t.py::a": "skipped"})
    assert ln.state == "red" and ln.blocking is True


def test_continuity_missing_planned_id_is_not_run_not_pass():
    ln = continuity_lane(["t.py::a"], {})   # never collected
    assert ln.state == "red"
    assert ln.evidence["outcomes"]["t.py::a"] == "not-run"


# ---------------------------------------------------------------------------
# (3) IMPACT-SELECTED REGRESSION — baseline-diff
# ---------------------------------------------------------------------------


def test_regression_failing_on_both_baseline_and_candidate_does_not_block():
    ln = regression_lane({"t.py::a": "failed"}, {"t.py::a": "failed"})
    assert ln.state == "clean" and ln.blocking is False
    assert "t.py::a" in ln.evidence["preexisting"]
    assert ln.evidence["newly_introduced"] == {}


def test_regression_newly_introduced_failure_blocks():
    ln = regression_lane({"t.py::a": "passed"}, {"t.py::a": "failed"})
    assert ln.state == "red" and ln.blocking is True
    assert "t.py::a" in ln.evidence["newly_introduced"]


def test_regression_new_test_failing_is_this_tasks_fault():
    # a test absent from baseline (brand new) that fails IS newly-introduced
    ln = regression_lane({}, {"t.py::new": "failed"})
    assert ln.blocking is True and "t.py::new" in ln.evidence["newly_introduced"]


# ---------------------------------------------------------------------------
# (4) SKIPPED / TIMEOUT / NOT-RUN != PASS
# ---------------------------------------------------------------------------


def test_pytest_parser_keeps_skipped_distinct_from_passed():
    out = ("tests/t.py::test_ok PASSED [ 33%]\n"
           "tests/t.py::test_bad FAILED [ 66%]\n"
           "tests/t.py::test_skip SKIPPED (why) [100%]\n")
    m = parse_pytest_statuses(out)
    assert m["tests/t.py::test_ok"] == "passed"
    assert m["tests/t.py::test_bad"] == "failed"
    assert m["tests/t.py::test_skip"] == "skipped"


def test_tier_status_all_skipped_is_not_run_not_pass():
    only_skipped = [Claim(0, "tooling.ruff", status="skipped"),
                    Claim(0, "tooling.pytest", status="skipped")]
    assert _tier_status(only_skipped, 0) == "not-run", (
        "a tier that verified nothing must not report pass")


def test_full_suite_not_run_is_a_refusal():
    ln = full_suite_lane({})
    assert ln.state == "red" and ln.blocking is True


def test_full_suite_all_green_passes():
    ln = full_suite_lane({"t.py::a": "passed", "t.py::b": "skipped"})
    assert ln.state == "green" and ln.blocking is False


# ---------------------------------------------------------------------------
# (5) ORDINARY vs RELEASE green
# ---------------------------------------------------------------------------


def test_ordinary_task_passes_on_three_lanes_without_full_suite(tmp_path):
    url, srv = _serve()
    repo, _ = _git_repo(tmp_path)
    try:
        v = VerifierService(str(tmp_path / "scores.db"))
        task = _task(oracle=url)
        # continuity: the one planned id now passes; regression scope empty
        # (no diff) so it is clean; oracle http probe passes.
        runner = lambda targets, rev=None: {i: "passed" for i in targets}  # noqa: E731
        report = v.run_green_lanes(
            task, workspace=str(repo), project="default",
            planned_ids=["t.py::a"], pytest_runner=runner)
    finally:
        srv.shutdown()
    assert report["verdict"] == "pass", report
    assert report["release"] is False
    assert "full_suite" not in report["lanes"], (
        "an ordinary task must not require whole-suite green")
    assert set(report["lanes"]) == {"oracle", "continuity", "regression"}


def test_release_task_requires_full_matrix(tmp_path):
    url, srv = _serve()
    repo, _ = _git_repo(tmp_path)
    try:
        v = VerifierService(str(tmp_path / "scores.db"))
        task = _task(oracle=url, tags=["release"])
        assert is_release_task(task) is True

        def runner(targets, rev=None):
            # the whole-suite scope carries a real failure; the narrow
            # continuity id is green — an ordinary task would pass.
            if targets == ["services/prism-service/tests"]:
                return {"tests/x.py::a": "failed"}
            return {i: "passed" for i in targets}

        report = v.run_green_lanes(
            task, workspace=str(repo), project="default",
            planned_ids=["t.py::a"], pytest_runner=runner)
    finally:
        srv.shutdown()
    assert "full_suite" in report["lanes"]
    assert report["verdict"] == "fail", report
    assert "full_suite" in report["blocking"]


# ---------------------------------------------------------------------------
# (6) verify_green MINTS a real EvidenceReceipt
# ---------------------------------------------------------------------------


def test_run_green_lanes_mints_fresh_passing_receipt(tmp_path):
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    repo, sha = _git_repo(tmp_path)
    tid = str(uuid.uuid4())
    try:
        v = VerifierService(str(tmp_path / "scores.db"))
        task = _task(id=tid, oracle=url)
        before = osp.read_receipts("default", tid)
        report = v.run_green_lanes(
            task, workspace=str(repo), project="default",
            planned_ids=["t.py::a"],
            pytest_runner=lambda targets, rev=None: {i: "passed" for i in targets})
    finally:
        srv.shutdown()
    after = osp.read_receipts("default", tid)
    assert len(after) == len(before) + 1, "verify_green must APPEND one receipt"
    assert report["lanes"]["oracle"]["state"] == "green"
    # and the gate's fresh-receipt tooth would then see it fresh
    spec = osp.OracleSpec.from_task(task)
    fresh = osp.fresh_passing_receipt("default", tid, sha, spec.spec_hash())
    assert fresh is not None and fresh.passed is True


def test_oracle_lane_failing_probe_blocks(tmp_path):
    url, srv = _serve(status=500, body="server exploded")
    srv_task = _task(oracle=url)
    try:
        ln, receipt = oracle_lane(srv_task, {"project": "default"})
    finally:
        srv.shutdown()
    assert ln.state == "red" and ln.blocking is True
    assert receipt is not None and receipt.passed is False


# ---------------------------------------------------------------------------
# (7) CORRECTED IGNORE RULE — data copy-trees out, first-party benchmarks in
# ---------------------------------------------------------------------------


def test_ignore_excludes_data_tree_but_not_first_party_benchmarks(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "benchmarks" / "mybench").mkdir(parents=True)
    (tmp_path / "benchmarks" / "mybench" / "run.py").write_text("x", encoding="utf-8")
    args = _pytest_ignore_args(tmp_path)
    joined = " ".join(args)
    # a data/ copy-tree path is excluded ...
    assert any(a.startswith("--ignore=") and a.rstrip("/\\").endswith("data")
               for a in args), args
    # ... graphify-src / data-bench copy trees are excluded ...
    assert "--ignore-glob=*graphify-src*" in args
    assert "--ignore-glob=*data-bench*" in args
    # ... but NO arg blanket-excludes the first-party benchmarks/ tree.
    assert "benchmarks" not in joined, (
        "first-party benchmarks/ must stay in the impact/full matrix")
