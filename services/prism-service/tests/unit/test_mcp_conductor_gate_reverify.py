"""MCP conductor_gate honest rubric re-verify (task 755724ea).

TOOLING GAP found live 2026-07-02: the service-level gate_decide
(conductor_service.py) and REST POST /api/conductor/gate both accept
``re_verify=true`` — an HONEST re-score of a FAILED gate (re-runs the
rubric/verifier fresh; NOT an override, no manual-override audit row) —
but the MCP ``conductor_gate`` tool schema/handler in mcp/tools.py DROP
the param. Over MCP a failed rubric gate (story_gate/plan_gate) is only
recoverable via override=true — the exact wrong incentive (doctrine:
never override rubric gates).

These tests pin the REAL SEAM — handle_tool, the way the MCP server
invokes the verb — not gate_decide in isolation (already covered by
tests/unit/test_conductor_re_verify_failed_gate.py):

  AC-1 — failed story_gate rubric + now-compliant plan_doc +
         conductor_gate(approve, re_verify=true) THROUGH the dispatcher
         releases the gate on merit, no override, no manual-override row.
  AC-2 — the same MCP call WITHOUT re_verify stays refused (param is
         load-bearing; default behavior unchanged).
  AC-3 — the conductor_gate tool inputSchema advertises re_verify so
         MCP clients can discover it.

AC-1 and AC-3 FAIL today: the schema has no re_verify property and the
handler never forwards it, so the honest recovery is refused over MCP.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


_PID = "test-mcp-gate-reverify"

# Satisfies the story_complete rubric (governance_rubrics.yaml): all
# required sections + AC-n ids each carrying the oracle: marker.
_COMPLIANT_STORY = """## Summary

Thread re_verify through the MCP conductor_gate schema and handler.

## Requirements

- FR-1: the MCP handler forwards re_verify to gate_decide.

## Acceptance Criteria

- AC-1: a failed rubric gate re-scores honestly over MCP. - oracle: this test.
- AC-2: omitting re_verify keeps the failed gate refused. - oracle: this test.
"""

# Misses the rubric on purpose: no Requirements/Acceptance Criteria
# sections, no AC ids, no oracles — the rubric MUST fail this.
_NONCOMPLIANT_STORY = "## Summary\n\nJust a summary, no ACs.\n"


def _isolated_project(tmp_path, monkeypatch, pid=_PID):
    from prism_service import config as cfg
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    # get_project no longer creates on miss (d37193da): seed explicitly.
    cfg.project_data_dir(pid)
    from prism_service import project_context as pc
    pc._contexts.clear()
    return pid


def _call(name, arguments, pid):
    from prism_service.mcp.tools import handle_tool
    out = asyncio.run(handle_tool(name, arguments, project_id=pid))
    assert len(out) == 1
    return json.loads(out[0].text)


def _task_svc(pid):
    from prism_service.project_context import get_project
    return get_project(pid).task_svc


def _fail_story_gate(tmp_path, monkeypatch):
    """Drive a task THROUGH the dispatcher to a latched-failed story_gate:
    advance to the gate with a non-compliant plan_doc, approve honestly,
    rubric fails -> gate_state='failed'. Returns (pid, task_id)."""
    pid = _isolated_project(tmp_path, monkeypatch)
    tid = _call("task_create", {"title": "reverify over MCP"}, pid)["id"]
    _call("task_update", {"id": tid, "plan_doc": _NONCOMPLIANT_STORY}, pid)
    # "" -> review_previous_notes -> draft_story -> story_gate (pending)
    for _ in range(3):
        _call("conductor_advance", {"id": tid, "session_id": "author-sid"}, pid)
    snap = _task_svc(pid).get(tid)
    assert snap.workflow_step == "story_gate"
    assert snap.gate_state == "pending"

    first = _call("conductor_gate", {
        "id": tid, "action": "approve", "session_id": "author-sid",
        "reason": "authored story; rubric consult"}, pid)
    assert first["ok"] is False, "non-compliant doc must fail the rubric"
    assert _task_svc(pid).get(tid).gate_state == "failed"
    return pid, tid


def _manual_override_rows(pid, task_id):
    return [r for r in _task_svc(pid).history(task_id)
            if r.action == "gate_decide" and r.actor == "manual-override"]


# ----------------------------------------------------------------------
# AC-3 — the tool schema advertises re_verify (discoverability)
# ----------------------------------------------------------------------


def test_conductor_gate_schema_advertises_re_verify():
    from prism_service.mcp.tools import TOOLS
    props = {t.name: t for t in TOOLS}["conductor_gate"].inputSchema["properties"]
    assert "re_verify" in props, (
        "conductor_gate inputSchema must advertise re_verify — without it "
        "MCP clients cannot discover the honest recovery and are pushed "
        "toward override=true on failed rubric gates")
    assert props["re_verify"].get("type") == "boolean"


# ----------------------------------------------------------------------
# AC-1 — re_verify over MCP re-scores a failed rubric gate on merit
# ----------------------------------------------------------------------


def test_mcp_re_verify_recovers_failed_rubric_gate(tmp_path, monkeypatch):
    pid, tid = _fail_story_gate(tmp_path, monkeypatch)

    # Fix the named gap: the doc is now rubric-compliant.
    _call("task_update", {"id": tid, "plan_doc": _COMPLIANT_STORY}, pid)

    # Honest re-score THROUGH the dispatcher — no override.
    res = _call("conductor_gate", {
        "id": tid, "action": "approve", "re_verify": True,
        "session_id": "author-sid",
        "reason": "fixed missing AC sections; rubric re-scored"}, pid)

    assert res["ok"] is True, (
        "conductor_gate(approve, re_verify=true) over MCP must re-run the "
        f"rubric and pass on the compliant doc — got {res!r}; the handler "
        "is dropping the re_verify param before gate_decide")
    assert res["gate_state"] == "passed"
    assert res.get("override") is not True, "honest release, not an override"
    assert res.get("re_verify") is True
    assert _task_svc(pid).get(tid).gate_state == "passed"
    # No manual-override audit row anywhere on this task.
    assert _manual_override_rows(pid, tid) == []


# ----------------------------------------------------------------------
# AC-2 — without re_verify the failed gate stays refused (unchanged)
# ----------------------------------------------------------------------


def test_mcp_failed_gate_plain_approve_still_refused(tmp_path, monkeypatch):
    pid, tid = _fail_story_gate(tmp_path, monkeypatch)
    _call("task_update", {"id": tid, "plan_doc": _COMPLIANT_STORY}, pid)

    # Same call, re_verify omitted -> still refused; param is load-bearing.
    res = _call("conductor_gate", {
        "id": tid, "action": "approve", "session_id": "author-sid",
        "reason": "fixed missing AC sections; rubric re-scored"}, pid)

    assert res["ok"] is False
    assert _task_svc(pid).get(tid).gate_state == "failed"
    assert "re_verify" in res.get("reason", "") or "override" in res.get(
        "reason", ""), f"refusal must name the recovery paths — got {res!r}"
