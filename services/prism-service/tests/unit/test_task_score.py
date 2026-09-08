"""RED scaffold - a task scores its delivery against its cost (task e6d76007).

PRISM records every commit, token and gate decision for a task and gives the
owner no single number. This suite pins the Task Score: throughput over
rework drag, from three inputs that PRISM already holds.

  SIZE    churn (added + deleted lines) over the commits carrying the
          `[task:<id8>` trailer that are reachable from origin/main. The
          same prefix needle `_shipped_sha_on_main` uses
          (prism_service/api/tasks.py:501). Unshipped work scores zero.
  EFFORT  sum of agent_runs.tokens for that task_id, through
          get_task_agent_rollup (services/agent_runs_data.py:647).
  REWORK  task_history rows: resume_actuator_parked, rewind, gate reject,
          and a resume_actuator_dispatch that bought no later advance_task
          (a quarter each).

THE NAMED MISFIRE IS THE EFFORT TERM. `task.spend` comes from `_attach_spend`,
which merges per-SESSION transcript spend, and a session holds no task_id --
today task 4e6e7417 and task 7a72ebcb both report about 8.45 billion tokens.
A ratio built on that number carries no signal, so this suite asserts the
effort term reads agent_runs and asserts the module never names spend at all.

ALL FAIL at base commit 58c91fd8: prism_service/services/task_score.py does
not exist, prism_service/api/tasks.py has no `/{task_id}/score` route, and
TaskDetailPage.tsx has no Task Score card.

Every import of a NEW symbol is lazy, inside the test that needs it, so a
pre-implementation run is a genuine RED (collection succeeds, tests FAIL,
rc==1) and never a collection ERROR (rc==2) -- the red_gate machine seat
accepts rc==1 only.

Every number asserted here is the literal value the story names (12000,
4000, 9000, 3.0, a ratio under 2.0), never the function under test applied
to its own input.
"""

from __future__ import annotations

import math
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _SERVICE_ROOT.parents[1]


# ---------------------------------------------------------------------
# helpers - disposable git repos and a real scores.db, never the checkout
# ---------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    ).stdout


def _repo_with_shipped_lines(tmp_path: Path, task_id: str, lines: int,
                             name: str = "repo") -> Path:
    """A throwaway repo whose origin/main carries ONE commit tagged for
    `task_id` that adds exactly `lines` lines."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with no trailer")
    (repo / "payload.txt").write_text("".join(f"line {i}\n" for i in range(lines)))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"the work [task:{task_id[:8]}]")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _empty_repo(tmp_path: Path, name: str = "bare-history") -> Path:
    """A repo with an origin/main that carries no task trailer at all."""
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base with no trailer")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _seed_tokens(scores_db: Path, task_id: str, tokens: int,
                 session_id: str = "sess-1", run_id: str = "run-1") -> None:
    """One agent_runs row through the REAL production writer. `model` and a
    nonzero `duration_ms` are required or _has_llm_turn_signature zeroes the
    tokens before they reach disk (agent_runs_data.py:142)."""
    from prism_service.services.agent_runs_data import upsert_agent_run
    res = upsert_agent_run(str(scores_db), {
        "run_id": run_id,
        "agent_id": f"agent-{task_id[:8]}",
        "step": "implement_tasks",
        "task_id": task_id,
        "session_id": session_id,
        "role": "dev",
        "model": "claude-opus-5",
        "started_at": "2026-09-08T13:00:00+00:00",
        "duration_ms": 1000,
        "tokens": tokens,
    })
    assert res.get("ok") is not False, f"seed row refused: {res}"


def _task(task_id: str, tags: list[str] | None = None) -> dict:
    return {"id": task_id, "tags": list(tags or [])}


def _hist(action: str, details: str = "", ts: str = "2026-09-08T13:00:00") -> dict:
    return {"action": action, "details": details, "timestamp": ts}


# ---------------------------------------------------------------------
# AC-2 - the effort term reads agent_runs, and never task.spend
# ---------------------------------------------------------------------


def test_score_task_reads_agent_runs_for_effort(tmp_path):
    """A seeded agent_runs row of 12000 tokens is the effort term."""
    from prism_service.services.task_score import score_task
    task_id = "e6d76007-2ee3-404c-a5e5-2d2b09342836"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 12000)

    out = score_task(str(repo), str(scores_db), _task(task_id), [])

    assert out["effort_tokens"] == 12000, (
        "effort must be the sum of agent_runs.tokens for this task_id, "
        f"got {out['effort_tokens']}")


def test_task_score_module_never_names_spend_or_session_outcomes():
    """AC-2's grep half. task.spend is session-scoped and holds no task_id,
    so a ratio built on it is the ticket's likely_misfire. The module must
    not import, call or name it."""
    src = (_SERVICE_ROOT / "prism_service" / "services" / "task_score.py").read_text()
    for forbidden in ("spend", "session_outcomes", "live_spend_for_session"):
        assert forbidden not in src, (
            f"task_score.py names {forbidden!r}: the effort term must read "
            "agent_runs only")


def test_task_score_module_stays_pure_data_access():
    """FR-1: no FastAPI and no project_context coupling, in the manner of
    agent_runs_data.py."""
    src = (_SERVICE_ROOT / "prism_service" / "services" / "task_score.py").read_text()
    assert "fastapi" not in src.lower(), "task_score.py must not import FastAPI"
    assert "project_context" not in src, (
        "task_score.py must not import project_context")


# ---------------------------------------------------------------------
# AC-3 - two tasks that shared one session report different effort
# ---------------------------------------------------------------------


def test_shared_session_reports_distinct_effort(tmp_path):
    """The whole reason the effort term is agent_runs: agent_runs is keyed
    on task_id, so one session that worked two tasks reports two numbers."""
    from prism_service.services.task_score import score_task
    first = "4e6e7417-0000-0000-0000-000000000001"
    second = "7a72ebcb-0000-0000-0000-000000000002"
    repo = _empty_repo(tmp_path)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, first, 4000, session_id="one-session", run_id="run-a")
    _seed_tokens(scores_db, second, 9000, session_id="one-session", run_id="run-b")

    out_first = score_task(str(repo), str(scores_db), _task(first), [])
    out_second = score_task(str(repo), str(scores_db), _task(second), [])

    assert out_first["effort_tokens"] == 4000
    assert out_second["effort_tokens"] == 9000, (
        "two tasks that shared one session must not report the same cost "
        "(today task.spend reports ~8.45 billion tokens for both)")


# ---------------------------------------------------------------------
# AC-4 - unshipped work scores zero
# ---------------------------------------------------------------------


def test_unshipped_scores_zero(tmp_path):
    """Done means shipped. A task no commit on origin/main carries scores 0,
    and says so, rather than raising or reporting a number it cannot back."""
    from prism_service.services.task_score import score_task
    task_id = "deadbeef-0000-0000-0000-00000000dead"
    repo = _empty_repo(tmp_path)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 50000)

    out = score_task(str(repo), str(scores_db), _task(task_id), [])

    assert out["size"] == 0
    assert out["score"] == 0.0
    assert "origin/main" in out["reason"], (
        "the reason must name origin/main so a reader knows the work has "
        f"not landed, got {out['reason']!r}")


def test_zero_effort_reports_none_and_never_raises(tmp_path):
    """FR-6: no measured tokens gives score None with a stated reason -
    never a divide-by-zero, never infinity, never a fabricated number."""
    from prism_service.services.task_score import score_task
    task_id = "cafe1234-0000-0000-0000-0000000012cf"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"

    out = score_task(str(repo), str(scores_db), _task(task_id), [])

    assert out["effort_tokens"] == 0
    assert out["score"] is None
    assert "token" in out["reason"].lower()


# ---------------------------------------------------------------------
# AC-5 - the size term is log-scaled, so bulk does not win
# ---------------------------------------------------------------------


def test_churn_is_log_scaled(tmp_path):
    """Ten times the churn must not buy ten times the score. At equal effort
    and equal rework, 2000 lines outranks 200 lines by log10(2001)/log10(201)
    = 1.43, which is above 1.0 and below 2.0."""
    from prism_service.services.task_score import score_task
    small_id = "11111111-0000-0000-0000-000000000200"
    large_id = "22222222-0000-0000-0000-000000002000"
    small_repo = _repo_with_shipped_lines(tmp_path, small_id, 200, name="small")
    large_repo = _repo_with_shipped_lines(tmp_path, large_id, 2000, name="large")
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, small_id, 10000, run_id="run-small")
    _seed_tokens(scores_db, large_id, 10000, run_id="run-large")

    small = score_task(str(small_repo), str(scores_db), _task(small_id), [])
    large = score_task(str(large_repo), str(scores_db), _task(large_id), [])

    assert small["size"] == 200
    assert large["size"] == 2000
    assert large["score"] > small["score"], "more delivered work must score higher"
    ratio = large["score"] / small["score"]
    assert ratio < 2.0, (
        f"ten times the churn bought {ratio:.2f}x the score: the size term "
        "has no cap")
    assert ratio == pytest.approx(math.log10(2001) / math.log10(201), rel=1e-6)


def test_lines_per_1k_reports_the_raw_uncapped_figure(tmp_path):
    """FR-5: the measured baseline over 77 shipped tasks (0.7 to 304, median
    8.8) is stated in raw lines per 1k tokens, so the payload keeps that
    figure beside the capped score. 200 lines over 10000 tokens is 20.0."""
    from prism_service.services.task_score import score_task
    task_id = "33333333-0000-0000-0000-000000000200"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)

    out = score_task(str(repo), str(scores_db), _task(task_id), [])

    assert out["lines_per_1k"] == pytest.approx(20.0, rel=1e-6)


# ---------------------------------------------------------------------
# AC-6 - rework divides the score, a barren dispatch counts a quarter
# ---------------------------------------------------------------------


def test_rework_divides_the_score(tmp_path):
    """Two parked rows (1.0 each) plus four dispatches that bought no
    advance (0.25 each) is 3.0, so the drag divisor is 4.0. Task 338f7810
    is the oscillator this term exists to rank: 37 dispatches over 4h40m."""
    from prism_service.services.task_score import score_task
    task_id = "338f7810-0000-0000-0000-000000000037"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)
    history = [
        _hist("resume_actuator_dispatch", ts="2026-09-08T13:01:00"),
        _hist("resume_actuator_dispatch", ts="2026-09-08T13:02:00"),
        _hist("resume_actuator_dispatch", ts="2026-09-08T13:03:00"),
        _hist("resume_actuator_dispatch", ts="2026-09-08T13:04:00"),
        _hist("resume_actuator_parked", ts="2026-09-08T13:05:00"),
        _hist("resume_actuator_parked", ts="2026-09-08T13:06:00"),
    ]

    out = score_task(str(repo), str(scores_db), _task(task_id), history)

    assert out["rework"] == 3.0, (
        "two parked rows plus four barren dispatches is 2.0 + 1.0 = 3.0, "
        f"got {out['rework']}")
    assert out["score"] == pytest.approx(out["throughput"] / 4.0, rel=1e-9)
    assert out["score"] == pytest.approx(math.log10(201) / 10.0 / 4.0, rel=1e-6)


def test_a_dispatch_that_bought_an_advance_is_not_rework(tmp_path):
    """FR-4: only a dispatch that no later advance_task follows carries the
    quarter weight. A dispatch that moved the task is not drag."""
    from prism_service.services.task_score import score_task
    task_id = "44444444-0000-0000-0000-000000000044"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)
    history = [
        _hist("resume_actuator_dispatch", ts="2026-09-08T13:01:00"),
        _hist("advance_task", ts="2026-09-08T13:02:00"),
    ]

    out = score_task(str(repo), str(scores_db), _task(task_id), history)

    assert out["rework"] == 0.0, (
        "a dispatch followed by an advance bought progress and is not "
        f"rework, got {out['rework']}")


def test_a_clean_task_divides_by_one(tmp_path):
    """FR-4: rework_drag is 1.0 + rework, so a task with no rework keeps
    its whole throughput."""
    from prism_service.services.task_score import score_task
    task_id = "55555555-0000-0000-0000-000000000055"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)

    out = score_task(str(repo), str(scores_db), _task(task_id), [])

    assert out["rework"] == 0.0
    assert out["score"] == pytest.approx(math.log10(201) / 10.0, rel=1e-6)


def test_a_gate_reject_counts_as_rework(tmp_path):
    """FR-4: a gate_decide row that records a reject is a full point."""
    from prism_service.services.task_score import score_task
    task_id = "66666666-0000-0000-0000-000000000066"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)
    history = [_hist("gate_decide", details="green_gate rejected: evidence stale")]

    out = score_task(str(repo), str(scores_db), _task(task_id), history)

    assert out["rework"] == 1.0, (
        f"a gate reject is one point of rework, got {out['rework']}")


# ---------------------------------------------------------------------
# AC-8 - the resolution multiplier is reported, and it is advisory
# ---------------------------------------------------------------------


def test_resolution_multiplier_is_advisory(tmp_path):
    """The owner rubric in memory mx-b0703f: codify highest, agentic less,
    hand work negative. The score REPORTS it. No gate reads it."""
    from prism_service.services.task_score import score_task
    task_id = "77777777-0000-0000-0000-000000000077"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)

    codified = score_task(str(repo), str(scores_db),
                          _task(task_id, ["resolution:codify"]), [])
    assert codified["resolution"] == "codify"
    assert codified["multiplier"] == 1.5
    assert codified["advisory"] is True

    unset = score_task(str(repo), str(scores_db), _task(task_id), [])
    assert unset["resolution"] == "unset"
    assert unset["multiplier"] == 1.0

    hand = score_task(str(repo), str(scores_db),
                      _task(task_id, ["resolution:hand"]), [])
    assert hand["multiplier"] == -1.0, (
        "hand work scores negative (owner rubric mx-b0703f)")

    agentic = score_task(str(repo), str(scores_db),
                         _task(task_id, ["resolution:agentic"]), [])
    assert agentic["multiplier"] == 1.0


def test_the_slice_edits_no_policy_file():
    """FR-11 and this task's own stop_if: a slice that edits a gate-policy
    file fails its own gates on the candidate-controls-judge tooth."""
    from prism_service.services.control_plane import POLICY_FILES
    changed = subprocess.run(
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        cwd=str(_REPO_ROOT), capture_output=True, text=True,
    ).stdout.split()
    policy_names = {Path(p).name for p in POLICY_FILES}
    offenders = [p for p in changed if Path(p).name in policy_names]
    assert offenders == [], (
        f"this slice edits a control_plane.POLICY_FILES entry: {offenders}")


# ---------------------------------------------------------------------
# AC-1 - the route answers with the three inputs
# ---------------------------------------------------------------------


def test_score_route_returns_the_three_inputs(tmp_path, monkeypatch):
    """GET /api/tasks/{id}/score, driven through the real endpoint function
    with the repo and scores.db resolved the way the sibling /delivery route
    resolves them (api/tasks.py:61, :694-695)."""
    import prism_service.api.tasks as tasks_mod
    from prism_service.api.tasks import get_task_score
    from prism_service.services import claude_transcripts as ct

    task_id = "88888888-0000-0000-0000-000000000088"
    repo = _repo_with_shipped_lines(tmp_path, task_id, 200)
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 10000)

    monkeypatch.setattr(ct, "_project_source_path", lambda project: str(repo))
    monkeypatch.setattr(tasks_mod, "_scores_db", lambda project: str(scores_db))
    fake_task = SimpleNamespace(id=task_id, tags=["resolution:codify"])
    fake_svc = SimpleNamespace(get=lambda tid: fake_task if tid == task_id else None,
                               history=lambda tid: [])
    monkeypatch.setattr(tasks_mod, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_svc))

    out = get_task_score(task_id, project="prism")

    for key in ("size", "effort_tokens", "rework", "score", "lines_per_1k",
                "resolution", "multiplier", "reason"):
        assert key in out, f"the score payload is missing {key!r}: {out}"
    assert out["size"] == 200
    assert out["effort_tokens"] == 10000


def test_score_route_404s_an_unknown_task(monkeypatch):
    """FR-8: an unknown task id is a 404, never a fabricated zero score."""
    import prism_service.api.tasks as tasks_mod
    from prism_service.api.tasks import get_task_score
    from fastapi import HTTPException

    fake_svc = SimpleNamespace(get=lambda tid: None, history=lambda tid: [])
    monkeypatch.setattr(tasks_mod, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_svc))

    with pytest.raises(HTTPException) as excinfo:
        get_task_score("99999999-0000-0000-0000-000000000099", project="prism")
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------
# AC-7 - a person opens the task page and reads the score
# ---------------------------------------------------------------------


def _tsx_without_comments() -> str:
    """The SPA has no JS test runner, so a UI acceptance criterion is pinned
    by asserting the ACTUAL TSX source (see
    tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6). Comments are
    stripped first: an explanatory comment has satisfied this kind of
    assertion three separate times before."""
    src = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
           / "TaskDetailPage.tsx").read_text(encoding="utf-8")
    out, i, n = [], 0, len(src)
    while i < n:
        if src.startswith("/*", i):
            end = src.find("*/", i + 2)
            i = n if end == -1 else end + 2
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end == -1 else end
        else:
            out.append(src[i])
            i += 1
    return "".join(out)


def test_task_detail_page_renders_the_task_score_card():
    """AC-7: the card a person actually reads, with all three inputs
    labelled. Asserted against comment-stripped source."""
    src = _tsx_without_comments()
    assert "Task Score" in src, (
        "TaskDetailPage.tsx renders no card headed 'Task Score'")
    for label in ("shipped churn", "agent tokens", "rework"):
        assert label.lower() in src.lower(), (
            f"the Task Score card does not label its {label!r} input")


def test_task_detail_page_fetches_the_score_route():
    """The card must read the real endpoint, in the manner of the existing
    delivery fetch (TaskDetailPage.tsx:1055-1063)."""
    src = _tsx_without_comments()
    assert "/score" in src, (
        "TaskDetailPage.tsx never fetches /api/tasks/{id}/score, so the card "
        "cannot be showing measured numbers")


def test_task_detail_page_states_the_honest_empty_cases():
    """FR-9: an unshipped task or one with no measured tokens must say so,
    never paint a number that reads as measured."""
    src = _tsx_without_comments().lower()
    assert "no measured tokens" in src, (
        "the card has no honest empty state for a task with no agent_runs "
        "tokens")
    assert "not shipped" in src, (
        "the card has no honest empty state for a task whose commits have "
        "not reached origin/main")


def test_an_impossible_token_row_is_not_counted_as_effort(tmp_path):
    """A count no model could produce is a wrong number, not a big one.

    Found by the live oracle walk, not by this suite: task 4e6e7417 carries
    one agent_runs row of 2,659,518,144 tokens, written before
    agent_runs_data._impossible_tokens_reason started refusing them. Summed
    blind it made the score report a 2.6 billion token task -- exactly the
    unusable cost figure this whole task exists to replace.
    """
    from prism_service.services.task_score import effort_tokens
    task_id = "fc471aed-0000-0000-0000-00000000beef"
    scores_db = tmp_path / "scores.db"
    _seed_tokens(scores_db, task_id, 8_000, run_id="real")
    from prism_service.services.agent_runs_data import upsert_agent_run
    upsert_agent_run(str(scores_db), {
        "run_id": "corrupt", "agent_id": "a", "step": "implement_tasks",
        "task_id": task_id, "session_id": "s2", "role": "dev",
        "model": "claude-opus-5", "started_at": "2026-09-08T13:00:00+00:00",
        "duration_ms": 1000, "tokens": 2_659_518_144,
    })

    assert effort_tokens(str(scores_db), task_id) == 8_000, (
        "a row above the model's own context window must be skipped, not "
        "summed: it is a wrong number, not a large one")
