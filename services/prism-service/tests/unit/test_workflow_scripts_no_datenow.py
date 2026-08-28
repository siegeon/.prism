"""Lint: workflow scripts must not use a client clock (Date.now / new Date()).

PRISM is the time authority — run timestamps are server-stamped on ingest
(see .claude/workflows/implement.js: "Timing is stamped server-side ... workflow
scripts forbid client clocks"). Client clocks are also unavailable in the
workflow sandbox and would break resume/caching. See PRISM memory mx-9945f2
(workflow-scripts-forbid-date-now).

This lint scans .claude/workflows/*.js and fails on any real usage; comment
lines (// or *) are allowed so the ban can be documented.
"""
import re
from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parents[4] / ".claude" / "workflows"
FORBIDDEN = re.compile(r"Date\.now|new\s+Date\s*\(")


def _is_comment(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("//") or s.startswith("*") or s.startswith("/*")


def test_no_client_clocks_in_workflow_scripts():
    if not WORKFLOWS_DIR.is_dir():
        return  # no workflows dir in this checkout — nothing to lint
    scripts = sorted(WORKFLOWS_DIR.glob("*.js"))
    assert scripts, f"expected workflow scripts under {WORKFLOWS_DIR}"
    offenders = []
    for js in scripts:
        for i, line in enumerate(js.read_text(encoding="utf-8").splitlines(), 1):
            if _is_comment(line):
                continue
            if FORBIDDEN.search(line):
                offenders.append(f"{js.name}:{i}: {line.strip()}")
    assert not offenders, (
        "workflow scripts must not use a client clock (Date.now / new Date()); "
        "PRISM server-stamps run timestamps (mx-9945f2). Offenders:\n"
        + "\n".join(offenders)
    )


_IMPLEMENT_JS = WORKFLOWS_DIR / "implement.js"


def test_preflight_clock_check_exempts_comments_like_this_lint_does():
    """Task 3a3f90da (2026-08-26): this file's OWN lint has always exempted
    comments (see docstring/_is_comment above) - but the live pre-flight
    step (implement.js's own bash-driven CLOCK-CLEAN check) had no such
    exception, and a plainly-worded comment documenting the ban tripped
    its own grep, halting a real drive twice ("even if in comment" was
    the literal live failure text). The two checks must agree: a
    comment-only hit must not fail the drive."""
    src = _IMPLEMENT_JS.read_text(encoding="utf-8")
    clock_at = src.index("CLOCK-CLEAN:")
    daemon_at = src.index("DAEMON/CONDUCTOR REACHABLE:")
    clock_block = src[clock_at:daemon_at]
    assert "COMMENT" in clock_block, (
        "the pre-flight instruction must explicitly exempt comment lines"
    )
    assert "REAL CODE" in clock_block or "real code" in clock_block, (
        "the pre-flight instruction must say only a real-code hit fails "
        "the drive, not merely 'any match'"
    )
    assert "test_workflow_scripts_no_datenow.py" in clock_block, (
        "the pre-flight instruction should point at this pinned lint as "
        "the scope authority, so the two never drift apart again"
    )
