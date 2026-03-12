#!/usr/bin/env python3
"""
Tests for get_usage_from_transcript() skill tracking in prism_stop_hook.py.

Acceptance criteria:
- AC-6: get_usage_from_transcript() correctly counts Skill tool_use blocks (skill_calls > 0)
- AC-7: get_usage_from_transcript() returns skill_calls=0 when no Skill blocks exist
- AC-8: step_history entry structure contains both 's' (skill_calls) and 'bq' (brain_queries) fields
"""
import json
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

from prism_stop_hook import get_usage_from_transcript  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_transcript(tmp_path: Path, lines: list) -> Path:
    """Write a list of dicts as JSONL to a temp transcript file."""
    p = tmp_path / "transcript.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for entry in lines:
            f.write(json.dumps(entry) + "\n")
    return p


def _assistant_with_skill(skill_name: str = "calculator") -> dict:
    """Assistant message containing a Skill tool_use block (uses 'skill' key per Skill tool spec)."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll invoke the skill now."},
                {"type": "tool_use", "id": "tu_001", "name": "Skill",
                 "input": {"skill": skill_name, "args": ""}},
            ],
        },
    }


def _assistant_with_other_tool(tool_name: str = "Bash") -> dict:
    """Assistant message containing a non-Skill tool_use block."""
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_002", "name": tool_name,
                 "input": {"command": "ls"}},
            ],
        },
    }


def _result_line() -> dict:
    """Minimal result line."""
    return {"type": "result", "subtype": "success", "result": "done"}


# ---------------------------------------------------------------------------
# AC-6: get_usage_from_transcript counts Skill tool_use blocks
# ---------------------------------------------------------------------------

def test_ac6_skill_calls_counted_when_skill_tool_used(tmp_path):
    """get_usage_from_transcript() counts Skill name='Skill' blocks as skill_calls."""
    lines = [_assistant_with_skill("calculator"), _result_line()]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_calls"] > 0, (
        f"Expected skill_calls > 0 when Skill tool_use block present; got {result}"
    )
    assert result["skill_calls"] == 1, (
        f"Expected skill_calls=1 for one Skill block; got {result['skill_calls']}"
    )


def test_ac6_multiple_skill_calls_counted(tmp_path):
    """get_usage_from_transcript() counts all Skill tool_use blocks across transcript."""
    lines = [
        _assistant_with_skill("calculator"),
        _assistant_with_skill("remember"),
        _assistant_with_skill("test-runner"),
        _result_line(),
    ]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_calls"] == 3, (
        f"Expected skill_calls=3 for three Skill blocks; got {result['skill_calls']}"
    )


def test_ac6_skill_calls_counted_separate_from_other_tool_calls(tmp_path):
    """Skill tool_use blocks counted separately from non-Skill tool calls."""
    lines = [
        _assistant_with_skill("calculator"),
        _assistant_with_other_tool("Bash"),
        _assistant_with_other_tool("Read"),
        _result_line(),
    ]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_calls"] == 1, (
        f"Expected skill_calls=1 (only Skill blocks); got {result['skill_calls']}"
    )
    assert result["tool_calls"] == 3, (
        f"Expected tool_calls=3 (all tool_use blocks); got {result['tool_calls']}"
    )


# ---------------------------------------------------------------------------
# AC-7: get_usage_from_transcript returns skill_calls=0 when no Skill blocks
# ---------------------------------------------------------------------------

def test_ac7_skill_calls_zero_when_no_skill_blocks(tmp_path):
    """get_usage_from_transcript() returns skill_calls=0 when no Skill tool_use blocks."""
    lines = [
        _assistant_with_other_tool("Bash"),
        _assistant_with_other_tool("Read"),
        _result_line(),
    ]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_calls"] == 0, (
        f"Expected skill_calls=0 when no Skill blocks; got {result['skill_calls']}"
    )


def test_ac7_skill_calls_zero_on_empty_transcript(tmp_path):
    """get_usage_from_transcript() returns skill_calls=0 for empty transcript."""
    transcript = _write_transcript(tmp_path, [])

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_calls"] == 0
    assert result["tool_calls"] == 0


def test_ac7_skill_calls_zero_on_text_only_messages(tmp_path):
    """get_usage_from_transcript() returns skill_calls=0 for text-only assistant messages."""
    lines = [
        {"type": "assistant", "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "Here is my answer."}],
        }},
        _result_line(),
    ]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_calls"] == 0


def test_ac7_skill_calls_zero_when_transcript_missing():
    """get_usage_from_transcript() returns skill_calls=0 for nonexistent path."""
    result = get_usage_from_transcript("/tmp/nonexistent-prism-transcript.jsonl")

    assert result["skill_calls"] == 0
    assert result["total_tokens"] == 0


def test_ac7_skill_calls_zero_when_path_empty_string():
    """get_usage_from_transcript('') returns zeroed dict."""
    result = get_usage_from_transcript("")

    assert result["skill_calls"] == 0
    assert result["total_tokens"] == 0


# ---------------------------------------------------------------------------
# AC-8: step_history entry contains 's' (skill_calls) and 'bq' (brain_queries)
# ---------------------------------------------------------------------------

def test_ac8_step_history_entry_has_skill_calls_key(tmp_path):
    """step_history entry built from get_usage_from_transcript output has 's' key."""
    lines = [_assistant_with_skill("calculator"), _result_line()]
    transcript = _write_transcript(tmp_path, lines)
    usage = get_usage_from_transcript(str(transcript))

    # Simulate what validate_step does at lines 2056-2063 of prism_stop_hook.py
    entry = {
        "i": 0,
        "d": 1.5,
        "t": usage.get("total_tokens", 0),
        "s": usage.get("skill_calls", 0),
        "tc": usage.get("tool_calls", 0),
        "bq": 0,
    }

    assert "s" in entry, "step_history entry must have 's' key for skill_calls"
    assert entry["s"] == 1, f"Expected s=1 skill_call; got {entry['s']}"


def test_ac8_step_history_entry_has_brain_queries_key(tmp_path):
    """step_history entry structure has 'bq' key for brain_queries."""
    lines = [_result_line()]
    transcript = _write_transcript(tmp_path, lines)
    usage = get_usage_from_transcript(str(transcript))

    brain_queries = 2  # Simulates conductor.last_had_brain_context = 2
    entry = {
        "i": 1,
        "d": 3.0,
        "t": usage.get("total_tokens", 0),
        "s": usage.get("skill_calls", 0),
        "tc": usage.get("tool_calls", 0),
        "bq": brain_queries,
    }

    assert "bq" in entry, "step_history entry must have 'bq' key for brain_queries"
    assert entry["bq"] == 2, f"Expected bq=2; got {entry['bq']}"


def test_ac8_step_history_entry_has_both_s_and_bq_keys(tmp_path):
    """step_history entry schema always contains both 's' and 'bq' regardless of values."""
    lines = [_assistant_with_skill("verify"), _result_line()]
    transcript = _write_transcript(tmp_path, lines)
    usage = get_usage_from_transcript(str(transcript))

    for brain_queries in (0, 1, 5):
        entry = {
            "i": 0,
            "d": 1.0,
            "t": usage.get("total_tokens", 0),
            "s": usage.get("skill_calls", 0),
            "tc": usage.get("tool_calls", 0),
            "bq": brain_queries,
        }
        assert "s" in entry, f"'s' key missing from step_history entry (bq={brain_queries})"
        assert "bq" in entry, f"'bq' key missing from step_history entry (bq={brain_queries})"


def test_ac8_step_history_json_roundtrip_preserves_tracking_keys(tmp_path):
    """step_history JSON roundtrip preserves 's' and 'bq' fields."""
    lines = [_assistant_with_skill("build"), _result_line()]
    transcript = _write_transcript(tmp_path, lines)
    usage = get_usage_from_transcript(str(transcript))

    entry = {
        "i": 2,
        "d": 5.1,
        "t": usage["total_tokens"],
        "s": usage["skill_calls"],
        "tc": usage["tool_calls"],
        "bq": 3,
    }
    history = [entry]

    # Roundtrip through JSON (as step_history is stored in YAML state file)
    restored = json.loads(json.dumps(history))
    restored_entry = restored[0]

    assert restored_entry["s"] == entry["s"]
    assert restored_entry["bq"] == entry["bq"]


# ---------------------------------------------------------------------------
# Step line start filtering
# ---------------------------------------------------------------------------

def test_step_line_start_excludes_prior_lines(tmp_path):
    """Skill calls before step_line_start are not counted."""
    # 3 lines before step, 1 Skill call after
    pre_step = [
        _assistant_with_other_tool("Bash"),
        _assistant_with_skill("old-skill"),
        _assistant_with_other_tool("Read"),
    ]
    post_step = [_assistant_with_skill("new-skill"), _result_line()]
    transcript = _write_transcript(tmp_path, pre_step + post_step)

    result = get_usage_from_transcript(str(transcript), step_line_start=3)

    # Only lines after step_line_start=3 are counted (lines 4 and 5)
    assert result["skill_calls"] == 1, (
        f"Expected 1 skill_call (after step boundary); got {result['skill_calls']}"
    )


# ---------------------------------------------------------------------------
# AC-1: get_usage_from_transcript returns skill_names list
# ---------------------------------------------------------------------------

def test_ac1_skill_names_returned_for_single_skill(tmp_path):
    """get_usage_from_transcript() returns skill_names list with invoked skill name."""
    lines = [_assistant_with_skill("commit"), _result_line()]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert "skill_names" in result, "Return dict must contain 'skill_names' key"
    assert result["skill_names"] == ["commit"], (
        f"Expected skill_names=['commit']; got {result['skill_names']}"
    )


def test_ac1_skill_names_multiple_skills(tmp_path):
    """get_usage_from_transcript() captures all skill names across transcript."""
    lines = [
        _assistant_with_skill("commit"),
        _assistant_with_skill("remember"),
        _assistant_with_skill("prism-bug"),
        _result_line(),
    ]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_names"] == ["commit", "remember", "prism-bug"], (
        f"Expected all 3 skill names in order; got {result['skill_names']}"
    )


def test_ac1_skill_names_empty_when_no_skill_blocks(tmp_path):
    """get_usage_from_transcript() returns empty skill_names when no Skill blocks present."""
    lines = [_assistant_with_other_tool("Bash"), _result_line()]
    transcript = _write_transcript(tmp_path, lines)

    result = get_usage_from_transcript(str(transcript))

    assert result["skill_names"] == [], (
        f"Expected empty skill_names list; got {result['skill_names']}"
    )


def test_ac1_skill_names_empty_on_missing_transcript():
    """get_usage_from_transcript() returns empty skill_names for nonexistent path."""
    result = get_usage_from_transcript("/tmp/nonexistent-prism-transcript-ac1.jsonl")

    assert "skill_names" in result
    assert result["skill_names"] == []


def test_ac1_skill_names_respects_step_line_start(tmp_path):
    """skill_names only captures skills after step_line_start boundary."""
    pre_step = [
        _assistant_with_skill("old-skill"),
        _assistant_with_other_tool("Read"),
    ]
    post_step = [_assistant_with_skill("new-skill"), _result_line()]
    transcript = _write_transcript(tmp_path, pre_step + post_step)

    result = get_usage_from_transcript(str(transcript), step_line_start=2)

    assert result["skill_names"] == ["new-skill"], (
        f"Expected only post-step skill; got {result['skill_names']}"
    )


def test_ac1_skill_names_ignores_block_without_skill_key(tmp_path):
    """Skill tool_use block missing 'skill' key does not contribute to skill_names."""
    # Block with no 'skill' key in input
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_x", "name": "Skill",
                 "input": {"args": "some args"}},  # no 'skill' key
            ],
        },
    }
    transcript = _write_transcript(tmp_path, [entry, _result_line()])

    result = get_usage_from_transcript(str(transcript))

    # skill_calls still incremented (block name is Skill), but no name extracted
    assert result["skill_calls"] == 1
    assert result["skill_names"] == [], (
        f"Block missing 'skill' key should not appear in skill_names; got {result['skill_names']}"
    )


# ---------------------------------------------------------------------------
# AC-2: Conductor.record_outcome records skill names via brain.record_skill_usage
# ---------------------------------------------------------------------------

def _make_conductor_with_mock_brain():
    """Create a Conductor with a MagicMock brain (follows existing test pattern)."""
    from unittest.mock import MagicMock
    from conductor_engine import Conductor

    conductor = Conductor.__new__(Conductor)
    mock_brain = MagicMock()
    mock_brain.last_result_count = 0
    conductor._brain = mock_brain
    conductor._brain_available = True
    conductor.last_had_brain_context = 0
    conductor.last_prompt_id = ""
    return conductor, mock_brain


def test_ac2_conductor_records_skill_names_to_brain():
    """Conductor.record_outcome() calls brain.record_skill_usage for each skill name."""
    conductor, mock_brain = _make_conductor_with_mock_brain()

    conductor.record_outcome(
        prompt_id="dev/implement_tasks",
        persona="dev",
        step_id="implement_tasks",
        metrics={
            "tokens_used": 1000,
            "duration_s": 30,
            "gate_passed": 1,
            "skill_calls": 2,
            "tool_calls": 10,
            "skill_names": ["commit", "remember"],
            "session_id": "test-session-123",
        },
    )

    calls = mock_brain.record_skill_usage.call_args_list
    assert mock_brain.record_skill_usage.call_count == 2, (
        f"Expected 2 record_skill_usage calls; got {mock_brain.record_skill_usage.call_count}. Calls: {calls}"
    )
    assert calls[0][0] == ("test-session-123", "commit", "implement_tasks"), (
        f"First call args wrong: {calls[0]}"
    )
    assert calls[1][0] == ("test-session-123", "remember", "implement_tasks"), (
        f"Second call args wrong: {calls[1]}"
    )


def test_ac2_conductor_skips_empty_skill_names():
    """Conductor.record_outcome() skips empty string skill names."""
    conductor, mock_brain = _make_conductor_with_mock_brain()

    conductor.record_outcome(
        prompt_id="dev/implement_tasks",
        persona="dev",
        step_id="implement_tasks",
        metrics={
            "tokens_used": 500,
            "duration_s": 15,
            "gate_passed": 1,
            "skill_calls": 1,
            "tool_calls": 5,
            "skill_names": ["", "valid-skill", ""],
            "session_id": "sess-abc",
        },
    )

    assert mock_brain.record_skill_usage.call_count == 1, (
        f"Expected 1 call (empty strings skipped); got {mock_brain.record_skill_usage.call_count}"
    )
    assert mock_brain.record_skill_usage.call_args[0] == ("sess-abc", "valid-skill", "implement_tasks")


def test_ac2_conductor_no_skill_recording_when_brain_unavailable():
    """Conductor.record_outcome() does not fail when Brain is unavailable."""
    from conductor_engine import Conductor
    conductor = Conductor.__new__(Conductor)
    conductor._brain = None
    conductor._brain_available = False
    conductor.last_prompt_id = ""

    # Should not raise
    conductor.record_outcome(
        prompt_id="dev/implement_tasks",
        persona="dev",
        step_id="implement_tasks",
        metrics={
            "tokens_used": 100,
            "duration_s": 5,
            "gate_passed": 1,
            "skill_calls": 1,
            "skill_names": ["commit"],
            "session_id": "sess-xyz",
        },
    )


def test_ac2_conductor_no_skill_recording_when_skill_names_absent():
    """Conductor.record_outcome() handles missing skill_names key gracefully."""
    conductor, mock_brain = _make_conductor_with_mock_brain()

    conductor.record_outcome(
        prompt_id="dev/implement_tasks",
        persona="dev",
        step_id="implement_tasks",
        metrics={
            "tokens_used": 200,
            "duration_s": 10,
            "gate_passed": 1,
            "skill_calls": 0,
            "tool_calls": 3,
            # no skill_names key
        },
    )

    assert mock_brain.record_skill_usage.call_count == 0, (
        "record_skill_usage should not be called when skill_names absent from metrics"
    )
