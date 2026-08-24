"""Gate-evidence wiring (task 9afd1b72, slice 2 of epic d381f259).

Pins three things:

  * ``oracle_spec.run_oracle`` MINTS real evidence for ui/demo tasks: it calls
    ``evidence_capture.capture_walkthrough`` against the feature URL and
    populates ``EvidenceReceipt.artifacts`` with {kind, path, provenance}
    entries naming REAL files under the task's evidence store, plus a
    per-pytest-id verbatim assertion-source entry for ids named in
    ``task.verify``. This is layered ON TOP of whatever the spec's own
    adapter already produced.
  * The capture is ALL best-effort: a Playwright failure (unavailable,
    unreachable app, a raised exception) never raises out of run_oracle and
    never fails the mint — it simply yields an artifact-less receipt.
  * conductor_service's green_gate artifact tooth (ui_artifact_gate_reason /
    green_gate_artifact_reason / has_captured_evidence) no longer trusts a
    bare "screenshot" MENTION in completion_proof on its own — that is
    gameable self-attested prose. It PASSES only when a real file backs the
    claim: either a captured file under data_dir/evidence/<id>/, or (new
    here) a fresh EvidenceReceipt's artifacts[] naming a file that exists on
    disk. A bare-substring proof with NO backing file anywhere is REJECTED.
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Harnesses (function-local imports throughout — collection must succeed even
# in an environment where playwright / conductor deps behave unexpectedly)
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


def _ui_demo_task(url: str, **kw):
    return types.SimpleNamespace(
        id=kw.pop("id", str(uuid.uuid4())),
        oracle=url,
        likely_misfire=kw.pop("likely_misfire", ""),
        proof_type=kw.pop("proof_type", "demo"),
        tags=kw.pop("tags", ["ui"]),
        verify=kw.pop("verify", []),
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. Mint-time capture — real files land in receipt.artifacts
# ---------------------------------------------------------------------------


def test_run_oracle_populates_artifacts_with_real_file_for_ui_demo_task(
        tmp_path, monkeypatch):
    from prism_service.services import oracle_spec as osp
    from prism_service.services import evidence_capture as ec
    from prism_service.data_dir import evidence_dir

    url, srv = _serve()
    try:
        task = _ui_demo_task(url)

        def _fake_capture(feature_url, out_dir, selector=None, video=True,
                          now=None):
            assert feature_url == url
            out = Path(out_dir)
            out.mkdir(parents=True, exist_ok=True)
            shot = out / "screenshot.png"
            shot.write_bytes(b"fake-png-bytes")
            return {"screenshot": str(shot), "video": None, "error": None}

        monkeypatch.setattr(ec, "capture_walkthrough", _fake_capture)

        spec = osp.OracleSpec.from_task(task)
        r = osp.run_oracle(spec, task, ctx={"project": "default"})
    finally:
        srv.shutdown()

    shots = [a for a in r.artifacts if a.get("kind") == "screenshot"]
    assert shots, f"expected a screenshot artifact, got {r.artifacts!r}"
    path = Path(shots[0]["path"])
    assert path.is_file(), "receipt.artifacts must name a REAL file on disk"
    assert path.parent == evidence_dir(task.id), (
        "capture must land under the task's evidence store")
    assert shots[0]["provenance"], "artifact must carry provenance metadata"


def test_run_oracle_capture_error_yields_artifactless_receipt_no_raise(
        tmp_path, monkeypatch):
    """A Playwright-unavailable / unreachable-app style FAILURE (an error
    dict, not an exception) must not add any screenshot/video artifact —
    the receipt is honestly artifact-less, not a silent fake pass."""
    from prism_service.services import oracle_spec as osp
    from prism_service.services import evidence_capture as ec

    url, srv = _serve()
    try:
        task = _ui_demo_task(url)

        def _failing_capture(*a, **kw):
            return {"screenshot": None, "video": None,
                    "error": "playwright unavailable: forced for test"}

        monkeypatch.setattr(ec, "capture_walkthrough", _failing_capture)

        spec = osp.OracleSpec.from_task(task)
        r = osp.run_oracle(spec, task, ctx={"project": "default"})
    finally:
        srv.shutdown()

    assert not [a for a in r.artifacts if a.get("kind") in
               ("screenshot", "video")]
    # The underlying http_probe still ran and can still pass/fail normally —
    # capture failure must not corrupt the oracle verdict.
    assert r.passed is True


def test_run_oracle_capture_exception_does_not_raise(tmp_path, monkeypatch):
    """A hard CRASH inside capture_walkthrough (not just an error dict) must
    be swallowed by run_oracle — evidence capture is opt-in proof, never a
    crash surface for the mint."""
    from prism_service.services import oracle_spec as osp
    from prism_service.services import evidence_capture as ec

    url, srv = _serve()
    try:
        task = _ui_demo_task(url)

        def _boom(*a, **kw):
            raise RuntimeError("simulated browser launch crash")

        monkeypatch.setattr(ec, "capture_walkthrough", _boom)

        spec = osp.OracleSpec.from_task(task)
        r = osp.run_oracle(spec, task, ctx={"project": "default"})  # no raise
    finally:
        srv.shutdown()

    assert r.passed is True
    assert not [a for a in r.artifacts if a.get("kind") in
               ("screenshot", "video")]


def test_run_oracle_attaches_assertion_source_for_verify_pytest_ids(
        tmp_path, monkeypatch):
    from prism_service.services import oracle_spec as osp
    from prism_service.services import evidence_capture as ec

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "test_sample.py").write_text(
        "def test_marker():\n    assert 1 == 1, 'demo assertion'\n",
        encoding="utf-8")
    node_id = "test_sample.py::test_marker"

    url, srv = _serve()
    try:
        task = _ui_demo_task(url, verify=[node_id])
        monkeypatch.setattr(
            ec, "capture_walkthrough",
            lambda *a, **kw: {"screenshot": None, "video": None,
                              "error": "skip for this test"})

        spec = osp.OracleSpec.from_task(task)
        r = osp.run_oracle(spec, task, ctx={"project": "default",
                                            "workspace": str(ws)})
    finally:
        srv.shutdown()

    hits = [a for a in r.artifacts if a.get("kind") == "assertion_source"]
    assert hits and hits[0]["path"] == node_id
    assert "assert 1 == 1" in hits[0]["provenance"].get("source", "")


# ---------------------------------------------------------------------------
# 2. has_captured_evidence consults receipt.artifacts (real files)
# ---------------------------------------------------------------------------


def test_has_captured_evidence_true_for_real_receipt_artifact_file(tmp_path):
    from prism_service.services import oracle_spec as osp
    from prism_service.services.conductor_service import has_captured_evidence

    task_id = str(uuid.uuid4())
    real_file = tmp_path / "shot.png"
    real_file.write_bytes(b"x")
    receipt = osp.EvidenceReceipt(
        task_id=task_id, job_id="j1", spec_hash="h", tree_sha="t",
        adapter="browser", passed=True, status=osp.ST_PASSED,
        artifacts=[{"kind": "screenshot", "path": str(real_file),
                   "provenance": {}}])
    osp.append_receipt("default", receipt)

    assert has_captured_evidence(task_id, "default") is True


def test_has_captured_evidence_false_when_receipt_artifact_file_missing(
        tmp_path):
    from prism_service.services import oracle_spec as osp
    from prism_service.services.conductor_service import has_captured_evidence

    task_id = str(uuid.uuid4())
    missing = tmp_path / "never-written.png"
    receipt = osp.EvidenceReceipt(
        task_id=task_id, job_id="j1", spec_hash="h", tree_sha="t",
        adapter="browser", passed=True, status=osp.ST_PASSED,
        artifacts=[{"kind": "screenshot", "path": str(missing),
                   "provenance": {}}])
    osp.append_receipt("default", receipt)

    assert has_captured_evidence(task_id, "default") is False


# ---------------------------------------------------------------------------
# 3. The artifact tooth itself: bare "screenshot" mention is NOT proof
# ---------------------------------------------------------------------------


def test_ui_artifact_gate_reason_rejects_bare_screenshot_substring():
    from prism_service.services.conductor_service import ui_artifact_gate_reason

    reason = ui_artifact_gate_reason(
        ["ui"], "demo",
        "we captured a screenshot of the dashboard, it looks great")
    assert reason, "a bare 'screenshot' mention with no real file must reject"


def test_ui_artifact_gate_reason_still_passes_on_concrete_signal():
    from prism_service.services.conductor_service import ui_artifact_gate_reason

    reason = ui_artifact_gate_reason(
        ["ui"], "demo", "agent-browser loaded the page; screenshot docs/ok.png")
    assert reason == "", "a real path/extension citation is still trusted"


def test_green_gate_artifact_reason_rejects_bare_screenshot_substring():
    from prism_service.services.conductor_service import green_gate_artifact_reason

    reason = green_gate_artifact_reason(
        "we captured a screenshot of the feature working", "", "demo")
    assert reason, "bare substring alone must not satisfy the artifact tooth"


def test_green_gate_artifact_reason_still_passes_on_concrete_signal():
    from prism_service.services.conductor_service import green_gate_artifact_reason

    reason = green_gate_artifact_reason(
        "pytest full suite: agent-browser screenshot at :8888/card.png", "",
        "demo")
    assert reason == ""


# ---------------------------------------------------------------------------
# 4. End-to-end: the green_gate DECISION honors receipt-backed evidence
# ---------------------------------------------------------------------------


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _walk_to_green_gate(cond, task_id: str) -> None:
    from prism_service.models.workflow import WORKFLOW_STEPS
    # task 3928b7ac (issue #222 continued): premise_grounded is now
    # unconditional on its own dedicated task.premise_notes field — seed it
    # once so this evidence-wiring walk (unrelated to premise content) can
    # leave review_previous_notes.
    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising the green_gate evidence "
        "wiring, not a real premise claim - UNVERIFIED\n"))
    # demo_evidence_gate_reason (task 3baadd19 qa discovery, 2026-08-24):
    # verify_green_state now refuses to advance a human-judgment task
    # (proof_type=demo/review) into green_gate with an EMPTY evidence
    # store. This walk is unrelated to that check's own concern (it's
    # exercising the SEPARATE oracle-receipt/artifact wiring the tests in
    # this file assert afterward, via their own monkeypatched capture) —
    # seed one placeholder file JUST long enough for the walk to pass
    # through the check, then remove it (finally, every exit path): a
    # test asserting NO real evidence backs a bare-substring proof must
    # not find this walk's own transient scaffolding still sitting there.
    from prism_service.data_dir import evidence_dir
    _seed_dir = evidence_dir(task_id)
    _seed_dir.mkdir(parents=True, exist_ok=True)
    _seed_path = _seed_dir / "walk-placeholder.png"
    _seed_path.write_bytes(b"\x89PNG\r\n\x1a\n0000")
    try:
        target = next(i for i, s in enumerate(WORKFLOW_STEPS)
                      if s["id"] == "green_gate")
        guard = (target + 1) * 3
        cleared = 0
        while guard > 0:
            guard -= 1
            snap = cond._task_svc.get(task_id)
            if snap.workflow_step == "green_gate" and snap.gate_state == "pending":
                return
            if snap.gate_state == "pending":
                cleared += 1
                cond.gate_decide(
                    task_id, action="approve",
                    reason="walk intermediate; independent re-run: pytest -> 1 failed",
                    override=True, actor=f"walk-bot-{cleared}",
                    session_id=f"walk-bot-{cleared}")
                continue
            cond.advance_task(task_id)
    finally:
        _seed_path.unlink(missing_ok=True)


_MISFIRE = "the page crashes with a blank white screen"
_GREEN_REASON = ("verify_green: pytest full suite passed (0 failed); oracle "
                 "EvidenceReceipt http_probe passed against the surface")


def test_green_gate_passes_bare_screenshot_proof_when_receipt_is_file_backed(
        tmp_path, monkeypatch):
    """The exact ticket scenario: completion_proof only SAYS 'screenshot' —
    no path, no extension — but a fresh EvidenceReceipt for the task names a
    REAL file. The gate must clear on the file-backed evidence."""
    from prism_service.services import oracle_spec as osp
    from prism_service.services import evidence_capture as ec

    task_svc, cond = _services(tmp_path)
    url, srv = _serve()
    try:
        t = task_svc.create(
            title="ui demo fixture", tags=["ui", "conductor"], oracle=url,
            likely_misfire=_MISFIRE, proof_type="demo",
            completion_proof="we captured a screenshot of the feature working")
        _walk_to_green_gate(cond, t.id)
        live = task_svc.get(t.id)

        real_shot = tmp_path / "real_capture.png"

        def _fake_capture(feature_url, out_dir, selector=None, video=True,
                          now=None):
            real_shot.write_bytes(b"real-bytes")
            return {"screenshot": str(real_shot), "video": None,
                    "error": None}

        monkeypatch.setattr(ec, "capture_walkthrough", _fake_capture)

        spec = osp.OracleSpec.from_task(live)
        r = osp.run_oracle(spec, live, ctx={"project": "default"})
        assert r.passed is True
        assert any(a.get("kind") == "screenshot" and
                  Path(a["path"]).is_file() for a in r.artifacts)
    finally:
        srv.shutdown()

    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    assert res["ok"] is True, res
    assert task_svc.get(t.id).gate_state == "passed"


def test_green_gate_refuses_bare_screenshot_proof_with_no_backing_file(
        tmp_path, monkeypatch):
    """Same bare-substring proof, but the capture FAILS (no file anywhere) —
    the gate must NOT clear on the self-attested word alone."""
    from prism_service.services import oracle_spec as osp
    from prism_service.services import evidence_capture as ec

    task_svc, cond = _services(tmp_path)
    url, srv = _serve()
    try:
        t = task_svc.create(
            title="ui demo fixture 2", tags=["ui", "conductor"], oracle=url,
            likely_misfire=_MISFIRE, proof_type="demo",
            completion_proof="we captured a screenshot of the feature working")
        _walk_to_green_gate(cond, t.id)
        live = task_svc.get(t.id)

        monkeypatch.setattr(
            ec, "capture_walkthrough",
            lambda *a, **kw: {"screenshot": None, "video": None,
                              "error": "playwright unavailable"})

        spec = osp.OracleSpec.from_task(live)
        r = osp.run_oracle(spec, live, ctx={"project": "default"})
        assert r.passed is True  # the http_probe itself still honestly passes
        assert not r.artifacts   # but capture yielded nothing real
    finally:
        srv.shutdown()

    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    assert res["ok"] is False
    assert res["gate_state"] in ("pending", "failed")
    assert "screenshot" in res["reason"].lower() or "artifact" in res["reason"].lower()
