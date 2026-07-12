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
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


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
    return conn
