"""The ONE sqlite connection chokepoint (task dde1162f).

sqlite-hardening workstream. The v6.7.24 pass had to sweep `timeout=5.0`
across ~41 call sites ONE BY ONE because there was no funnel — any new
bare ``sqlite3`` connect could silently forget it and reintroduce the
97.6% lock-error rate measured at 8 concurrent writers. This module is
that funnel: every service/api/route opens its connection through
``connect()`` so the four canonical settings are applied in exactly one
place. ``engines.brain_engine._connect`` delegates here too, then layers
its Brain-only FTS function on top — one source of truth for the PRAGMAs.

Canonical settings (do not diverge — change them HERE):
- ``timeout=5.0``           — wait up to 5s for the file lock on connect
- ``row_factory=Row``       — name- and index-addressable rows everywhere
- ``PRAGMA journal_mode=WAL``   — readers never block the writer
- ``PRAGMA busy_timeout=5000``  — cap the writer-lock wait so a stuck txn
  surfaces as SQLITE_BUSY instead of stalling the uvicorn loop (issue #38)
- ``PRAGMA recursive_triggers=ON`` — SQLite defaults this OFF, and with it
  OFF an ``INSERT OR REPLACE`` conflict deletes the old row WITHOUT firing
  the AFTER DELETE trigger. Brain's ``docs_fts_ad`` is such a trigger, so
  every re-index of a changed document left its superseded FTS5 entry
  behind at a rowid no ``docs`` row occupies (task 72ccaf94: 306,959
  segment rows for 1,419 live documents)
- ``PRAGMA journal_size_limit`` — cap the ``-wal`` file, read from
  ``PRISM_SQLITE_JOURNAL_SIZE_LIMIT`` (bytes, default 64 MB). Without it a
  WAL that a long-lived reader keeps pinned only ever grows; the limit is
  what lets a PASSIVE checkpoint hand the space back
- ``PRAGMA synchronous=NORMAL`` — the standard WAL pairing: fsync at the
  checkpoint boundary instead of every commit. Corruption-safe under WAL;
  the only exposure is the last few commits on a full OS crash, and the
  default FULL was costing an fsync per task_history/agent_runs write on
  every SDLC transition (task 9974d407, "PRISM feels instant")
"""

from __future__ import annotations

import ast
import os
import sqlite3
from pathlib import Path

DEFAULT_JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024


def journal_size_limit() -> int:
    """Read PRISM_SQLITE_JOURNAL_SIZE_LIMIT (bytes); fall back to 64 MB."""
    raw = os.environ.get("PRISM_SQLITE_JOURNAL_SIZE_LIMIT", "")
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return DEFAULT_JOURNAL_SIZE_LIMIT


def connect(path: "str | Path", *, timeout: float = 5.0, **kwargs) -> sqlite3.Connection:
    """Open ``path`` with the canonical hardening applied.

    Extra keyword args are forwarded to ``sqlite3.connect`` so the ~80
    existing ``sqlite3.connect(db, timeout=5.0)`` sites reroute by name
    alone (the redundant ``timeout=5.0`` they pass just re-sets the same
    default). Returns a ``sqlite3.Connection`` with ``row_factory=Row``.
    """
    conn = sqlite3.connect(str(path), timeout=timeout, **kwargs)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA recursive_triggers=ON")
    # Must follow journal_mode=WAL: the limit applies to the WAL file.
    conn.execute(f"PRAGMA journal_size_limit={journal_size_limit()}")
    return conn


def find_bare_connects(root: "str | Path") -> list[str]:
    """Return ``relpath:line`` for every sqlite3 connect under ``root``.

    The funnel invariant, made checkable on an arbitrary tree. The scan
    parses each module, so an ALIASED import (``import sqlite3 as _s`` then
    ``_s.connect(..)``) is caught where a text match for ``sqlite3.connect(``
    is blind, and a comment or docstring that names the call cannot trip it.
    ``root`` itself is not filtered: the caller owns its own allowlist.
    """
    root = Path(root)
    hits: list[str] = []
    for py in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        # Bindings are collected per module, not per scope: prism_service
        # imports sqlite3 inside function bodies, and a wide binding set
        # only makes the scan stricter, never blinder.
        modules: set[str] = set()
        direct: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlite3":
                        modules.add(alias.asname or "sqlite3")
            elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                for alias in node.names:
                    if alias.name == "connect":
                        direct.add(alias.asname or "connect")
        rel = py.relative_to(root).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "connect"
                    and isinstance(func.value, ast.Name)
                    and func.value.id in modules):
                hits.append(f"{rel}:{node.lineno}")
            elif isinstance(func, ast.Name) and func.id in direct:
                hits.append(f"{rel}:{node.lineno}")
    return hits
