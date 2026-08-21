"""The conductor's flow was missing its final step for AUTONOMOUS tasks:
ship + validate (owner 2026-08-21, task <this task>).

`ship_worker._awaiting_ship`/`sweep_once` only ever ships a task carrying a
RECORDED HUMAN 'ship=queued' approval — the demo/review, ship-on-approve
track (task 5b6aefc1). A proof_type=test task never records that row (no
human approval is required for it; the adjudicator seat, task eaafdf75,
owns its own green) — so a fully machine-driven task can clear every OTHER
green_gate tooth (fresh receipt, reachability, candidate-controls-judge,
screen-claim) and then park FOREVER on `_unshipped_gate_reason` alone,
because nothing else ever pushes/PRs/merges its branch. That is the gap the
owner pointed at directly: "missing a final step in our conductor flow to
ship and validate" — and it is not a daemon-restart concern at all, it is
plain deterministic git+gh mechanics, exactly like the existing human track.

This suite pins the fix: `_awaiting_ship_machine` finds these parked,
proof_type-non-demo/review tasks, and `sweep_once` ships them through the
SAME deterministic `ship_task` pipeline `test_ship_worker.py` already
proves (push -> pr create -> pr checks -> pr merge -> fetch), then calls
`_adjudicate_after_ship` — the machine-track twin of `_replay_owner_
approval` — which re-runs `adjudicate_green_gate` so the SAME pre-flight
teeth decide, now that shipped-ness has been cleared for real.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(cwd, *args) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, env=env,
                          capture_output=True, text=True).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _unshipped_workspace(tmp_path: Path, task_id: str):
    """A real bare `origin` + a work checkout whose `[task:<id8>]` commit
    sits on the task's own branch, unpushed — same shape as
    test_ship_worker.py's fixture, parametrized by a REAL task id."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "--bare", "-q", str(origin))

    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(origin), str(work))
    _write(work, "README.md", "# baseline\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "baseline")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-q", "origin", "main")

    branch = f"prism/ws/{task_id}"
    _git(work, "checkout", "-q", "-b", branch)
    _write(work, "feature.txt", "the change under test\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm",
         f"feat: machine-track task lands its own branch\n\n[task:{task_id[:8]}]")
    return origin, work, branch


class FakeGh:
    """Records every argv; runs `git` for real, answers only `gh` — a
    genuine squash-style merge into origin's own main, same as
    test_ship_worker.py's FakeGh."""

    def __init__(self, origin: Path, branch: str):
        self.origin = origin
        self.branch = branch
        self.calls: list[list] = []

    def __call__(self, argv, cwd=None):
        self.calls.append(list(argv))
        head = " ".join(str(a) for a in argv[:3])

        if argv[0] == "git":
            proc = subprocess.run(
                [str(a) for a in argv], cwd=str(cwd) if cwd else None,
                capture_output=True, text=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "t",
                     "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                     "GIT_COMMITTER_EMAIL": "t@t"})
            return proc.returncode, proc.stdout or "", proc.stderr or ""

        if argv[0] != "gh":
            raise AssertionError(f"pipeline shelled out to {argv[0]!r}: {argv}")

        if "pr create" in head:
            return 0, "https://github.com/siegeon/.prism/pull/99\n", ""
        if "pr checks" in head:
            return 0, "all checks passed\n", ""
        if "pr merge" in head:
            tmp = self.origin.parent / "merger"
            if not tmp.exists():
                _git(self.origin.parent, "clone", "-q", str(self.origin),
                     str(tmp))
            _git(tmp, "fetch", "-q", "origin", f"{self.branch}:{self.branch}")
            _git(tmp, "checkout", "-q", "main")
            _git(tmp, "merge", "-q", "--no-ff", "-m",
                 f"merge {self.branch}", self.branch)
            _git(tmp, "push", "-q", "origin", "main")
            return 0, "merged\n", ""
        return 0, "", ""

    def stages(self) -> list[str]:
        out = []
        for c in self.calls:
            head = " ".join(str(a) for a in c[:3])
            if c[0] == "git" and c[1:2] == ["push"]:
                out.append("push")
            elif "pr create" in head:
                out.append("pr_create")
            elif "pr checks" in head:
                out.append("ci_wait")
            elif "pr merge" in head:
                out.append("merge")
        return out


def _services(tmp_path):
    from prism_service.services.task_service import TaskService
    from prism_service.services.conductor_service import ConductorService

    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc, verifier_svc=None)
    return task_svc, cond


def _walk_to_green_gate(cond, task_id: str) -> None:
    from prism_service.models.workflow import WORKFLOW_STEPS

    cond._task_svc.update(task_id, premise_notes=(
        "## Premises\n- fixture walk exercising the machine-track ship "
        "step, not a real premise claim - UNVERIFIED\n"))
    target_idx = next(i for i, s in enumerate(WORKFLOW_STEPS)
                      if s["id"] == "green_gate")
    guard = (target_idx + 1) * 3
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


def _wire_ws(monkeypatch, work: Path, branch: str, task_id: str):
    import prism_service.services.task_workspace as tw

    rec = {"task_id": task_id, "path": str(work), "branch": branch,
           "repo_root": str(work)}
    monkeypatch.setattr(tw, "workspace_for", lambda tid: dict(rec))
    monkeypatch.setattr(tw, "workspace_record", lambda tid: dict(rec),
                        raising=False)


def _shipped(repo: Path, task_id: str) -> bool:
    from prism_service.api.tasks import _is_shipped_on_main

    _git(repo, "fetch", "-q", "origin")
    return _is_shipped_on_main(str(repo), task_id)


# ---------------------------------------------------------------------------
# Eligibility: _awaiting_ship_machine
# ---------------------------------------------------------------------------


def test_machine_track_ignores_demo_review_proof_types(tmp_path, monkeypatch):
    """The human ship-on-approve track (task 5b6aefc1) owns demo/review —
    the machine track must not double-ship them."""
    from prism_service.services import ship_worker
    from prism_service.project_context import get_project

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="demo track", tags=["conductor"],
                        proof_type="demo")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    status="in_progress")
    monkeypatch.setattr(cond, "_unshipped_gate_reason", lambda task: "unshipped!")

    ctx = get_project("default")
    monkeypatch.setattr(ctx, "_conductor_svc", cond, raising=False)
    monkeypatch.setattr(ctx, "_task_svc", task_svc, raising=False)

    assert ship_worker._awaiting_ship_machine("default") == [], (
        "a demo/review task must stay on the human track, never the "
        "machine track")


def test_machine_track_finds_a_parked_unshipped_test_task(tmp_path, monkeypatch):
    from prism_service.services import ship_worker
    from prism_service.project_context import get_project

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="test track", tags=["conductor"],
                        proof_type="test")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    status="in_progress")
    monkeypatch.setattr(cond, "_unshipped_gate_reason",
                        lambda task: "green_gate: unshipped")

    ctx = get_project("default")
    monkeypatch.setattr(ctx, "_conductor_svc", cond, raising=False)
    monkeypatch.setattr(ctx, "_task_svc", task_svc, raising=False)

    assert ship_worker._awaiting_ship_machine("default") == [t.id]


def test_machine_track_skips_a_task_thats_not_actually_unshipped(
        tmp_path, monkeypatch):
    from prism_service.services import ship_worker
    from prism_service.project_context import get_project

    task_svc, cond = _services(tmp_path)
    t = task_svc.create(title="already shipped", tags=["conductor"],
                        proof_type="test")
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    status="in_progress")
    monkeypatch.setattr(cond, "_unshipped_gate_reason", lambda task: "")

    ctx = get_project("default")
    monkeypatch.setattr(ctx, "_conductor_svc", cond, raising=False)
    monkeypatch.setattr(ctx, "_task_svc", task_svc, raising=False)

    assert ship_worker._awaiting_ship_machine("default") == []


# ---------------------------------------------------------------------------
# End-to-end: sweep_once ships a machine-track task, then re-adjudicates it
# through the REAL green_gate pre-flight — never a stub verdict.
# ---------------------------------------------------------------------------


def test_sweep_ships_and_adjudicates_a_machine_track_task(tmp_path, monkeypatch):
    from prism_service.services import ship_worker
    from prism_service.services import oracle_spec as osp
    from prism_service.project_context import get_project

    task_svc, cond = _services(tmp_path)

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>ok</html>")

        def log_message(self, *a):
            return

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/probe"

    t = task_svc.create(title="ship me end to end", tags=["conductor"],
                        oracle=url, proof_type="test",
                        completion_proof=(
                            f"pytest tests/unit/test_probe.py::test_ok PASSED "
                            f"— http_probe {url} -> 200, 1 passed, 0 failed"))
    task_svc.update(t.id, status="in_progress")

    origin, work, branch = _unshipped_workspace(tmp_path, t.id)
    _wire_ws(monkeypatch, work, branch, t.id)
    assert not _shipped(work, t.id), "fixture must start UNshipped"

    monkeypatch.setattr(
        "prism_service.services.reachability_check.unreachable_entry_point_reason",
        lambda task: "")

    try:
        _walk_to_green_gate(cond, t.id)
        live = task_svc.get(t.id)
        receipt = osp.run_oracle(osp.OracleSpec.from_task(live), live,
                                 ctx={"project": "default", "workspace": str(work)})
        assert receipt.passed is True, receipt

        assert cond.adjudicate_green_gate(t.id) is None, (
            "must still refuse before shipping — the ONLY thing missing "
            "is shipped-ness")
        parked = task_svc.get(t.id)
        assert parked.gate_state == "pending"
        assert "unshipped" in (parked.gate_reason or "").lower() \
            or "origin/main" in (parked.gate_reason or "").lower(), (
            f"got {parked.gate_reason!r}")

        ctx = get_project("default")
        monkeypatch.setattr(ctx, "_conductor_svc", cond, raising=False)
        monkeypatch.setattr(ctx, "_task_svc", task_svc, raising=False)
        monkeypatch.setattr(
            "prism_service.project_context.get_all_projects",
            lambda: ["default"])
        monkeypatch.setattr(ship_worker, "_default_runner",
                            FakeGh(origin, branch))

        res = ship_worker.sweep_once()

        assert res is not None and res.get("ok") is True, res
        assert _shipped(work, t.id), (
            "the sweep must have actually pushed/PR'd/merged the branch")
        after = task_svc.get(t.id)
        assert after.gate_state == "passed", (
            f"the machine seat must re-adjudicate and pass now that "
            f"shipped-ness is cleared: gate_state={after.gate_state!r}, "
            f"gate_reason={after.gate_reason!r}")
        assert after.status == "done"
    finally:
        srv.shutdown()


def test_ship_task_calls_on_landed_after_a_real_merge(tmp_path, monkeypatch):
    """Narrower, deterministic proof that ship_task's new on_landed hook
    fires with the right arguments once the branch is genuinely on
    origin/main — the machine-track wiring point sweep_once relies on."""
    from prism_service.services import ship_worker

    task_id = "9a51e670-0e88-4506-a5b8-749f999fcbc3"
    origin, work, branch = _unshipped_workspace(tmp_path, task_id)
    _wire_ws(monkeypatch, work, branch, task_id)
    assert not _shipped(work, task_id)

    calls = []

    def _on_landed(task_svc, cond, tid):
        calls.append((task_svc, cond, tid))
        return True

    gh = FakeGh(origin, branch)
    res = ship_worker.ship_task(task_id, runner=gh, poll_interval_s=0,
                                on_landed=_on_landed)

    assert res["ok"] is True, res
    assert res["replayed"] is True
    assert calls and calls[0][2] == task_id
    assert _shipped(work, task_id), (
        "on_landed must only fire AFTER the branch is genuinely merged")


def test_sweep_once_ships_machine_track_when_no_human_track_pending(
        tmp_path, monkeypatch):
    """sweep_once must reach the machine-track scan (not just the human
    one) and pass _adjudicate_after_ship as on_landed."""
    from prism_service.services import ship_worker

    task_id = "b5b6a90b-e6f2-4a18-8bd2-a3fb87892d08"
    monkeypatch.setattr(ship_worker, "get_all_projects", None, raising=False)
    import prism_service.project_context as pc
    monkeypatch.setattr(pc, "get_all_projects", lambda: ["default"])
    monkeypatch.setattr(ship_worker, "_awaiting_ship", lambda pid: [])
    monkeypatch.setattr(ship_worker, "_awaiting_ship_machine",
                        lambda pid: [task_id] if pid == "default" else [])

    captured = {}

    def _fake_ship_task(tid, pid, on_landed=None, **kw):
        captured["tid"] = tid
        captured["pid"] = pid
        captured["on_landed"] = on_landed
        return {"ok": True, "stage": "merged", "error": "", "pr": 1}

    monkeypatch.setattr(ship_worker, "ship_task", _fake_ship_task)

    res = ship_worker.sweep_once()

    assert res == {"ok": True, "stage": "merged", "error": "", "pr": 1}
    assert captured["tid"] == task_id
    assert captured["pid"] == "default"
    assert captured["on_landed"] is ship_worker._adjudicate_after_ship
