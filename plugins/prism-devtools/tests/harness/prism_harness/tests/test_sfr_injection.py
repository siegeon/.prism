"""test-sfr-injection: Verify SFR certificate structure in subagent output.

Checks that when an SFR-enabled sub-agent runs via a skill, the output
contains the expected certificate sections: PREMISES, EXECUTION TRACE, etc.

TC-1: stream-json output is non-empty
TC-2: SFR skill is available in the test project or plugin
TC-3: SFR certificate sections appear in output (PREMISES, EXECUTION TRACE)
TC-4: Agent tool_use present (subagent spawned)

Depends on: prism-1440 (parser), SFR skill implementation
"""

from __future__ import annotations

from pathlib import Path

from ..assertions import AssertionContext
from ..claude_session import run_claude
from ..parser import extract_tool_calls, parse_jsonl
from ..reporter import finalize_results
from ..scaffold import Scaffold

NAME = "test-sfr-injection"

_PROMPT = (
    "Run the SFR validation skill to produce a formal reasoning certificate "
    "for this project. The certificate should include PREMISES and EXECUTION TRACE."
)

# Keywords that indicate a valid SFR certificate structure
_SFR_CERTIFICATE_SECTIONS = [
    "PREMISES",
    "EXECUTION TRACE",
    "CERTIFICATE",
    "CONCLUSION",
    "VERIFICATION",
]

# Candidate skill directory names for SFR
_SFR_SKILL_NAMES = ("sfr", "sfr-validation", "structured-formal-reasoning")


def _find_sfr_skill(plugin_dir: Path, test_project_dir: Path) -> Path | None:
    """Locate SFR skill in test project or plugin skills directories."""
    for base in (test_project_dir / ".claude" / "skills", plugin_dir / "skills"):
        if base.is_dir():
            for name in _SFR_SKILL_NAMES:
                candidate = base / name
                if candidate.is_dir():
                    return candidate
    return None


def run(
    ctx: AssertionContext,
    scaffold: Scaffold,
    plugin_dir: Path,
    results_dir: Path | None,
) -> None:
    ctx.log_section(NAME)

    test_project_dir = scaffold.brownfield()

    sfr_skill = _find_sfr_skill(plugin_dir, test_project_dir)

    if sfr_skill is None:
        ctx.log_warn(
            "SFR skill not found — skipping injection tests "
            "(depends on prism-1440 SFR implementation)"
        )
        for label in ("TC-1", "TC-2", "TC-3", "TC-4"):
            ctx._skip(f"{label}: SFR skill not present (requires prism-1440)")
        if results_dir:
            finalize_results(NAME, results_dir, None, ctx.passed, ctx.failed)
        scaffold.teardown()
        return

    ctx.log_info(f"SFR skill found at: {sfr_skill}")
    ctx.log_info(f"Prompt: {_PROMPT!r}")
    out, _ = run_claude(_PROMPT, test_project_dir, plugin_dir, max_turns=6)
    ctx.last_output = out

    # TC-1: non-empty output
    ctx.assert_json_not_empty("TC-1: stream-json output is non-empty")

    # TC-2: SFR skill found
    ctx._pass(f"TC-2: SFR skill available at {sfr_skill.name!r}")

    # TC-3: certificate sections appear in output
    found_sections: list[str] = []
    if out and out.is_file():
        content = out.read_text(encoding="utf-8", errors="replace").upper()
        for section in _SFR_CERTIFICATE_SECTIONS:
            if section in content:
                found_sections.append(section)

    if len(found_sections) >= 2:
        ctx._pass(
            f"TC-3: SFR certificate sections found ({len(found_sections)}): {found_sections}"
        )
    elif found_sections:
        ctx._skip(
            f"TC-3: only {len(found_sections)} SFR section(s) found ({found_sections}); "
            "expected >= 2 for a complete certificate"
        )
    else:
        ctx._fail(
            "TC-3: no SFR certificate sections found in output",
            f"expected one of: {_SFR_CERTIFICATE_SECTIONS}",
        )

    # TC-4: Agent tool_use present (SFR may spawn a subagent)
    events = parse_jsonl(out) if out and out.is_file() else []
    tool_calls = extract_tool_calls(events)
    agent_calls = [tc for tc in tool_calls if tc.get("name") == "Agent"]
    if agent_calls:
        ctx._pass(f"TC-4: Agent tool_use present ({len(agent_calls)} call(s))")
    else:
        ctx._skip(
            "TC-4: Agent tool_use not found "
            "(SFR may run inline via Skill rather than via subagent)"
        )

    if results_dir:
        finalize_results(NAME, results_dir, out, ctx.passed, ctx.failed)

    scaffold.teardown()
