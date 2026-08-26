"""SignalStore -- persisted Queue intake rows (task a6858911).

Real sqlite rows in the project's OWN data directory, beside tasks.db /
brain.db / ontology.db (config.project_data_dir -- the same resolver every
other per-project store uses), as signals.db. A signal is intake only:
this store never writes a tasks row (that only happens when the owner acts
in the app, out of scope for this walking skeleton).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Optional

from prism_service.config import project_data_dir
from prism_service.models.signal import Signal
from prism_service.services import sqlite_db


class SignalStore:
    def __init__(self, project: str) -> None:
        self._db_path = project_data_dir(project) / "signals.db"
        # sqlite chokepoint (timeout + WAL + busy_timeout), never bare connect.
        self._conn = sqlite_db.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                channel TEXT DEFAULT '',
                channel_ref TEXT DEFAULT '',
                subject TEXT DEFAULT '',
                body TEXT DEFAULT '',
                sender TEXT DEFAULT '',
                arrived_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'open',
                task_id TEXT DEFAULT '',
                matches TEXT DEFAULT '{}',
                drop_reason TEXT DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
            """
        )
        self._conn.commit()

    def _row_to_signal(self, row: sqlite3.Row) -> Signal:
        return Signal(
            id=row["id"],
            project=row["project"],
            channel=row["channel"],
            channel_ref=row["channel_ref"],
            subject=row["subject"],
            body=row["body"],
            sender=row["sender"],
            arrived_at=row["arrived_at"],
            state=row["state"],
            task_id=row["task_id"],
            matches=json.loads(row["matches"] or "{}"),
            drop_reason=row["drop_reason"],
        )

    def create(self, signal: Signal) -> Signal:
        self._conn.execute(
            "INSERT INTO signals "
            "(id,project,channel,channel_ref,subject,body,sender,"
            "arrived_at,state,task_id,matches,drop_reason) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal.id, signal.project, signal.channel, signal.channel_ref,
                signal.subject, signal.body, signal.sender, signal.arrived_at,
                signal.state, signal.task_id, json.dumps(signal.matches),
                signal.drop_reason,
            ),
        )
        self._conn.commit()
        return signal

    def get(self, signal_id: str) -> Optional[Signal]:
        row = self._conn.execute(
            "SELECT * FROM signals WHERE id=?", (signal_id,)
        ).fetchone()
        return self._row_to_signal(row) if row else None

    def list(self, state: Optional[str] = None, limit: int = 200) -> list[Signal]:
        if state:
            rows = self._conn.execute(
                "SELECT * FROM signals WHERE state=? "
                "ORDER BY arrived_at DESC LIMIT ?", (state, limit),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM signals ORDER BY arrived_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return [self._row_to_signal(r) for r in rows]

    def update(self, signal_id: str, **kwargs: object) -> Optional[Signal]:
        signal = self.get(signal_id)
        if signal is None:
            return None
        for key, value in kwargs.items():
            if hasattr(signal, key):
                setattr(signal, key, value)
        self._conn.execute(
            "UPDATE signals SET channel=?, channel_ref=?, subject=?, body=?, "
            "sender=?, state=?, task_id=?, matches=?, drop_reason=? WHERE id=?",
            (
                signal.channel, signal.channel_ref, signal.subject, signal.body,
                signal.sender, signal.state, signal.task_id,
                json.dumps(signal.matches), signal.drop_reason, signal_id,
            ),
        )
        self._conn.commit()
        return signal

    def close(self) -> None:
        self._conn.close()
