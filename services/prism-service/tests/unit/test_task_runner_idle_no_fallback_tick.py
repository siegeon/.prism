"""task_runner is the one standing worker still ticking on a fixed
PRISM_TASK_RUNNER_INTERVAL clock even when nothing changed -- unlike its
eight siblings (gate_adjudicator, resume_actuator, ship_worker,
deploy_worker, dispatch_guard, maintenance_clock, language_alignment_worker),
which all wait on wakeups.worker_fallback_s() (None by default -- no
periodic wake at all unless an operator explicitly opts in via
PRISM_WORKER_FALLBACK_S). Owner directive 2026-09-13: "even when the
application is driving a task, it should be QUIESCENT ... this is a
REACTIVE system, not a passive constant scanning system".

This pins the production wait timeout (`stop_event is None`, the shape
`start_task_runner` actually launches) to the same shared, opt-in-only
contract, while leaving the test-only `stop_event` path (which needs a
finite wait to end deterministically) on the plain interval.
"""
import threading

from prism_service.services import task_runner as tr


def test_production_fallback_is_none_by_default(monkeypatch):
    monkeypatch.delenv("PRISM_WORKER_FALLBACK_S", raising=False)
    assert tr._fallback_timeout_s(900, None) is None, (
        "a production loop (stop_event is None) must have NO periodic "
        "fallback tick unless an operator explicitly opts in -- ticking "
        "on PRISM_TASK_RUNNER_INTERVAL regardless of activity is exactly "
        "the passive constant-scanning behaviour the owner ruled out")


def test_production_fallback_honors_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PRISM_WORKER_FALLBACK_S", "30")
    assert tr._fallback_timeout_s(900, None) == 30.0


def test_test_plumbing_keeps_the_plain_interval(monkeypatch):
    monkeypatch.delenv("PRISM_WORKER_FALLBACK_S", raising=False)
    stop_event = threading.Event()
    assert tr._fallback_timeout_s(5, stop_event) == 5
