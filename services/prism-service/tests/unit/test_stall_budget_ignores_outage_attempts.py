"""An attempt that never reached a model does not spend the step's budget.

Live on task b490fabc, 2026-09-11: three draft_story attempts died on a dead
AOS engine ("API Error: 500 ... Cannot connect to host
inference.dev.internal:8080"). After the engine came back and the task went
pending -> in_progress, _stall_count still read 3, and the runner re-blocked
the task 45 s later WITHOUT one attempt. An outage is not the step's failure,
so its attempts must not count. A report that only MENTIONS a refused
connection in its own prose is a real attempt and still counts.
"""
from __future__ import annotations

import types

from prism_service.services import task_runner as tr

LIVE_PROOF = (
    "API Error: 500 litellm.InternalServerError: InternalServerError: "
    "OpenAIException - Cannot connect to host inference.dev.internal:8080 "
    "ssl:<ssl.SSLContext object at 0x7c1267be2b70> [Name or service not "
    "known]. Received Model Group=claude-haiku-4-5-20251001")


def _svc(*proofs: str):
    rows = [types.SimpleNamespace(
        action=tr.ATTEMPT_ACTION,
        details=f"step=draft_story; advanced=false; proof={p}")
        for p in proofs]
    return types.SimpleNamespace(history=lambda _tid: rows)


def test_outage_attempts_do_not_spend_the_budget():
    svc = _svc(LIVE_PROOF, LIVE_PROOF, LIVE_PROOF)
    assert tr._stall_count(svc, "t", "draft_story") == 0


def test_ordinary_failures_still_spend_it():
    svc = _svc("half a report", "a rubric miss", "no premises cited")
    assert tr._stall_count(svc, "t", "draft_story") == 3


def test_a_report_that_mentions_a_refused_connection_still_counts():
    """Prose about networking is a real attempt, not a gateway error."""
    prose = ("## Story\nWhen the proxy answers Connection refused, the "
             "runner must name the endpoint.")
    assert tr._stall_count(_svc(prose, prose, prose), "t", "draft_story") == 3


def test_a_mix_counts_only_the_real_attempts():
    svc = _svc(LIVE_PROOF, "a rubric miss", LIVE_PROOF)
    assert tr._stall_count(svc, "t", "draft_story") == 1
