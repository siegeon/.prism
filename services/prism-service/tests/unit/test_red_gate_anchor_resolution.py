"""Red gate can anchor before the implementation — task ed3263b4.

A lane whose tests and implementation land in ONE commit (the 19e4e7f7
shape) has no tests-only [task:<id8>] commit anywhere in history, so the
red seat's two existing tiers both fail: tier 1 (_red_tests_commit) finds
nothing, and mint_red_evidence's tier-2 fallback lands on the worktree
HEAD, where the fix already makes the pinned tests PASS — never red.

This suite pins a THIRD tier: the driver may NAME a pre-change ref (a
`red-anchor-ref: <sha>` marker line in the red-step completion_proof); the
SEAT ITSELF checks that ref out in a disposable worktree, copies the
pinned test files over it, and runs pytest there. Only a real rc==1
observed by the daemon counts as red — a driver's pasted transcript never
does (distinct-actor rule).

  AC-1  attested ref -> mint_red_evidence + adjudicate_test_red_gate reach
        a machine-decided red (real git worktree + real pytest, nothing
        mocked).
  AC-2  a fabricated pytest transcript with NO marker line is never
        evidence — mint falls through to worktree HEAD, where the fix
        makes the tests pass, so no red receipt and no approval follow.
  AC-3  neither a tests-only commit nor an attested ref -> adjudicate_test_
        red_gate parks pending (never failed) naming BOTH missing sources.
  AC-4  an attested ref that cannot be resolved is refused with a reason
        distinct from AC-3's, naming the offending ref.
  AC-6  GET-equivalent gate_readiness names the attested-ref option when
        neither anchor source resolves.

Tooth 2 (readiness must not promise "no owner action needed" for a
swept-and-refused anchor) is ALREADY SHIPPED (mx-ec8075, commit f4ffe69) —
not re-tested here; this file only pins Tooth 1.
"""

from __future__ import annotations

import subprocess


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True)


def _rev_parse(repo, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()


def _bundled_commit_repo(tmp_path, tid8):
    """The 19e4e7f7 shape: commit A = pre-change buggy source (no tests),
    commit B = tests AND the fix landed TOGETHER in one commit — never
    tests-only. Returns (repo, pre_change_sha, head_sha)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    (repo / "conftest.py").write_text(
        "import sys, pathlib\n"
        "sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))\n",
        encoding="utf-8")
    (repo / "feature.py").write_text("VALUE = 1  # pre-change bug\n",
                                     encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline: buggy feature")
    pre_change = _rev_parse(repo)

    (repo / "feature.py").write_text("VALUE = 2  # fixed\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_feature.py").write_text(
        "from feature import VALUE\n\n"
        "def test_value():\n    assert VALUE == 2\n",
        encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm",
        f"fix(feature): correct VALUE + tests [task:{tid8}]")
    head = _rev_parse(repo)
    return repo, pre_change, head


def _world(tmp_path, monkeypatch, tid8=None):
    """A red_gate-pending task wired to a fresh bundled-commit repo. Returns
    (task_svc, cond, task_id, repo, pre_change, head)."""
    import prism_service.services.task_workspace as tw
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="red anchor bundled commit",
                        oracle="tests fail at the pre-change ref",
                        proof_type="test")
    task_svc.update(t.id, verify=["tests/test_feature.py::test_value"],
                    workflow_step="red_gate", gate_state="pending")
    repo, pre_change, head = _bundled_commit_repo(tmp_path, tid8 or t.id[:8])
    ws = {"path": str(repo), "baseline": pre_change, "repo_root": str(repo)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: ws)
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    return task_svc, cond, t.id, repo, pre_change, head


# ---------------------------------------------------------------------------
# AC-1 — attested pre-change ref reaches machine-decided red (real git +
# real pytest subprocess; nothing about the checkout or the run is mocked).
# ---------------------------------------------------------------------------


def test_attested_pre_change_ref_reaches_machine_decided_red(
        tmp_path, monkeypatch):
    from prism_service.services.conductor_service import _red_pytest_spec
    from prism_service.services import oracle_spec as osp

    task_svc, cond, tid, _repo, pre_change, _head = _world(tmp_path,
                                                            monkeypatch)
    task_svc.update(tid, completion_proof=(
        "Bundled the fix and the tests into one commit by mistake. "
        f"red-anchor-ref: {pre_change}\n"
        "Checking that ref out and running the pinned test there proves "
        "the one thing that matters: it fails against the pre-change "
        "source."))

    minted = cond.mint_red_evidence(tid)
    assert minted["ok"] is True, minted
    assert minted["red_sha"] == pre_change, minted

    spec = _red_pytest_spec(task_svc.get(tid))
    fresh = osp.fresh_red_receipt("testproj", tid, pre_change,
                                  spec.spec_hash())
    assert fresh is not None and fresh.status == osp.ST_RED, fresh
    assert fresh.tree_sha == pre_change

    approved = cond.adjudicate_test_red_gate(tid)
    assert approved is not None and approved.get("ok") is True, approved
    after = task_svc.get(tid)
    assert after.gate_state not in ("pending", "failed"), after.gate_state
    hist = " ".join(str(h) for h in task_svc.history(tid))
    assert "RED demonstrated" in hist and pre_change[:12] in hist, hist


# ---------------------------------------------------------------------------
# AC-2 — a pasted/fabricated pytest transcript with NO marker line is never
# evidence: mint falls through to the worktree HEAD, where the fix already
# makes the pinned tests pass, so no prose can manufacture a red receipt.
# ---------------------------------------------------------------------------


def test_pasted_transcript_without_marker_is_never_evidence(
        tmp_path, monkeypatch):
    from prism_service.services.conductor_service import _red_pytest_spec
    from prism_service.services import oracle_spec as osp

    task_svc, cond, tid, _repo, _pre_change, head = _world(tmp_path,
                                                            monkeypatch)
    fabricated = ("Ran the suite locally:\n"
                  "1 failed, 0 passed in 0.11s\n"
                  "FAILED tests/test_feature.py::test_value - "
                  "AssertionError: assert 1 == 2\n")
    task_svc.update(tid, completion_proof=fabricated)

    minted = cond.mint_red_evidence(tid)
    # tier 1 (tests-only commit) absent, tier 2 (attested ref) absent (no
    # marker), so mint resolves the legacy tier 3 - worktree HEAD, where
    # VALUE == 2 and the pinned test genuinely PASSES.
    assert minted["red_sha"] == head, minted
    assert minted["ok"] is False, minted

    spec = _red_pytest_spec(task_svc.get(tid))
    assert osp.fresh_red_receipt("testproj", tid, head,
                                 spec.spec_hash()) is None

    approved = cond.adjudicate_test_red_gate(tid)
    assert approved is None, approved
    after = task_svc.get(tid)
    assert after.gate_state == "pending"
    assert after.workflow_step == "red_gate"


# ---------------------------------------------------------------------------
# AC-3 — neither a tests-only commit nor an attested ref -> the seat parks
# PENDING (never failed) naming BOTH missing sources, not a bare rc code.
# ---------------------------------------------------------------------------


def test_no_anchor_source_parks_naming_both_missing(tmp_path, monkeypatch):
    task_svc, cond, tid, _repo, _pre_change, _head = _world(tmp_path,
                                                             monkeypatch)
    task_svc.update(tid, completion_proof="no marker here, just prose")

    result = cond.adjudicate_test_red_gate(tid)
    assert result is None, result
    after = task_svc.get(tid)
    assert after.gate_state == "pending"
    assert after.workflow_step == "red_gate"
    reason = str(after.gate_reason or "")
    assert reason, "a computed refusal must be recorded, never thrown away"
    assert "tests-only" in reason and "[task:" in reason, reason
    assert "attested" in reason and "red-anchor-ref" in reason, reason
    assert not reason.strip().lower().startswith("rc="), reason


# ---------------------------------------------------------------------------
# AC-4 — an attested ref that cannot be resolved is refused with a reason
# distinct from AC-3's, and no red receipt is ever written for it.
# ---------------------------------------------------------------------------


def test_unresolvable_attested_ref_is_refused_distinctly(tmp_path,
                                                          monkeypatch):
    from prism_service.services.conductor_service import _red_pytest_spec
    from prism_service.services import oracle_spec as osp

    task_svc, cond, tid, _repo, _pre_change, _head = _world(tmp_path,
                                                             monkeypatch)
    bogus = "deadbeef" * 5  # 40 hex chars, absent from this repo entirely
    task_svc.update(tid, completion_proof=f"red-anchor-ref: {bogus}\n")

    result = cond.adjudicate_test_red_gate(tid)
    assert result is None, result
    after = task_svc.get(tid)
    assert after.gate_state == "pending"
    reason = str(after.gate_reason or "")
    assert "could not be resolved" in reason, reason
    assert bogus[:12] in reason, reason
    assert "no attested pre-change ref supplied" not in reason, reason

    spec = _red_pytest_spec(task_svc.get(tid))
    receipts = osp.read_receipts("testproj", tid)
    assert not any(r.status == osp.ST_RED and r.spec_hash == spec.spec_hash()
                  for r in receipts), receipts


# ---------------------------------------------------------------------------
# AC-6 — the surface a person reads (gate_readiness) names the attested-ref
# option when neither anchor source resolves, instead of promising a
# machine sweep that cannot come.
# ---------------------------------------------------------------------------


def test_gate_readiness_names_attested_ref_option_when_unresolved(
        tmp_path, monkeypatch):
    from prism_service.api import conductor as capi

    task_svc, cond, tid, _repo, _pre_change, _head = _world(tmp_path,
                                                             monkeypatch)
    task_svc.update(tid, completion_proof="no marker, no evidence")
    monkeypatch.setattr(capi, "_svc", lambda project: cond)

    out = capi.gate_readiness(task_id=tid, project="testproj")
    assert out["receipt_ok"] is False, out
    assert "red-anchor-ref" in out["receipt_refusal"], out


# ---------------------------------------------------------------------------
# Direct unit pins for the two new helpers named by this task's own plan
# (conductor_service.py: _attested_red_ref, _resolve_attested_ref).
# ---------------------------------------------------------------------------


def test_attested_red_ref_parses_the_marker_line(tmp_path, monkeypatch):
    import types
    from prism_service.services.conductor_service import ConductorService

    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False)
    task = types.SimpleNamespace(
        completion_proof="notes above\nred-anchor-ref: abc123def456\nmore")
    assert cond._attested_red_ref(task) == "abc123def456"


def test_attested_red_ref_absent_without_the_marker(tmp_path):
    import types
    from prism_service.services.conductor_service import ConductorService

    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False)
    task = types.SimpleNamespace(
        completion_proof="5 failed, 0 passed\ntraceback ...\nno marker here")
    assert cond._attested_red_ref(task) == ""


def test_resolve_attested_ref_rev_parses_in_the_task_worktree(
        tmp_path, monkeypatch):
    task_svc, cond, tid, repo, pre_change, _head = _world(tmp_path,
                                                           monkeypatch)
    sha, resolved_repo = cond._resolve_attested_ref(tid, pre_change)
    assert sha == pre_change
    assert resolved_repo == str(repo)


def test_resolve_attested_ref_empty_for_an_unknown_ref(tmp_path,
                                                        monkeypatch):
    task_svc, cond, tid, _repo, _pre_change, _head = _world(tmp_path,
                                                            monkeypatch)
    sha, resolved_repo = cond._resolve_attested_ref(tid, "deadbeef" * 5)
    assert sha == "" and resolved_repo == ""
