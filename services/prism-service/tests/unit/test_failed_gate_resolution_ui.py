"""RED scaffold — a failed gate must be resolved, not closed around
(task b43b33b8).

A task parked at gate_state=failed (or pending) can be closed with one
click from the task page header, and nothing on the server stops it:
TaskService.update (the chokepoint BOTH write surfaces funnel through)
sets status=done, stamps completed_at, and writes a bare change line —
the load-bearing gate is BYPASSED with no actor and no reason anywhere.

These tests pin the fix per the approved plan_doc:
  * AC-1  REST PATCH to done refuses on a failed gate (4xx naming the
          gate; status unchanged, completed_at unstamped)
  * AC-2  the guard lives in TaskService.update itself (pending AND
          failed), and mcp task_update forwards gate_bypass_reason
  * AC-3  an explicit bypass with a reason succeeds AND writes a
          gate_decide-shaped history row carrying actor + reason
  * AC-4  a blank/whitespace reason refuses and appends NO history row
  * AC-5  a passed gate still closes (conductor terminal close intact),
          pinned with a READ-ONLY ordering scan of conductor_service.py
  * AC-6  an ungated task closes in one click exactly as today
  * AC-7  the done button routes a gated close through a reason prompt
          and surfaces the server refusal (fetch does not reject on 4xx)
  * AC-8  the v6.9.2 failed-gate controls are UNREGRESSED (verify, not
          rebuild): gateDecide POSTs /api/conductor/gate, override
          demands a reason, task.gate_reason is rendered

AC-1..AC-4 and AC-7 FAIL against the current source (no guard, no
GatedDoneRefused, no bypass plumbing, setStatus never checks res.ok).
AC-5, AC-6 and AC-8 pass today: they are regression pins that keep the
fix from freezing legitimate closes or rebuilding shipped controls.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are
pinned by asserting the ACTUAL TSX source (the convention documented at
test_conductor_page_animated_cleanup_ui.py). Every TSX assertion parses
the enclosing block by BALANCED BRACKET DEPTH from a literal marker —
never a fixed character window — and the comment stripper carries the
(?<!\\)// guard (mx-5f916a): TaskDetailPage.tsx embeds escaped-slash
regex literals that the unguarded idiom eats.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # services/prism-service
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_TASK_DETAIL = _SRC / "pages" / "TaskDetailPage.tsx"
_CONDUCTOR_PY = (
    _SERVICE_ROOT / "prism_service" / "services" / "conductor_service.py")
_TOOLS_PY = _SERVICE_ROOT / "prism_service" / "mcp" / "tools.py"


# ---------------------------------------------------------------------------
# helpers — house conventions
# ---------------------------------------------------------------------------

def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy a
    source assertion (the repeated failure mode in the lessons). The
    (?<!\\)// guard keeps escaped-slash regex literals (e.g. /\\/\\//)
    from swallowing the rest of their line — without it every balanced
    scan downstream of such a line desyncs (mx-5f916a)."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _balanced_block(src: str, open_idx: int) -> str:
    """src[open_idx] must open a bracket. Returns the substring through its
    matching close, tracking (), [] and {} together — robust to JSX mixing
    brace and paren groups, which a fixed-width slice is not (a comment
    above an element has pushed a real guard out of a fixed window
    before)."""
    assert src[open_idx] in "([{", src[open_idx]
    pairs = {"(": ")", "[": "]", "{": "}"}
    closers = set(pairs.values())
    stack: list[str] = []
    for i in range(open_idx, len(src)):
        ch = src[i]
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in closers:
            assert stack and ch == stack[-1], (
                f"bracket desync at {i}: expected {stack[-1] if stack else '?'},"
                f" got {ch}")
            stack.pop()
            if not stack:
                return src[open_idx:i + 1]
    raise AssertionError(f"unbalanced brackets from index {open_idx}")


def _arrow_fn_body(src: str, decl_marker: str) -> str:
    """The balanced {...} body of `const <name> = async (...) => {...}`,
    located from its declaration marker."""
    idx = src.index(decl_marker)
    arrow = src.index("=>", idx)
    brace = src.index("{", arrow)
    return _balanced_block(src, brace)


def _refusal_type():
    """The narrow service-layer refusal the plan names. None while the
    guard does not exist — each test asserts on that explicitly so the
    red run reads as a missing feature, not a collection error."""
    from prism_service.services import task_service
    return getattr(task_service, "GatedDoneRefused", None)


@pytest.fixture()
def svc(tmp_path):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / "tasks.db"))


def _gated_task(svc, gate_state: str):
    t = svc.create(title="gated task")
    svc.update(t.id, status="in_progress", workflow_step="green_gate",
               gate_state=gate_state)
    return svc.get(t.id)


# ---------------------------------------------------------------------------
# AC-1 — the REST surface a human's click lands on refuses the silent close
# ---------------------------------------------------------------------------

def test_rest_patch_to_done_is_refused_on_a_failed_gate(svc, monkeypatch):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import prism_service.api.tasks as tasks_mod
    from prism_service.api.auth import authorize_project_request

    t = _gated_task(svc, "failed")
    monkeypatch.setattr(tasks_mod, "get_project",
                        lambda project: SimpleNamespace(task_svc=svc))
    app = FastAPI()
    app.include_router(tasks_mod.router, prefix="/api/tasks")
    app.dependency_overrides[authorize_project_request] = lambda: None
    client = TestClient(app)

    r = client.patch(f"/api/tasks/{t.id}", params={"project": "prism"},
                     json={"status": "done"})
    assert 400 <= r.status_code < 500, (
        f"a PATCH to done on a failed gate must be REFUSED with a 4xx, got "
        f"{r.status_code}: {r.text[:200]}")
    assert "gate" in r.text.lower(), (
        f"the refusal must NAME the unresolved gate so the human knows the "
        f"resolution path: {r.text[:200]}")

    fresh = svc.get(t.id)
    assert fresh.status != "done", "the refused close must not mutate status"
    assert not (fresh.completed_at or "").strip(), (
        "completed_at must stay unstamped on a refused close")


# ---------------------------------------------------------------------------
# AC-2 — the guard lives in the ONE chokepoint both surfaces funnel through
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("gate_state", ["pending", "failed"])
def test_the_guard_lives_in_the_shared_chokepoint(svc, gate_state):
    t = _gated_task(svc, gate_state)
    exc = _refusal_type()
    assert exc is not None, (
        "task_service defines no GatedDoneRefused — the done transition has "
        "no gate guard at the chokepoint (TaskService.update), so both the "
        "REST and MCP surfaces silently close around an unresolved gate")
    with pytest.raises(exc):
        svc.update(t.id, status="done")
    fresh = svc.get(t.id)
    assert fresh.status != "done"
    assert not (fresh.completed_at or "").strip()


def test_mcp_task_update_forwards_the_bypass_reason():
    """AC-2 (MCP half): the task_update handler's forwarded-key tuple must
    carry gate_bypass_reason, or an agent can never supply the audited
    bypass at all (the handler builds update_kwargs from a FIXED tuple)."""
    src = _TOOLS_PY.read_text(encoding="utf-8")
    start = src.index('if name == "task_update":')
    end = src.index('task_svc.update(arguments["id"]', start)
    handler = src[start:end]
    assert "gate_bypass_reason" in handler, (
        "mcp task_update does not forward gate_bypass_reason — the fixed "
        "key tuple silently drops it, so the MCP surface cannot express "
        "the audited bypass")


# ---------------------------------------------------------------------------
# AC-3 — the intentional bypass survives and is AUDITED
# ---------------------------------------------------------------------------

def test_an_audited_bypass_records_actor_and_reason(svc):
    t = _gated_task(svc, "failed")
    out = svc.update(t.id, status="done",
                     gate_bypass_reason="verifier wedged; owner said ship it")
    assert out is not None and out.status == "done", (
        "a done transition carrying a non-blank bypass reason must succeed")

    rows = svc.history(t.id)
    bypass_rows = [r for r in rows
                   if "gate_bypass" in (getattr(r, "action", "") or "")]
    assert bypass_rows, (
        "the bypass must append a gate_decide-shaped history row (action "
        "gate_bypass) — a bare 'status: ... -> done' change line is exactly "
        f"the silent flip this task exists to end; got actions: "
        f"{[getattr(r, 'action', '') for r in rows]}")
    row = bypass_rows[-1]
    details = getattr(row, "details", "") or ""
    assert "verifier wedged" in details, (
        f"the audit row must carry the supplied reason text: {details!r}")
    actor = getattr(row, "actor", "") or ""
    assert actor.strip(), "the audit row must carry a non-empty actor"


# ---------------------------------------------------------------------------
# AC-4 — a blank reason refuses, and the audit trail can never be empty
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_bypass_reason_is_refused_and_writes_no_row(svc, blank):
    t = _gated_task(svc, "failed")
    exc = _refusal_type()
    assert exc is not None, (
        "task_service defines no GatedDoneRefused — blank-reason refusal "
        "cannot exist without the guard")
    before = len(svc.history(t.id))
    with pytest.raises(exc):
        svc.update(t.id, status="done", gate_bypass_reason=blank)
    assert len(svc.history(t.id)) == before, (
        "a refused bypass must append NO history row (the rewind_task "
        "contract: a blank reason never reaches the audit trail)")
    assert svc.get(t.id).status != "done"


# ---------------------------------------------------------------------------
# AC-5 — the conductor's legitimate terminal close is UNBROKEN
# ---------------------------------------------------------------------------

def test_a_passed_gate_still_closes(svc):
    t = svc.create(title="green task")
    svc.update(t.id, workflow_step="green_gate", gate_state="passed")
    out = svc.update(t.id, status="done")
    assert out is not None and out.status == "done", (
        "a task whose gate_state is 'passed' must close with no bypass "
        "reason — the conductor's terminal close depends on it")
    assert (svc.get(t.id).completed_at or "").strip()


def test_conductor_sets_gate_passed_before_it_writes_done():
    """READ-ONLY ordering pin (conductor_service.py is a POLICY FILE and is
    NOT edited by this slice): gate_decide writes gate_state='passed'
    BEFORE it writes status='done', which is the premise that keeps the
    new guard out of the conductor's terminal-close path."""
    src = _CONDUCTOR_PY.read_text(encoding="utf-8")
    i_passed = src.index('gate_state="passed",')
    i_done = src.index('status="done")', i_passed)
    assert i_passed < i_done, (
        "expected the gate_state='passed' write to precede the "
        "status='done' write in gate_decide — if this ordering moves, the "
        "done-transition guard would refuse the conductor's own terminal "
        "close")


# ---------------------------------------------------------------------------
# AC-6 — an ordinary un-gated task is untouched
# ---------------------------------------------------------------------------

def test_an_ungated_task_closes_in_one_click(svc):
    t = svc.create(title="plain task")
    out = svc.update(t.id, status="done")
    assert out is not None and out.status == "done", (
        "gate_state='none' must close exactly as today — the guard must "
        "not become a blanket freeze on closing tasks")
    assert (svc.get(t.id).completed_at or "").strip(), (
        "completed_at must still be stamped on an ordinary close")


# ---------------------------------------------------------------------------
# AC-7 — the done button a person actually clicks stops being a silent bypass
# ---------------------------------------------------------------------------

def test_the_done_button_routes_a_gated_close_through_a_reason():
    src = _strip_comments(_TASK_DETAIL.read_text(encoding="utf-8"))

    # The RENDERED transition-button branch (transitions.map), parsed by
    # balanced bracket depth from the literal marker.
    m = re.search(r"transitions\.map\(", src)
    assert m, "expected the transitions.map render in TaskDetailPage.tsx"
    branch = _balanced_block(src, src.index("(", m.start()))
    assert re.search(r"gate_state|gateBlocksDone", branch), (
        "the rendered done-button branch never references the task's gate "
        "state — a done click on a gated task PATCHes immediately, the "
        "exact silent one-click bypass this task exists to end")

    # setStatus must send the collected reason and check the response —
    # fetch() does NOT reject on 4xx, so without a res.ok check the new
    # 422 refusal would render as a successful close.
    body = _arrow_fn_body(src, "const setStatus = async (")
    assert "gate_bypass_reason" in body, (
        "setStatus never sends gate_bypass_reason — the reason a human "
        "types has no way to reach the server")
    assert re.search(r"\.ok\b", body), (
        "setStatus never checks the response's ok flag — fetch does not "
        "reject on 4xx, so a server refusal would be announced as "
        "'Moved to done.'")


# ---------------------------------------------------------------------------
# AC-8 — the v6.9.2 failed-gate resolution controls hold (verified, NOT
# rebuilt — v6.9.2 shipped them; this is a regression pin, not new work)
# ---------------------------------------------------------------------------

def test_the_v692_failed_gate_controls_are_unregressed():
    raw = _TASK_DETAIL.read_text(encoding="utf-8")
    src = _strip_comments(raw)

    body = _arrow_fn_body(src, "const gateDecide = async (")
    assert "/api/conductor/gate" in body, (
        "gateDecide no longer POSTs /api/conductor/gate — failed-gate "
        "recovery (shipped v6.9.2) has regressed")
    assert re.search(r"gateReason\.trim\(\)", body) and "needsReason" in body, (
        "gateDecide's override/reject path no longer demands a non-blank "
        "reason before sending (v6.9.2 contract)")

    # The stored refusal reason must reach the human via a RENDERED element
    # (a JSX expression consuming task.gate_reason), never only a comment.
    assert re.search(r"\{task\.gate_reason", src), (
        "no rendered element consumes task.gate_reason — the human cannot "
        "see WHY the gate failed (v6.9.2 contract)")
