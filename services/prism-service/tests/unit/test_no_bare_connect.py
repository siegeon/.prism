"""RED grep-gate: no bare sqlite3.connect outside the chokepoint (AC-3).

trace: sqlite-hardening / grep-gate. Walks the prism_service package
source and asserts ZERO `sqlite3.connect(` occurrences outside the
allowlist. This is the tooth that kills the likely_misfire — "helper
added but call sites left unrouted": it FAILS while the ~80 scattered
bare connects remain, and only passes once every site routes through
services/sqlite_db.connect().
"""
from __future__ import annotations

import re
from pathlib import Path

import prism_service

# Where a bare sqlite3.connect() is allowed to live, with the reason on record:
#  - services/sqlite_db.py — the central helper itself (the ONE funnel).
#  - services/verifier_service.py + services/conductor_service.py — the gate's
#    OWN judge/policy machinery. The control-plane guard (commit bf86b76)
#    REFUSES a green_gate whose candidate worktree edits its own judge, so this
#    task must NOT reroute them. Their connects already carry timeout=5.0 from
#    the v6.7.24 sweep (safe); routing them through the helper is deferred to a
#    separate 'policy-change'-tagged task. brain_engine._connect is NOT
#    allowlisted — it delegates to the helper, so it holds no bare connect.
_ALLOWLIST = {
    ("services", "sqlite_db.py"),
    ("services", "verifier_service.py"),
    ("services", "conductor_service.py"),
}

_PAT = re.compile(r"sqlite3\.connect\(")


def _rel_key(path: Path, root: Path) -> tuple[str, ...]:
    return path.relative_to(root).parts


def test_no_bare_sqlite_connect_outside_helper():
    root = Path(prism_service.__file__).parent
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        parts = _rel_key(py, root)
        if parts in _ALLOWLIST:
            continue
        text = py.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if _PAT.search(line):
                offenders.append(f"{'/'.join(parts)}:{i}")
    assert not offenders, (
        f"{len(offenders)} bare sqlite3.connect( outside the helper — "
        f"route through services/sqlite_db.connect():\n" + "\n".join(offenders[:20]))
