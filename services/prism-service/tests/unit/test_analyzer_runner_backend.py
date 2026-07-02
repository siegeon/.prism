"""Red scaffold — env-gated pi-agent backend for the Understand analyzer runner.

Task 96341ef8 (claude-p-exit epic be898578): `inference.analyzer_runner.
run_analyzer` shells `claude_cli.invoke` for a ~35-turn Read/Glob/Grep
file-walk. This adds a PRISM_ANALYZER_BACKEND=pi seam that routes the SAME
analyzer through the pi-agent runtime (local micro model, keyless) with an
IDENTICAL return contract, while the default env stays byte-for-byte on
claude_cli. The pi runtime bridges Brain tools, NOT the Read/Glob/Grep file
tools an analyzer needs, so a skip-guard falls back to claude when those fs
tools are absent (a follow-up adds them to the pi catalog — web/** is out of
this task's scope).

Acceptance criteria pinned here:

  * AC-1 default env (unset) -> claude_cli.invoke, pi_agent.invoke never
         called; the {payload, tokens_used, wall_clock_s, status, error}
         contract is unchanged.
  * AC-2 PRISM_ANALYZER_BACKEND=pi WITH fs tools available routes through
         pi_agent.invoke (allowed_tools=ANALYZER_FS_TOOLS, project + max_turns
         + purpose passthrough); claude_cli.invoke never called.
  * AC-3 the pi path maps the runner result to the IDENTICAL contract:
         payload parsed the same way, status via the shared classifier,
         tokens_used from the pi result, wall_clock_s from ms.
  * AC-4 skip-guard: PRISM_ANALYZER_BACKEND=pi but the pi runtime advertises
         no Read/Glob/Grep tools -> fall back to claude_cli.invoke (pi never
         called); documents the follow-up need. Today the real pi catalog
         has no fs tools, so _pi_has_fs_tools() is False.
  * AC-5 the pi path routes through pi_agent.invoke, which is the single
         pi_run_log recording point (backend='pi') — no separate/duplicate
         claude ledger write on the pi path.

Every test FAILS today: analyzer_runner has no backend seam.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.inference import analyzer_runner as ar  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures / stubs
# ---------------------------------------------------------------------------

@dataclass
class _FakeCliResult:
    """Stand-in for ClaudeCliResult carrying a single JSON text block."""
    exit_code: int = 0
    parsed_events: list = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    run_id: str = "claude-run-1"
    duration_s: float = 0.1

    def final_text(self) -> str:  # parity with the real result
        return ""


def _events_with_text(text: str) -> list:
    return [{"message": {"content": [{"type": "text", "text": text}]}}]


_ARCH_JSON = '{"schema": "architecture_analyzer_v1", "layers": [{"id": "svc"}]}'


@pytest.fixture(autouse=True)
def _stub_source_dir(monkeypatch, tmp_path):
    """analyzer_runner reads a real prompt file then resolves the source dir;
    keep the prompt (architecture_analyzer.md ships) but stub the source dir."""
    monkeypatch.setattr(
        "prism_service.services.source_service.source_dir_for",
        lambda project: tmp_path, raising=False,
    )


def _stub_claude(monkeypatch, capture: dict, text: str = _ARCH_JSON):
    def _invoke(prompt, source_dir=None, **kw):
        capture["called"] = True
        capture["kwargs"] = {"prompt": prompt, "source_dir": source_dir, **kw}
        return _FakeCliResult(
            exit_code=0, parsed_events=_events_with_text(text),
            usage={"input_tokens": 10, "output_tokens": 7},
        )
    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _invoke)


def _stub_pi(monkeypatch, capture: dict, result: dict):
    def _invoke(prompt, **kw):
        capture["called"] = True
        capture["kwargs"] = {"prompt": prompt, **kw}
        return result
    monkeypatch.setattr("prism_service.inference.pi_agent.invoke", _invoke)


# ---------------------------------------------------------------------------
# AC-1: default env -> claude_cli, pi never touched, contract intact
# ---------------------------------------------------------------------------

def test_default_env_routes_claude_cli(monkeypatch):
    monkeypatch.delenv("PRISM_ANALYZER_BACKEND", raising=False)
    claude: dict = {}
    _stub_claude(monkeypatch, claude)

    def _boom(*a, **k):
        raise AssertionError("pi_agent.invoke must not run on the default path")
    monkeypatch.setattr("prism_service.inference.pi_agent.invoke", _boom)

    out = ar.run_analyzer("prism", "architecture_analyzer", "deadbeef")
    assert claude.get("called") is True
    assert set(out) == {"payload", "tokens_used", "wall_clock_s",
                        "status", "error"}
    assert out["status"] == "complete"
    assert out["tokens_used"] == 17


# ---------------------------------------------------------------------------
# AC-2 / AC-3: backend=pi with fs tools -> pi path, identical contract
# ---------------------------------------------------------------------------

def test_pi_backend_routes_pi_agent_with_fs_tools(monkeypatch):
    monkeypatch.setenv("PRISM_ANALYZER_BACKEND", "pi")
    monkeypatch.setattr(ar, "_pi_has_fs_tools", lambda: True)

    def _boom(*a, **k):
        raise AssertionError("claude_cli.invoke must not run on the pi path")
    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _boom)

    pi: dict = {}
    _stub_pi(monkeypatch, pi, {
        "ok": True, "text": _ARCH_JSON, "tokens": 42, "ms": 1500.0,
        "turns": 4, "model": "qwen3:1.7b",
    })

    out = ar.run_analyzer("prism", "architecture_analyzer", "deadbeef",
                          max_turns=35)
    assert pi.get("called") is True
    kw = pi["kwargs"]
    assert kw.get("allowed_tools") == ar.ANALYZER_FS_TOOLS
    assert kw.get("project") == "prism"
    assert kw.get("max_turns") == 35
    assert "pi" in str(kw.get("purpose", "")).lower()
    # Identical contract shape + values mapped from the pi result.
    assert set(out) == {"payload", "tokens_used", "wall_clock_s",
                        "status", "error"}
    assert out["status"] == "complete"
    assert out["tokens_used"] == 42
    assert out["wall_clock_s"] == pytest.approx(1.5)
    assert out["payload"]["schema"] == "architecture_analyzer_v1"


def test_pi_backend_partial_on_not_ok(monkeypatch):
    """Parseable payload but ok=False (e.g. max-turns) -> partial, like the
    claude exit!=0 path."""
    monkeypatch.setenv("PRISM_ANALYZER_BACKEND", "pi")
    monkeypatch.setattr(ar, "_pi_has_fs_tools", lambda: True)
    _stub_pi(monkeypatch, {}, {
        "ok": False, "text": _ARCH_JSON, "tokens": 5, "ms": 900.0,
    })
    out = ar.run_analyzer("prism", "architecture_analyzer", "deadbeef")
    assert out["status"] == "partial"


def test_pi_backend_failed_on_junk_payload(monkeypatch):
    monkeypatch.setenv("PRISM_ANALYZER_BACKEND", "pi")
    monkeypatch.setattr(ar, "_pi_has_fs_tools", lambda: True)
    _stub_pi(monkeypatch, {}, {"ok": True, "text": "sorry, no idea",
                               "tokens": 3, "ms": 100.0})
    out = ar.run_analyzer("prism", "architecture_analyzer", "deadbeef")
    assert out["status"] == "failed"
    assert out["error"]


# ---------------------------------------------------------------------------
# AC-4: skip-guard — pi requested but fs tools absent -> fall back to claude
# ---------------------------------------------------------------------------

def test_pi_backend_without_fs_tools_falls_back_to_claude(monkeypatch):
    monkeypatch.setenv("PRISM_ANALYZER_BACKEND", "pi")
    # Do NOT patch _pi_has_fs_tools: the real pi catalog has no fs tools.
    assert ar._pi_has_fs_tools() is False, (
        "the real pi runtime advertises no Read/Glob/Grep tools yet — "
        "the skip-guard's premise"
    )
    claude: dict = {}
    _stub_claude(monkeypatch, claude)

    def _boom(*a, **k):
        raise AssertionError("pi must not run when fs tools are absent")
    monkeypatch.setattr("prism_service.inference.pi_agent.invoke", _boom)

    out = ar.run_analyzer("prism", "architecture_analyzer", "deadbeef")
    assert claude.get("called") is True, "must fall back to claude_cli"
    assert out["status"] == "complete"


# ---------------------------------------------------------------------------
# AC-5: the pi path routes through pi_agent.invoke (the pi_run_log recorder)
# ---------------------------------------------------------------------------

def test_pi_path_uses_pi_agent_recording_point(monkeypatch):
    """No separate claude ledger write on the pi path — routing exclusively
    through pi_agent.invoke (which records the backend='pi' pi_run_log row)."""
    monkeypatch.setenv("PRISM_ANALYZER_BACKEND", "pi")
    monkeypatch.setattr(ar, "_pi_has_fs_tools", lambda: True)

    calls = {"pi": 0, "claude": 0}

    def _pi(prompt, **kw):
        calls["pi"] += 1
        return {"ok": True, "text": _ARCH_JSON, "tokens": 1, "ms": 1.0}

    def _claude(*a, **k):
        calls["claude"] += 1
        return _FakeCliResult()

    monkeypatch.setattr("prism_service.inference.pi_agent.invoke", _pi)
    monkeypatch.setattr("prism_service.inference.claude_cli.invoke", _claude)

    ar.run_analyzer("prism", "architecture_analyzer", "deadbeef")
    assert calls == {"pi": 1, "claude": 0}
