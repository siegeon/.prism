"""Unit tests for prism_service.inference.claude_cli.

Mocks subprocess.run so no real `claude` CLI is invoked. Asserts:
  * INV-1: env strip removes ANTHROPIC_API_KEY / CLAUDECODE /
           CLAUDE_CODE_ENTRYPOINT before invoking the child.
  * ClaudeNotLoggedInError raised when stderr signals auth failure.
  * ClaudeCliResult shape on success.
  * Legacy run_claude(...) tuple-return wrapper.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from prism_service.inference import claude_cli


def _completed(exit_code: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=exit_code, stdout=None, stderr=stderr,
    )


def test_strip_env_removes_inv1_vars(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("OTHER", "keep-me")

    env = claude_cli._strip_env()

    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env.get("OTHER") == "keep-me"


def test_invoke_strips_env_before_subprocess(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")

    captured = {}

    def fake_run(cmd, cwd, env, stdout, stderr):
        captured["env"] = env
        captured["cmd"] = cmd
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        res = claude_cli.invoke(
            "hello", tmp_path, tmp_path, max_turns=1, parse_events=False,
        )

    assert "ANTHROPIC_API_KEY" not in captured["env"]
    assert res.exit_code == 0
    assert res.output_path.exists()


def test_invoke_raises_not_logged_in_on_auth_failure(tmp_path):
    auth_stderr = b"Error: you are not logged in. Run `claude login` first."

    def fake_run(cmd, cwd, env, stdout, **kwargs):
        return _completed(exit_code=1, stderr=auth_stderr)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        with pytest.raises(claude_cli.ClaudeNotLoggedInError) as exc:
            claude_cli.invoke("hi", tmp_path, tmp_path, max_turns=1)

    assert "claude login" in str(exc.value)


def test_invoke_does_not_raise_on_success_with_empty_stderr(tmp_path):
    def fake_run(cmd, cwd, env, stdout, **kwargs):
        return _completed(exit_code=0, stderr=b"")

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        res = claude_cli.invoke(
            "hi", tmp_path, tmp_path, max_turns=1, parse_events=False,
        )

    assert res.exit_code == 0
    assert isinstance(res, claude_cli.ClaudeCliResult)


def test_run_claude_returns_tuple(tmp_path):
    def fake_run(cmd, cwd, env, stdout, **kwargs):
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        out, code = claude_cli.run_claude(
            "hi", tmp_path, tmp_path, max_turns=1,
        )

    assert isinstance(out, Path)
    assert code == 0


def test_invoke_parses_jsonl_events(tmp_path):
    """parse_events=True takes usage from the result event ONCE, not by summing
    per-assistant-event snapshots (task 45e04fad). The two assistant events sum
    to 12/8; the authoritative result event reports different totals + all four
    fields + cost + model — usage must reflect the result event, not the sum."""

    def fake_run(cmd, cwd, env, stdout, **kwargs):
        stdout.write(
            '{"type":"assistant","message":{"usage":{"input_tokens":10,"output_tokens":5}}}\n'
        )
        stdout.write(
            '{"type":"assistant","message":{"usage":{"input_tokens":2,"output_tokens":3}}}\n'
        )
        stdout.write(
            '{"type":"result","subtype":"success",'
            '"usage":{"input_tokens":50,"output_tokens":9,'
            '"cache_read_input_tokens":100,"cache_creation_input_tokens":20},'
            '"total_cost_usd":0.0123,'
            '"modelUsage":{"claude-haiku-4-5":{"costUSD":0.0123}}}\n'
        )
        stdout.flush()
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        res = claude_cli.invoke(
            "hi", tmp_path, tmp_path, max_turns=1, parse_events=True,
        )

    assert len(res.parsed_events) == 3
    # 50/9 (the result event) — NOT 12/8 (the cross-event sum).
    assert res.usage["input_tokens"] == 50
    assert res.usage["output_tokens"] == 9
    assert res.usage["cache_read_input_tokens"] == 100
    assert res.usage["cache_creation_input_tokens"] == 20
    assert round(res.usage["cost_usd"], 4) == 0.0123
    assert res.usage["model"] == "claude-haiku-4-5"


def test_build_cmd_includes_required_flags():
    cmd = claude_cli._build_cmd(
        "hello", "/plugin/dir", model="", max_budget_usd=0.0, max_turns=3,
    )
    # v5.1.7: scoped permissions instead of --dangerously-skip-permissions.
    # Analyzers only need read-only access to the source tree.
    assert "--dangerously-skip-permissions" not in cmd
    assert "--allowedTools" in cmd
    allowed_idx = cmd.index("--allowedTools")
    allowed = cmd[allowed_idx + 1 : allowed_idx + 4]
    assert allowed == ["Read", "Glob", "Grep"]
    assert "--no-session-persistence" in cmd
    assert "--output-format" in cmd
    assert "stream-json" in cmd
    assert "--max-turns" in cmd and "3" in cmd
    assert "--plugin-dir" in cmd and "/plugin/dir" in cmd


def test_build_cmd_adds_json_schema_flag_as_json_string():
    cmd = claude_cli._build_cmd(
        "draft a story", "/plugin/dir", model="haiku", max_budget_usd=0.5,
        max_turns=4, json_schema={"type": "object", "properties": {"x": {"type": "string"}}},
    )
    assert "--json-schema" in cmd
    flag_value = cmd[cmd.index("--json-schema") + 1]
    assert json.loads(flag_value) == {"type": "object", "properties": {"x": {"type": "string"}}}


def test_build_cmd_omits_json_schema_flag_when_not_given():
    cmd = claude_cli._build_cmd(
        "hi", "/plugin/dir", model="", max_budget_usd=0.0, max_turns=1,
    )
    assert "--json-schema" not in cmd


def test_invoke_populates_structured_output_from_the_result_event(tmp_path):
    """Verified live (2026-08-20): --json-schema + --output-format json
    returns an already-parsed "structured_output" dict on the type=="result"
    event -- this proves invoke() surfaces it without extra json.loads()
    on the caller's part."""

    def fake_run(cmd, cwd, env, stdout, **kwargs):
        stdout.write(
            '{"type":"result","subtype":"success","is_error":false,'
            '"result":"{\\"story_md\\":\\"## Summary\\\\nx\\"}",'
            '"structured_output":{"story_md":"## Summary\\nx"},'
            '"total_cost_usd":0.05}\n'
        )
        stdout.flush()
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        res = claude_cli.invoke(
            "draft a story", tmp_path, tmp_path, max_turns=4, parse_events=True,
            json_schema={"type": "object", "properties": {"story_md": {"type": "string"}}},
        )

    assert res.structured_output == {"story_md": "## Summary\nx"}


def test_invoke_passes_timeout_s_through_to_subprocess_run(tmp_path):
    """AC-1 (epic 3baadd19, task_runner drive worker hardening): invoke()
    must accept timeout_s and forward it to subprocess.run's own timeout=
    kwarg, so a wedged `claude -p` child cannot hang the drive worker
    forever (claude_cli.py:278-281 currently passes no timeout= at all)."""
    captured = {}

    def fake_run(cmd, cwd, env, stdout, stderr, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        claude_cli.invoke(
            "hi", tmp_path, tmp_path, max_turns=1, parse_events=False,
            timeout_s=5.0,
        )

    assert captured["timeout"] == 5.0


def test_invoke_returns_timeout_marked_result_instead_of_raising(tmp_path):
    """AC-1: a TimeoutExpired from subprocess.run must be caught inside
    invoke() and turned into a ClaudeCliResult with a non-zero, timeout-
    marked exit code -- never propagated as a raw exception, so
    task_runner._run_one_step's existing try/except Exception isn't the
    only backstop against a wedged child."""

    def fake_run(cmd, cwd, env, stdout, stderr, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout") or 0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        res = claude_cli.invoke(
            "hi", tmp_path, tmp_path, max_turns=1, parse_events=False,
            timeout_s=0.01,
        )

    assert isinstance(res, claude_cli.ClaudeCliResult)
    assert res.exit_code == claude_cli.TIMEOUT_EXIT_CODE
    assert res.exit_code != 0


def test_build_cmd_appends_session_id_flag_when_given():
    """AC-1 (task 12403c60): _build_cmd must append --session-id <value>
    to the built argv when session_id is passed non-empty, so a runner-
    driven claude -p child can be attributed its own identity on /live."""
    cmd = claude_cli._build_cmd(
        "hello", "/plugin/dir", model="", max_budget_usd=0.0, max_turns=3,
        session_id="x",
    )
    assert "--session-id" in cmd
    idx = cmd.index("--session-id")
    assert cmd[idx + 1] == "x"


def test_build_cmd_omits_session_id_flag_when_not_given():
    """AC-1 negative case: an empty/default session_id must never emit
    the flag at all (never a bare --session-id or an empty-string value)."""
    cmd = claude_cli._build_cmd(
        "hello", "/plugin/dir", model="", max_budget_usd=0.0, max_turns=3,
    )
    assert "--session-id" not in cmd

    cmd_empty = claude_cli._build_cmd(
        "hello", "/plugin/dir", model="", max_budget_usd=0.0, max_turns=3,
        session_id="",
    )
    assert "--session-id" not in cmd_empty


def test_invoke_passes_session_id_through_to_build_cmd(tmp_path):
    """AC-2 (task 12403c60): invoke() must accept session_id and thread it
    through to _build_cmd unchanged -- no default substitution, no reuse
    of SEAT_ID or any other constant -- so the flag reaches the real
    subprocess argv, not just the Python call."""
    captured = {}

    def fake_run(cmd, cwd, env, stdout, stderr, **kwargs):
        captured["cmd"] = cmd
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        claude_cli.invoke(
            "hi", tmp_path, tmp_path, max_turns=1, parse_events=False,
            session_id="x",
        )

    assert "--session-id" in captured["cmd"]
    idx = captured["cmd"].index("--session-id")
    assert captured["cmd"][idx + 1] == "x"


def test_invoke_leaves_structured_output_none_without_a_schema(tmp_path):
    """No json_schema passed -> never even LOOK for the field, even if a
    result event happens to carry one (a plain call shouldn't silently
    start returning structured data a caller didn't ask for)."""

    def fake_run(cmd, cwd, env, stdout, **kwargs):
        stdout.write('{"type":"result","subtype":"success","structured_output":{"x":1}}\n')
        stdout.flush()
        return _completed(exit_code=0)

    with patch("prism_service.inference.claude_cli.subprocess.run", side_effect=fake_run):
        res = claude_cli.invoke("hi", tmp_path, tmp_path, max_turns=1, parse_events=True)

    assert res.structured_output is None
