"""RED: the chokepoint guard is blind to the ALIASED import form.

trace: sqlite-hardening / task 22ee4cb3 (route the judge machinery through
services/sqlite_db.connect()).

tests/unit/test_no_bare_connect.py matches the literal text
``sqlite3.connect(``. Seven live call sites in prism_service/ spell the same
call through an alias -- ``import sqlite3 as _sq3`` then ``_sq3.connect(..)``
-- so the regex cannot see them and the guard passes while the funnel leaks.
That is this task's likely_misfire word for word: "leaving the test allowlist
over-permissive so a future bare connect can hide".

The leak is not cosmetic. Every aliased site skips the canonical PRAGMAs that
services/sqlite_db.py documents, including ``recursive_triggers=ON`` -- the
PRAGMA whose absence produced 306,959 orphan FTS segment rows for 1,419 live
documents in task 72ccaf94. Two of the seven sites run ``INSERT OR REPLACE``,
which is the exact statement class that defect turns on.
"""
from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import prism_service
from prism_service.services import sqlite_db

# The ONE funnel. Same set as tests/unit/test_no_bare_connect.py::_ALLOWLIST;
# both guards must agree, so widen them together or not at all.
_ALLOWLIST = {
    ("services", "sqlite_db.py"),
}


def _connect_calls(source: str) -> list[int]:
    """Return the line of every sqlite3 connect call, whatever it is named.

    Parses the module, so a comment or a docstring that mentions the call
    cannot satisfy or trip this check. Import bindings are collected for the
    whole module rather than per scope: prism_service imports sqlite3 inside
    function bodies, and an over-wide binding set only makes the guard
    stricter, never blinder.
    """
    tree = ast.parse(source)
    module_names: set[str] = set()
    direct_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    module_names.add(alias.asname or "sqlite3")
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name == "connect":
                    direct_names.add(alias.asname or "connect")
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "connect"
                and isinstance(func.value, ast.Name)
                and func.value.id in module_names):
            hits.append(node.lineno)
        elif isinstance(func, ast.Name) and func.id in direct_names:
            hits.append(node.lineno)
    return hits


def test_no_aliased_bare_sqlite_connect_in_prism_service():
    """AC-1, widened: zero sqlite3 connects in prism_service outside the funnel.

    RED at 5a7f0e39 with 7 offenders: mcp/tools.py:3591, :4364, :4406 and
    services/graph_service.py:472, :714, :740, :786.
    """
    root = Path(prism_service.__file__).parent
    offenders: list[str] = []
    for py in sorted(root.rglob("*.py")):
        parts = py.relative_to(root).parts
        if parts in _ALLOWLIST:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        offenders += [f"{'/'.join(parts)}:{n}" for n in _connect_calls(src)]
    assert offenders == [], (
        f"{len(offenders)} sqlite3 connect(s) bypass "
        f"services/sqlite_db.connect() -- an aliased import hides from the "
        f"text guard in test_no_bare_connect.py:\n" + "\n".join(offenders))


_PLANTED = (
    "import sqlite3 as _s\n"
    "def go(p):\n"
    "    return _s.connect(p)\n"
)
_ROUTED = (
    "from prism_service.services import sqlite_db\n"
    "def go(p):\n"
    "    return sqlite_db.connect(p)\n"
)


def test_the_funnel_guard_flags_a_planted_aliased_connect(tmp_path):
    """AC-2's tooth clause, proven on a fixture tree instead of by planting.

    The scanner must be callable on an arbitrary root, so the guard can be
    refuted without editing a control_plane.POLICY_FILES entry in this shared
    checkout. RED at 5a7f0e39: services.sqlite_db exposes no such helper.
    """
    finder = getattr(sqlite_db, "find_bare_connects", None)
    assert callable(finder), (
        "services/sqlite_db.py must expose find_bare_connects(root) so the "
        "funnel invariant is checkable against a fixture tree")
    (tmp_path / "leaky.py").write_text(_PLANTED, encoding="utf-8")
    flagged = finder(tmp_path)
    assert [str(f).replace("\\", "/") for f in flagged] == ["leaky.py:3"], (
        f"an aliased connect must be flagged; got {flagged!r}")
    (tmp_path / "leaky.py").write_text(_ROUTED, encoding="utf-8")
    assert list(finder(tmp_path)) == [], (
        "a call that routes through the funnel must not be flagged")


class _Recorder(sqlite3.Connection):
    """Snapshots the PRAGMAs a caller left set, at the moment it closes."""

    seen: list[dict] = []

    def close(self):
        try:
            self.seen.append({
                "recursive_triggers":
                    self.execute("PRAGMA recursive_triggers").fetchone()[0],
                "journal_mode":
                    self.execute("PRAGMA journal_mode").fetchone()[0],
                "busy_timeout":
                    self.execute("PRAGMA busy_timeout").fetchone()[0],
            })
        finally:
            super().close()


def test_graph_community_summary_opens_a_hardened_connection(
        tmp_path, monkeypatch):
    """AC-4 as BEHAVIOUR: the connection carries the canonical hardening.

    A grep proves a spelling; this proves what the process actually opened.
    RED at 5a7f0e39: graph_service.py:472 opens brain.db raw, so the
    connection reports recursive_triggers=0 -- the exact PRAGMA whose absence
    left 306,959 orphan FTS rows in task 72ccaf94.
    """
    from prism_service.services import graph_service

    db = tmp_path / "brain.db"
    seed = sqlite3.connect(db)
    seed.execute("CREATE TABLE docs (entity_name TEXT, content TEXT)")
    seed.execute("INSERT INTO docs VALUES ('Widget', 'a widget of note')")
    seed.commit()
    seed.close()

    real = sqlite3.connect
    _Recorder.seen = []
    monkeypatch.setattr(
        sqlite3, "connect",
        lambda *a, **k: real(*a, **{**k, "factory": _Recorder}))

    graph_service._derive_community_summary(["w.py"], ["Widget"], str(db))

    assert _Recorder.seen, "the summary path opened no connection to brain.db"
    opened = _Recorder.seen[0]
    assert opened["recursive_triggers"] == 1, (
        f"brain.db opened without recursive_triggers=ON: {opened}")
    assert str(opened["journal_mode"]).lower() == "wal", (
        f"brain.db opened without journal_mode=WAL: {opened}")
    assert opened["busy_timeout"] == 5000, (
        f"brain.db opened without busy_timeout=5000: {opened}")
