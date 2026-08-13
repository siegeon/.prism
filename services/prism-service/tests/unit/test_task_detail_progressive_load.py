"""RED — task-detail page opens instantly and fills in progressively
(task d8f73a68).

GET /api/tasks/{id} (api/tasks.get_task) currently assembles the ENTIRE
detail payload inline, including four transcript-parsing attachments
(_attach_turn_tokens, _step_token_totals, _attach_spend, _build_timeline)
that walk .jsonl transcripts on disk on every request. This pins the split:

  FR-1 — get_task's body carries none of those four calls; the lean route
    returns only task/history/sessions/mirrors/mirror/phase_progress/
    activity/has_prototype/artifact_url, with ZERO transcript work.
  FR-2 — a new GET /{task_id}/detail-extras route (registered beside
    GET /{task_id}/trace) serves spend, step_tokens, timeline and per-turn
    turn_tokens keyed by TaskHistory.id, and 404s on an unknown task id.
  FR-3/FR-4/FR-5 — TaskDetailPage.tsx's load() no longer types/awaits
    spend/step_tokens/timeline on its first-paint fetch; a dedicated
    deferred effect (mirroring the existing traceLoading pattern) fetches
    /detail-extras behind an extrasLoading skeleton flag and renders a
    VISIBLE failure (extrasError) on rejection, never a silent or
    indefinite skeleton.

Per NFR-3 these are source-scan + call-count assertions pinning WHICH code
paths do transcript work, never a wall-clock budget.

ALL FAIL today: get_task still calls all four helpers inline, GET
/{task_id}/detail-extras does not exist (404s), and TaskDetailPage.tsx's
load() still types spend/step_tokens/timeline on its one combined fetch.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_TASKS_SRC = (_SERVICE_ROOT / "prism_service" / "api" / "tasks.py").read_text(
    encoding="utf-8")
_DETAIL_TSX = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"
               / "TaskDetailPage.tsx").read_text(encoding="utf-8")

_BASE = "2026-01-01T00:"


def _iso(mm: int, ss: int) -> str:
    return f"{_BASE}{mm:02d}:{ss:02d}+00:00"


def _function_body(src: str, name: str) -> str:
    """Slice one module-level function's body out by name, from its
    `def name(` to the next `def ` at the SAME indent — robust to line
    drift elsewhere in the file (mirrors test_task_views_dont_overfetch.py's
    helper of the same name)."""
    lines = src.splitlines()
    start = None
    indent = None
    pat = re.compile(r"(\s*)(?:async )?def " + re.escape(name) + r"\(")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if m:
            start, indent = i, m.group(1)
            break
    assert start is not None, f"could not find def {name}( in tasks.py"
    end = len(lines)
    end_pat = re.compile(indent + r"(?:async )?def \w+\(")
    for i in range(start + 1, len(lines)):
        if end_pat.match(lines[i]):
            end = i
            break
    return "\n".join(lines[start:end])


_GET_TASK_BODY = _function_body(_TASKS_SRC, "get_task")


# ---------------------------------------------------------------------------
# FR-1 — get_task's body carries no inline transcript work
# ---------------------------------------------------------------------------

def test_get_task_body_no_longer_calls_transcript_helpers_inline():
    for needle in ("_attach_turn_tokens(", "_step_token_totals(",
                   "_attach_spend(", "_build_timeline("):
        assert needle not in _GET_TASK_BODY, (
            f"get_task still calls {needle} inline — this transcript-"
            f"parsing work belongs on GET /{{task_id}}/detail-extras, not "
            f"the lean detail route (FR-1)"
        )


# ---------------------------------------------------------------------------
# Shared TestClient harness (mirrors test_task_activity_gantt.py's _client)
# ---------------------------------------------------------------------------

class _FakeTaskSvc:
    """Minimal stand-in for TaskService — only what get_task/detail-extras
    touch."""

    def __init__(self, task, history, sessions):
        self._task, self._history, self._sessions = task, history, sessions

    def get(self, _tid):
        return self._task

    def history(self, _tid):
        return self._history

    def sessions_for_task(self, _tid):
        return self._sessions


class _FakeCtx:
    def __init__(self, task_svc):
        self.task_svc = task_svc
        self.conductor_svc = None  # best-effort phase_progress just skips


def _history():
    return [
        {"id": 1, "action": "created", "actor": "x", "details": "",
         "timestamp": _iso(0, 0)},
        {"id": 2, "action": "updated", "actor": "x", "details": "",
         "timestamp": _iso(1, 0)},
        {"id": 3, "action": "updated", "actor": "x", "details": "",
         "timestamp": _iso(30, 0)},
    ]


def _client(monkeypatch, task, history, sessions):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import tasks as tasks_api

    ctx = _FakeCtx(_FakeTaskSvc(task, history, sessions))
    monkeypatch.setattr(tasks_api, "get_project", lambda _p: ctx)
    app = FastAPI()
    app.include_router(tasks_api.router, prefix="/api/tasks")
    return TestClient(app)


def _patch_counters(monkeypatch):
    """Replace the four transcript-work helpers with call counters so the
    test observes WHICH ROUTE invokes them — never a wall-clock budget
    (NFR-3)."""
    from prism_service.api import tasks as T
    counts = {"turn_tokens": 0, "step_tokens": 0, "spend": 0, "timeline": 0}

    def fake_turn_tokens(history, sessions, project):
        counts["turn_tokens"] += 1

    def fake_step_tokens(history, sessions, project):
        counts["step_tokens"] += 1
        return {}

    def fake_attach_spend(out, sessions, project):
        counts["spend"] += 1
        out["spend"] = {
            "main": {}, "background": {},
            "total": {"tokens": 0, "usd": 0, "priced": True,
                      "components": {}, "unpriced_tokens": 0},
            "priced": True,
        }

    def fake_build_timeline(history, sessions):
        counts["timeline"] += 1
        return {"window": {"start": 0, "end": 0}, "lanes": [], "gates": []}

    monkeypatch.setattr(T, "_attach_turn_tokens", fake_turn_tokens)
    monkeypatch.setattr(T, "_step_token_totals", fake_step_tokens)
    monkeypatch.setattr(T, "_attach_spend", fake_attach_spend)
    monkeypatch.setattr(T, "_build_timeline", fake_build_timeline)
    return counts


# ---------------------------------------------------------------------------
# FR-1 (route-level) — GET /api/tasks/{id} does zero transcript work
# ---------------------------------------------------------------------------

def test_get_task_route_makes_zero_transcript_helper_calls(monkeypatch):
    counts = _patch_counters(monkeypatch)
    task = {"id": "d8f73a68", "title": "t", "status": "in_progress",
            "priority": 0}
    client = _client(monkeypatch, task, _history(), [])

    r = client.get("/api/tasks/d8f73a68?project=proj")

    assert r.status_code == 200, r.text
    assert counts == {"turn_tokens": 0, "step_tokens": 0, "spend": 0,
                       "timeline": 0}, (
        f"GET /api/tasks/{{id}} must do zero transcript-parsing work on "
        f"the request path, observed {counts}"
    )
    body = r.json()
    for key in ("spend", "step_tokens", "timeline"):
        assert key not in body, (
            f"the lean detail route must not carry {key!r} any more — it "
            f"moved to GET /{{task_id}}/detail-extras (FR-1/FR-2)"
        )
    for key in ("task", "history", "sessions", "mirrors", "mirror",
                "has_prototype", "artifact_url"):
        assert key in body, f"lean route dropped required field {key!r}"


# ---------------------------------------------------------------------------
# FR-2 — GET /{task_id}/detail-extras: 404 on unknown id, like get_task_trace
# ---------------------------------------------------------------------------

def test_detail_extras_404s_on_unknown_task_id(monkeypatch):
    client = _client(monkeypatch, None, [], [])
    r = client.get("/api/tasks/nope/detail-extras?project=proj")
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# FR-2 — detail-extras serves spend/step_tokens/timeline/turn_tokens, the
# latter keyed by TaskHistory.id (real transcript, end-to-end)
# ---------------------------------------------------------------------------

def _write_transcript(path, sess, events):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"sessionId": sess, "timestamp": ts,
                     "message": {"role": "assistant",
                                 "usage": {"output_tokens": tok}}})
        for ts, tok in events
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_detail_extras_serves_all_four_fields_with_id_keyed_turn_tokens(
    tmp_path, monkeypatch,
):
    from prism_service.services import claude_transcripts as ct
    from prism_service.services.claude_transcripts import path_to_slug

    sess = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    source_path = str(tmp_path / "src" / "proj")
    claude_home = tmp_path / "claude"
    proj_dir = claude_home / "projects" / path_to_slug(source_path)
    # event @0:30 -> row id=2's window (0:00,1:00]; event @29:50 -> row
    # id=3's window (1:00,30:00]. Nothing precedes row id=1.
    _write_transcript(proj_dir / f"{sess}.jsonl", sess,
                       [(_iso(0, 30), 50), (_iso(29, 50), 200)])
    monkeypatch.setattr(ct, "resolve_claude_home", lambda: claude_home)
    monkeypatch.setattr(ct, "_project_source_path", lambda _p: source_path)

    task = {"id": "d8f73a68", "title": "t", "status": "in_progress",
            "priority": 0}
    sessions = [{"session_id": sess, "started_at": _iso(0, 0),
                 "ended_at": _iso(30, 0), "duration_s": 1800}]
    client = _client(monkeypatch, task, _history(), sessions)

    r = client.get("/api/tasks/d8f73a68/detail-extras?project=proj")

    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("spend", "step_tokens", "timeline", "turn_tokens"):
        assert key in body, f"detail-extras missing {key!r}: {body}"
    tt = body["turn_tokens"]
    assert tt.get("2") == 50, tt
    assert tt.get("3") == 200, tt
    assert "1" not in tt, tt


# ---------------------------------------------------------------------------
# FR-3 — TaskDetailPage.load()'s first-paint fetch no longer types the
# transcript-derived fields (source-scan, not a fetch mock)
# ---------------------------------------------------------------------------

def test_load_response_type_drops_spend_step_tokens_timeline():
    m = re.search(
        r"const load = useCallback\(async \(\) => \{"
        r".*?const d = await api\.get<\{([^}]*)\}>\(",
        _DETAIL_TSX, re.S,
    )
    assert m, "could not find TaskDetailPage's load() api.get<{...}> call"
    payload_type = m.group(1)
    for field in ("spend", "step_tokens", "timeline"):
        assert field not in payload_type, (
            f"load()'s first-paint fetch still types {field!r} on its "
            f"response — that transcript-derived field must move to "
            f"GET /api/tasks/{{id}}/detail-extras (FR-1/FR-3)"
        )


# ---------------------------------------------------------------------------
# FR-4 — a dedicated deferred effect fetches /detail-extras behind a
# skeleton flag, mirroring the existing traceLoading pattern
# ---------------------------------------------------------------------------

def test_detail_extras_fetched_via_dedicated_deferred_effect_with_skeleton():
    needle = "/api/tasks/${id}/detail-extras"
    assert needle in _DETAIL_TSX, (
        "TaskDetailPage must fetch the new deferred GET /detail-extras "
        "route (FR-4)"
    )
    assert "extrasLoading" in _DETAIL_TSX, (
        "a dedicated loading flag (mirroring traceLoading) must gate the "
        "Spend/timeline/step-token skeletons while /detail-extras is in "
        "flight (FR-4)"
    )
    idx = _DETAIL_TSX.index(needle)
    window = _DETAIL_TSX[max(0, idx - 400): idx + 400]
    assert "setExtrasLoading(true)" in window, (
        "the /detail-extras effect must flip extrasLoading true before "
        "the fetch, mirroring the existing traceLoading effect (:1129-1144)"
    )
    assert "setExtrasLoading(false)" in window, (
        "the /detail-extras effect must flip extrasLoading false once it "
        "resolves (success or failure), mirroring traceLoading's finally"
    )


# ---------------------------------------------------------------------------
# FR-5 — a rejected /detail-extras fetch renders a VISIBLE failure state,
# never a silent swallow (traceLoading's old pattern) or infinite skeleton
# ---------------------------------------------------------------------------

def test_extras_fetch_failure_renders_a_visible_error_state():
    assert "setExtrasError" in _DETAIL_TSX, (
        "a rejected /detail-extras fetch must record a visible failure "
        "state (extrasError), never fail silently like traceLoading's "
        "swallow-to-empty catch (FR-5)"
    )
    assert re.search(r"extrasError\s*&&|extrasError\s*\?", _DETAIL_TSX), (
        "extrasError must actually be RENDERED as a conditional JSX "
        "branch, not just captured and ignored — an indefinite skeleton "
        "is forbidden (FR-5)"
    )
