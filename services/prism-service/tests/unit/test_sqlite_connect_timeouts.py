"""Source-scan guard — no bare sqlite3.connect() without a timeout.

sqlite-hardening workstream: stress runs measured a 97.6% lock-error
rate at 8 concurrent writers with bare connects vs 0.0% once every
connection waits (timeout=5.0 / busy_timeout). This test pins the
sweep so a new bare connect can't regress it silently.

Scans every prism_service/**/*.py for sqlite3.connect(...) calls and
asserts each one passes a timeout= keyword. URI mode=ro read-only
sites (if any appear) are exempt, as is the allowlist below.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_PKG = _HERE.parent.parent.parent / "prism_service"

# Files allowed to keep bare connects, with the reason on record.
_ALLOWLIST = {
    # Owned by a parallel PR — remove once it lands with its own sweep.
    "services/claude_transcripts.py",
}

# sqlite3.connect( ...args... ) — args may span lines but never contain
# an unbalanced ')' in this codebase (one nested call like str(db) is
# allowed by the inner group).
_CONNECT_RE = re.compile(
    r"sqlite3\.connect\(([^()]*(?:\([^()]*\)[^()]*)*)\)", re.DOTALL)


def _bare_connect_sites() -> list[str]:
    """Return 'relpath:lineno: args' for every connect lacking a timeout."""
    offenders: list[str] = []
    for py in sorted(_PKG.rglob("*.py")):
        rel = py.relative_to(_PKG).as_posix()
        if rel in _ALLOWLIST:
            continue
        src = py.read_text(encoding="utf-8")
        for m in _CONNECT_RE.finditer(src):
            args = m.group(1)
            if "timeout" in args:
                continue
            if "mode=ro" in args:  # read-only URI sites are exempt
                continue
            lineno = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{rel}:{lineno}: connect({args.strip()})")
    return offenders


def test_package_exists_and_scans_files():
    assert _PKG.is_dir(), f"package dir not found: {_PKG}"
    assert any(_PKG.rglob("*.py")), "no python sources found to scan"


def test_no_bare_sqlite_connect_without_timeout():
    offenders = _bare_connect_sites()
    assert offenders == [], (
        "bare sqlite3.connect without timeout= — under concurrent writers "
        "these fail with 'database is locked' (measured 97.6% error rate "
        "at 8 writers; 0.0% with timeout=5.0). Add timeout=5.0 or, for a "
        "legitimate read-only URI site, mode=ro:\n  " + "\n  ".join(offenders)
    )


def test_allowlist_shrinks_when_parallel_pr_lands():
    """The allowlist must only carry files that still contain a bare
    connect — a stale entry means the parallel PR landed and the
    exemption should be deleted."""
    for rel in sorted(_ALLOWLIST):
        p = _PKG / rel
        assert p.exists(), f"allowlisted file vanished: {rel} — drop it"
        src = p.read_text(encoding="utf-8")
        still_bare = any(
            "timeout" not in m.group(1)
            for m in _CONNECT_RE.finditer(src)
        )
        assert still_bare, (
            f"{rel} no longer has bare connects — remove it from "
            "_ALLOWLIST so the guard covers it too"
        )
