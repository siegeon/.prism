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
