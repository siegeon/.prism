"""Override actually releases a stuck gate (task 377b00a8).

Owner incident 2026-08-06 (task 7feed0c8, mirrored github #311 / jira
PRIS-43): a stuck green_gate with a refused/stale oracle receipt turned a
single Approve/Override click into a permanently stranded task. Pins the
three defects named in this ticket's oracle - never the readiness-vs-decider
divergence (that belongs to sibling 5c61e0e6, already done).

  1. COPY (TaskDetailPage.tsx:2101): the Override checkbox says "bypass the
     verifier and release on manual judgment" - unconditional, but override
     does NOT skip the oracle receipt check (conductor_service.py "NO
     OVERRIDE-SKIPS-THE-ORACLE", ~3550-3579): a stale/refused receipt still
     refuses. Truthful copy must say what override actually bypasses (the
     shell verifier, never the oracle) and name the real remedy (re-run the
     oracle for a fresh receipt, then Approve with override UNTICKED).
  2. RECOVERY STATE (conductor_service.py gate_decide): a refused oracle-
     receipt Approve - override ticked (~3557-3579) or plain (~3447-3473) -
     sets gate_state="failed" today. 'failed' reads terminal to the owner;
     the honest recovery state is 'pending' + an actionable gate_reason, the
     _park_green_refusal precedent the sibling rollup fix (457b38db) already
     established a few lines above at ~3420-3433.
  3. CLIENT UX (TaskDetailPage.gateDecide): the gate-decide POST has no
     AbortController/timeout, so a wedged request leaves "checking..."
     indistinguishable from a dead page forever.

Backend fixture: a real one-commit git repo, a fresh PASSING receipt minted
at commit A, then the workspace advances to commit B - the receipt's
tree_sha (A) no longer matches workspace HEAD (B), the STALE branch
_oracle_receipt_refusal takes (conductor_service.py:1898-1914).
"""

from __future__ import annotations

import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))
_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
        / "TaskDetailPage.tsx")

from prism_service.services import oracle_spec as osp  # noqa: E402


# --------------------------------------------------------------------------
# Backend fixture harness (mirrors tests/integration/test_oracle_evidence_
# receipt.py's git+http harness, duplicated small so this file is self-
# contained under its own allowed_files slice).
# --------------------------------------------------------------------------

def _serve(status: int = 200, body: str = "<html>ok honest page</html>"):
    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/", srv


def _git_repo(tmp: Path) -> tuple[Path, str]:
    import os
    repo = tmp / "ws"
    repo.mkdir()
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, env=e)
    (repo / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, env=e)
    subprocess.run(["git", "commit", "-qm", "c1"], cwd=repo, check=True,
                   env=e)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True, env=e).stdout.strip()
    return repo, sha


def _new_commit(repo: Path) -> str:
    import os
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    (repo / "f.txt").write_text("b", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "c2"], cwd=repo, check=True,
                   env=e)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True,
        capture_output=True, text=True, env=e).stdout.strip()


def _register_workspace(task_id: str, repo: Path) -> None:
    import json
    from prism_service.data_dir import resolve_data_dir
    root = resolve_data_dir() / "task_workspaces"
    root.mkdir(parents=True, exist_ok=True)
    idx_path = root / "index.json"
    idx = {}
    if idx_path.exists():
        idx = json.loads(idx_path.read_text())
    idx[task_id] = {"task_id": task_id, "path": str(repo), "baseline": "",
                    "branch": "b", "repo_root": str(repo)}
    idx_path.write_text(json.dumps(idx))


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _walk_to_green_gate(cond, task_id: str) -> None:
    from prism_service.models.workflow import WORKFLOW_STEPS
    target = next(i for i, s in enumerate(WORKFLOW_STEPS)
                  if s["id"] == "green_gate")
    guard = (target + 1) * 3
    cleared = 0
    while guard > 0:
        guard -= 1
        snap = cond._task_svc.get(task_id)
        if (snap.workflow_step == "green_gate"
                and snap.gate_state == "pending"):
            return
        if snap.gate_state == "pending":
            cleared += 1
            cond.gate_decide(
                task_id, action="approve",
                reason="walk intermediate; independent re-run: 1 failed",
                override=True, actor=f"walk-bot-{cleared}",
                session_id=f"walk-bot-{cleared}")
            continue
        cond.advance_task(task_id)
    raise AssertionError("never reached green_gate")


def _make_task(task_svc, cond, oracle: str, **kw):
    t = task_svc.create(title=kw.pop("title", "stuck-gate fixture"),
                        tags=kw.pop("tags", ["conductor"]), oracle=oracle,
                        likely_misfire=kw.pop(
                            "likely_misfire", "the page crashes blank"),
                        proof_type=kw.pop("proof_type", "test"),
                        completion_proof=kw.pop("completion_proof", ""),
                        **kw)
    task_svc.update(t.id, premise_notes=(
        "## Premises\n- fixture walk exercising the stuck-gate recovery "
        "state, not a real premise claim - UNVERIFIED\n"))
    _walk_to_green_gate(cond, t.id)
    return t


_GREEN_REASON = ("verify_green: pytest full suite passed; oracle "
                 "EvidenceReceipt http_probe passed against the surface")


# --------------------------------------------------------------------------
# Defect 2 - a refused oracle-receipt Approve must PARK pending, never fail.
# --------------------------------------------------------------------------

def test_plain_approve_on_stale_receipt_parks_pending_not_failed(tmp_path):
    """Non-override branch (conductor_service.py:3447-3473). Today it sets
    gate_state='failed' on a stale-tree_sha refusal; the fix must park
    'pending' with an actionable reason instead (_park_green_refusal
    precedent, ~2184-2209)."""
    task_svc, cond = _services(tmp_path)
    repo, sha_a = _git_repo(tmp_path)
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        t = _make_task(task_svc, cond, oracle=url,
                       completion_proof=f"loaded {url}; docs/ok.png")
        _register_workspace(t.id, repo)
        live = task_svc.get(t.id)
        r = osp.run_oracle(
            osp.OracleSpec.from_task(live), live,
            ctx={"project": "default", "workspace": str(repo)})
        assert r.passed is True and r.tree_sha == sha_a
    finally:
        srv.shutdown()
    sha_b = _new_commit(repo)
    assert sha_b != sha_a
    res = cond.gate_decide(t.id, "approve", reason=_GREEN_REASON,
                           actor="qa-final", session_id="qa-final")
    live = task_svc.get(t.id)
    assert live.gate_state == "pending", (
        f"a refused receipt must PARK pending (recoverable), not strand "
        f"'failed' - got {live.gate_state!r}, res={res}")
    assert (live.gate_reason or "").strip(), (
        "parked refusal must carry an ACTIONABLE gate_reason, not an "
        "empty string (task e0149f1f precedent)")
    assert res.get("ok") is False


def test_override_approve_on_stale_receipt_parks_pending_not_failed(
        tmp_path):
    """Override branch (conductor_service.py:3557-3579) - same stale-
    tree_sha fixture, override=True. Override still requires the oracle
    receipt (NO-OVERRIDE-SKIPS-THE-ORACLE), so this must ALSO park
    pending, never fail - ticking Override cannot be the difference
    between recoverable and stranded (the owner's actual 7feed0c8 click)."""
    task_svc, cond = _services(tmp_path)
    repo, sha_a = _git_repo(tmp_path)
    url, srv = _serve(status=200, body="<html>ok honest page renders</html>")
    try:
        t = _make_task(task_svc, cond, oracle=url,
                       completion_proof=f"loaded {url}; docs/ok.png")
        _register_workspace(t.id, repo)
        live = task_svc.get(t.id)
        r = osp.run_oracle(
            osp.OracleSpec.from_task(live), live,
            ctx={"project": "default", "workspace": str(repo)})
        assert r.passed is True and r.tree_sha == sha_a
    finally:
        srv.shutdown()
    sha_b = _new_commit(repo)
    assert sha_b != sha_a
    res = cond.gate_decide(
        t.id, "approve",
        reason=_GREEN_REASON + "; independent review, overriding to green",
        override=True, actor="qa-override", session_id="qa-override")
    live = task_svc.get(t.id)
    assert live.gate_state == "pending", (
        f"override on a refused/stale receipt must PARK pending, not "
        f"strand 'failed' - got {live.gate_state!r}, res={res}")
    assert (live.gate_reason or "").strip()
    assert res.get("ok") is False


# --------------------------------------------------------------------------
# Defect 1 - the Override checkbox copy is truthful and names the remedy.
# --------------------------------------------------------------------------

def _override_label_block() -> str:
    """The RENDERED <label> block wrapping the Override checkbox
    (TaskDetailPage.tsx ~2098-2103) - parses the enclosing JSX branch by
    brace/tag structure, never a fixed character window a comment above
    the element could satisfy (a lesson paid for three times already)."""
    src = _TSX.read_text(encoding="utf-8")
    m = re.search(
        r"<label[^>]*>.*?checked=\{gateOverride\}.*?</label>", src, re.S)
    assert m, (
        "could not locate the rendered Override <label>...<input "
        "checked={gateOverride}.../>...</label> block in "
        "TaskDetailPage.tsx - the affordance moved; update this locator")
    span = re.search(r"<span>(.*?)</span>", m.group(0), re.S)
    assert span, "the Override <label> has no <span> copy to read"
    return span.group(1)


def test_override_copy_names_the_verifier_not_the_oracle():
    low = _override_label_block().lower()
    assert "verifier" in low, (
        "Override copy must name what it actually bypasses - the shell "
        "verifier")
    assert "bypass the oracle" not in low, (
        "Override copy must NEVER claim it bypasses the oracle - override "
        "still requires a fresh oracle EvidenceReceipt "
        "(conductor_service.py NO-OVERRIDE-SKIPS-THE-ORACLE)")
    assert "oracle" in low, (
        "truthful copy must say the oracle evidence check STILL applies "
        "under override, or an owner reads 'release on manual judgment' "
        "as unconditional - the exact misreading behind the 7feed0c8 "
        "incident")


def test_override_copy_names_the_working_remedy():
    low = _override_label_block().lower()
    assert re.search(r"re-?run", low), (
        "copy must name the working remedy: re-run the oracle to mint a "
        "fresh receipt")
    assert re.search(r"(uncheck|un-?tick|without override)", low), (
        "copy must say the recovery path is Approve with override "
        "UNTICKED once evidence is fresh - not 'stay ticked forever'")
    assert "release on manual judgment" not in low, (
        "the unconditional 'release on manual judgment' claim is the "
        "lie: a refused oracle receipt still refuses under override "
        "today")


# --------------------------------------------------------------------------
# Defect 3 - the gate-decide client submit has a real timeout/abort UX.
# --------------------------------------------------------------------------

def _gate_decide_fn_source() -> str:
    """The gateDecide() function body, brace-matched - never a fixed line
    window: an UNRELATED setTimeout/clearTimeout pair already exists
    elsewhere in this file (a notice-dismiss timer at ~1297-1298), so a
    whole-file grep for those tokens would false-pass today."""
    src = _TSX.read_text(encoding="utf-8")
    start = src.index("const gateDecide = async")
    i = src.index("{", start)
    depth, j = 0, i
    while True:
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return src[start:j + 1]


def test_gate_decide_has_abort_controller_wired_to_the_fetch():
    fn = _gate_decide_fn_source()
    assert "AbortController" in fn, (
        "gateDecide() has no AbortController - a wedged POST leaves "
        "'checking...' indistinguishable from a dead page forever")
    assert re.search(r"signal\s*:", fn), (
        "the AbortController's signal must be wired into the fetch() "
        "call options, or constructing it does nothing")
    assert "setTimeout(" in fn and ".abort()" in fn, (
        "a setTimeout must call the controller's .abort() - an "
        "AbortController with no timer never actually times out")
    assert "clearTimeout" in fn, (
        "the timer must be cleared on a normal (non-timeout) completion, "
        "or a finished request still fires a stray abort() later")


def test_gate_decide_surfaces_elapsed_time_while_checking():
    fn = _gate_decide_fn_source()
    assert re.search(r"elapsed", fn, re.I), (
        "gateDecide() must surface elapsed time while CHECKING - the "
        "owner cannot tell a slow machine check from a hung one "
        "otherwise (mx-d6c1df names exactly this drive-liveness failure "
        "mode)")
    assert re.search(r"Date\.now\(\)", fn), (
        "elapsed time must be computed from a real clock read "
        "(Date.now()) captured at submit time, not a hardcoded label")


def test_gate_decide_timeout_returns_control_with_a_next_action():
    fn = _gate_decide_fn_source()
    assert re.search(r"AbortError", fn), (
        "the catch block must distinguish a client-side timeout/abort "
        "(err.name === 'AbortError') from a real server refusal, or the "
        "owner gets the same opaque message either way")
    assert re.search(r"(timed out|timeout)", fn, re.I), (
        "the timeout branch must say plainly that it timed out")
    assert re.search(r"(try again|retry|re-run|check readiness)", fn,
                     re.I), (
        "the timeout message must name a next action - clearing busy "
        "silently with no guidance reproduces the same stuck-owner "
        "defect this ticket exists to fix")
    assert "setBusy(false)" in fn, (
        "on timeout, busy must still clear (control returns to the "
        "owner) - a hang that never resets busy is the CHECKING-looks-"
        "like-DEAD failure mode verbatim")
