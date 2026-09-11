"""The runner waits out a dead local engine instead of blocking the task.

Live incident 2026-09-11, task b490fabc: PRISM_INFERENCE_BACKEND=local, the
AOS `inference` container had died, and the LiteLLM proxy in front of it
answered every call with `Cannot connect to host inference.dev.internal:8080`.
The runner spent all three draft_story attempts on that outage, then blocked
the task with "the step did not produce a usable report". That reason blames
the step for an outage the step could not see.

`_engine_unreachable()` is the sibling of `_system_overloaded()`: a host
breaker at the same two call sites that refuses the whole tick while the
engine cannot answer. No claim, no dispatch, no attempt spent, so the task
resumes by itself when the engine returns.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

PROXY = "http://proxy.test:8087"
LIVENESS = PROXY + "/health/liveliness"
ENGINE = "http://engine.test:8086/health"


@pytest.fixture()
def local_backend(monkeypatch):
    from prism_service import config
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(config, "INFERENCE_BACKEND", "local")
    monkeypatch.setattr(config, "LOCAL_INFERENCE_BASE_URL", PROXY)
    monkeypatch.setattr(config, "LOCAL_ENGINE_HEALTH_URL", ENGINE)
    monkeypatch.setattr(tr, "_ENGINE_PROBE", {"at": None, "down": False})
    return tr


def _answers(monkeypatch, tr, up: dict) -> list:
    """Fake every probe: `up` maps a URL to whether it answers 2xx."""
    calls: list = []

    def _fake(url, timeout_s=2.0):
        calls.append(url)
        return up.get(url, False)

    monkeypatch.setattr(tr, "_http_ok", _fake)
    return calls


def test_the_default_backend_never_probes(monkeypatch):
    from prism_service import config
    from prism_service.services import task_runner as tr

    monkeypatch.setattr(config, "INFERENCE_BACKEND", "claude")
    calls = _answers(monkeypatch, tr, {})
    assert tr._engine_unreachable() is False
    assert calls == [], "the default backend has no local engine to probe"


def test_a_healthy_proxy_and_engine_let_the_tick_run(local_backend,
                                                     monkeypatch):
    tr = local_backend
    _answers(monkeypatch, tr, {LIVENESS: True, ENGINE: True})
    assert tr._engine_unreachable() is False


def test_a_dead_proxy_refuses_the_tick(local_backend, monkeypatch, capsys):
    tr = local_backend
    _answers(monkeypatch, tr, {ENGINE: True})
    assert tr._engine_unreachable() is True
    err = capsys.readouterr().err
    assert PROXY in err and "unreachable" in err, err


def test_a_live_proxy_with_a_dead_engine_refuses_the_tick(local_backend,
                                                           monkeypatch):
    """The 2026-09-11 shape: LiteLLM answered, the engine behind it was gone."""
    tr = local_backend
    _answers(monkeypatch, tr, {LIVENESS: True})
    assert tr._engine_unreachable() is True


def test_an_empty_engine_url_probes_the_proxy_only(local_backend,
                                                   monkeypatch):
    from prism_service import config

    tr = local_backend
    monkeypatch.setattr(config, "LOCAL_ENGINE_HEALTH_URL", "")
    calls = _answers(monkeypatch, tr, {LIVENESS: True})
    assert tr._engine_unreachable() is False
    assert calls == [LIVENESS]


def test_the_verdict_is_reused_within_one_tick(local_backend, monkeypatch):
    """sweep_once asks per project. 130 projects must not mean 260 probes."""
    tr = local_backend
    calls = _answers(monkeypatch, tr, {LIVENESS: True, ENGINE: True})
    tr._engine_unreachable()
    tr._engine_unreachable()
    assert len(calls) == 2, calls


def test_http_ok_is_false_on_a_refused_port():
    """A real socket, not a fake: port 1 on loopback refuses the connect."""
    from prism_service.services import task_runner as tr

    assert tr._http_ok("http://127.0.0.1:1/health", timeout_s=1.0) is False


def _breakers(monkeypatch, tr, engine_down: bool) -> None:
    monkeypatch.setattr(tr, "_spend_ceiling_crossed", lambda: False)
    monkeypatch.setattr(tr, "_system_overloaded", lambda: False)
    monkeypatch.setattr(tr, "_engine_unreachable", lambda: engine_down)


def test_eligible_tasks_refuses_while_the_engine_is_down(monkeypatch):
    from prism_service.services import task_runner as tr

    _breakers(monkeypatch, tr, engine_down=True)
    assert tr.eligible_tasks("any-project") == []


def test_sweep_once_starts_nothing_while_the_engine_is_down(monkeypatch):
    from prism_service.services import task_runner as tr

    _breakers(monkeypatch, tr, engine_down=True)
    monkeypatch.setattr(
        tr, "run_one_step",
        lambda pid, tid: pytest.fail("dispatched onto a dead engine"))
    assert tr.sweep_once() is None


# The proof all three of b490fabc's draft_story attempts returned.
LIVE_PROOF = (
    "API Error: 500 litellm.InternalServerError: InternalServerError: "
    "OpenAIException - Cannot connect to host inference.dev.internal:8080 "
    "ssl:<ssl.SSLContext object at 0x7c1267be2b70> [Name or service not "
    "known]. Received Model Group=claude-haiku-4-5-20251001")


def test_an_unreachable_endpoint_is_named_not_called_a_crash():
    from prism_service.services import task_runner as tr

    result = types.SimpleNamespace(exit_code=1, duration_s=4.0)
    reason = tr._failure_reason(result, 400.0, proof=LIVE_PROOF)
    assert "unreachable" in reason, reason
    assert "inference.dev.internal:8080" in reason, reason
    assert "crash/auth/truncated" not in reason, reason


def test_an_ordinary_crash_keeps_its_old_words():
    from prism_service.services import task_runner as tr

    result = types.SimpleNamespace(exit_code=1, duration_s=4.0)
    reason = tr._failure_reason(result, 400.0, proof="half a report, cut off")
    assert reason == ("exit=1, non-graceful failure "
                      "(crash/auth/truncated mid-turn)")
