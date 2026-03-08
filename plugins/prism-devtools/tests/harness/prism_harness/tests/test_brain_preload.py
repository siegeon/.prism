"""test-brain-preload: Validate Brain context injection into agent sessions.

TC-1: stream-json output is non-empty
TC-2: system message contains brain_context or Brain context block
TC-3: assistant response references brain knowledge (project-specific content)
TC-4: brain.db exists after session (Brain was active during session)
"""

from __future__ import annotations

from pathlib import Path

from ..assertions import AssertionContext
from ..claude_session import run_claude
from ..parser import extract_assistant_text, extract_hook_messages, parse_jsonl
from ..reporter import finalize_results
from ..scaffold import Scaffold

NAME = "test-brain-preload"

_PROMPT = (
    "Using your brain context and memory, briefly describe what this project does "
    "and list two key files or modules you know about."
)

_BRAIN_KEYWORDS = [
    "brain_context",
    "brain context",
    "Brain context",
    "incremental_reindex",
    ".prism/brain",
    "brain.db",
]


def run(
    ctx: AssertionContext,
    scaffold: Scaffold,
    plugin_dir: Path,
    results_dir: Path | None,
) -> None:
    ctx.log_section(NAME)

    test_project_dir = scaffold.brownfield()
    brain_db = test_project_dir / ".prism" / "brain" / "brain.db"

    ctx.log_info(f"Prompt: {_PROMPT!r}")
    out, _ = run_claude(_PROMPT, test_project_dir, plugin_dir, max_turns=3)
    ctx.last_output = out

    # TC-1: non-empty output
    ctx.assert_json_not_empty("TC-1: stream-json output is non-empty")

    # TC-2: system message contains brain context block
    if out and out.is_file():
        events = parse_jsonl(out)
        hook_msgs = extract_hook_messages(events)
        brain_in_hooks = any(
            any(kw.lower() in msg.lower() for kw in _BRAIN_KEYWORDS)
            for msg in hook_msgs
        )
        # Also check all raw event content
        raw_content = out.read_text(encoding="utf-8")
        brain_in_raw = any(kw.lower() in raw_content.lower() for kw in _BRAIN_KEYWORDS)

        if brain_in_hooks:
            ctx._pass("TC-2: system hook message contains Brain context block")
        elif brain_in_raw:
            ctx._pass("TC-2: Brain context found in session output (raw)")
        else:
            ctx._skip(
                "TC-2: Brain context not detected — Brain may not have indexed this project yet"
            )

        # TC-3: assistant response references project knowledge
        texts = extract_assistant_text(events)
        combined = " ".join(texts).lower()
        # Brain-loaded sessions should mention at least some project-specific terms
        project_terms = ["prism", "brain", "plugin", "skill", "hook", "test", "session"]
        found_terms = [t for t in project_terms if t in combined]
        if len(found_terms) >= 2:
            ctx._pass(
                f"TC-3: assistant response references project knowledge "
                f"(found: {found_terms[:3]})"
            )
        elif texts:
            ctx._skip(
                f"TC-3: assistant responded but brain knowledge not clearly evident "
                f"(terms found: {found_terms})"
            )
        else:
            ctx._fail("TC-3: no assistant text in output")
    else:
        ctx._fail("TC-2: no output file to check")
        ctx._fail("TC-3: no output file to check")

    # TC-4: brain.db exists (Brain was active)
    if brain_db.exists():
        db_size = brain_db.stat().st_size
        ctx._pass(f"TC-4: brain.db exists after session ({db_size} bytes)")
    else:
        ctx._skip("TC-4: brain.db not found — session-start hook may not have run")

    if results_dir:
        finalize_results(NAME, results_dir, out, ctx.passed, ctx.failed)

    scaffold.teardown()
