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

# The ONE place a bare sqlite3.connect() is allowed to live: the central
# helper itself. brain_engine._connect is expected to DELEGATE to the
# helper (so it holds no bare connect either) — stricter than the story's
# floor, closing the spot a future bare connect could hide.
_ALLOWLIST = {("services", "sqlite_db.py")}

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
