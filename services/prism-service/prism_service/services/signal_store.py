"""SignalStore -- persisted Queue intake rows (task a6858911).

Real sqlite rows in the project's OWN data directory, beside tasks.db /
brain.db / ontology.db (config.project_data_dir -- the same resolver every
other per-project store uses), as signals.db. A signal is intake only:
this store never writes a tasks row (that only happens when the owner acts
in the app, out of scope for this walking skeleton).

aligned_subject/aligned_body/style (task ed034701): create() runs the
same STE pipeline TaskService._apply_ste runs on every task write --
services.ste.normalize then services.lexicon.align, then services.ste.
check -- over subject and body, and stores the result in the new
columns. subject/body are never rewritten. update() (task aa7fab99)
re-runs the same alignment whenever subject or body is one of the
fields being changed, so a refreshed signal (e.g. rule_decisions'
dedup re-post) keeps its aligned columns current.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from prism_service.config import project_data_dir
from prism_service.models.signal import Signal
from prism_service.services import sqlite_db

logger = logging.getLogger(__name__)

# ALTER TABLE columns added after the walking skeleton shipped (task
# ed034701), mirrors TaskService._migrate_task_columns: backfilled on an
# existing signals.db, issued only when the column is actually missing.
_ALIGN_COLUMNS: list[tuple[str, str]] = [
    ("aligned_subject", "TEXT DEFAULT ''"),
    ("aligned_body", "TEXT DEFAULT ''"),
    ("style", "TEXT DEFAULT '{}'"),
]


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
                drop_reason TEXT DEFAULT '',
                aligned_subject TEXT DEFAULT '',
                aligned_body TEXT DEFAULT '',
                style TEXT DEFAULT '{}'
            );
            CREATE INDEX IF NOT EXISTS idx_signals_state ON signals(state);
            """
        )
        self._conn.commit()
        self._migrate_align_columns()

    def _migrate_align_columns(self) -> None:
        """Backfill aligned_subject/aligned_body/style on a signals.db
        created before task ed034701. Idempotent: an ALTER is only
        issued when the column is actually missing."""
        existing = {
            row[1] for row in self._conn.execute("PRAGMA table_info(signals)")
        }
        for col, col_decl in _ALIGN_COLUMNS:
            if col in existing:
                continue
            try:
                self._conn.execute(
                    f"ALTER TABLE signals ADD COLUMN {col} {col_decl}"
                )
                self._conn.commit()
            except sqlite3.OperationalError:
                pass

    def _row_to_signal(self, row: sqlite3.Row) -> Signal:
        keys = row.keys()
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
            aligned_subject=(row["aligned_subject"] if "aligned_subject" in keys else "") or "",
            aligned_body=(row["aligned_body"] if "aligned_body" in keys else "") or "",
            style=json.loads((row["style"] if "style" in keys else "") or "{}"),
        )

    def _align(self, signal: Signal) -> None:
        """Run the STE pipeline over subject/body and set aligned_subject/
        aligned_body/style on `signal` in place. Never raises -- a
        normaliser bug must never drop a signal, so a failure logs and
        leaves the aligned fields empty (task ed034701)."""
        try:
            from prism_service.services import ste
            from prism_service.services import lexicon

            def _process(value: str) -> tuple[str, list[str], list, list]:
                fixed, rules = ste.normalize(value, mode="flavored")
                fixed, aligned = lexicon.align(fixed)
                if aligned:
                    rules = list(rules) + ["lexicon"]
                findings = ste.check(fixed, mode="flavored")
                return fixed, rules, findings, aligned

            aligned_subject, subj_rules, subj_findings, subj_aligned = (
                _process(signal.subject))
            aligned_body, body_rules, body_findings, body_aligned = (
                _process(signal.body))

            signal.aligned_subject = aligned_subject
            signal.aligned_body = aligned_body
            signal.style = ste.style_block({
                "subject": (subj_rules, subj_findings, subj_aligned),
                "body": (body_rules, body_findings, body_aligned),
            })
        except Exception:
            logger.warning(
                "STE alignment failed for signal %s; the signal is stored "
                "with empty aligned fields.", signal.id, exc_info=True)
            signal.aligned_subject = ""
            signal.aligned_body = ""
            signal.style = {}

    def create(self, signal: Signal) -> Signal:
        self._align(signal)
        self._conn.execute(
            "INSERT INTO signals "
            "(id,project,channel,channel_ref,subject,body,sender,"
            "arrived_at,state,task_id,matches,drop_reason,"
            "aligned_subject,aligned_body,style) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                signal.id, signal.project, signal.channel, signal.channel_ref,
                signal.subject, signal.body, signal.sender, signal.arrived_at,
                signal.state, signal.task_id, json.dumps(signal.matches),
                signal.drop_reason, signal.aligned_subject,
                signal.aligned_body, json.dumps(signal.style),
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
        """Task aa7fab99: when `subject` or `body` is one of the updated
        fields, re-run the SAME alignment create() runs so aligned_subject/
        aligned_body/style track the NEW text -- a refreshed ontology
        signal (rule_decisions' dedup re-post) must keep showing CURRENT
        aligned text, never what the signal's first post produced."""
        signal = self.get(signal_id)
        if signal is None:
            return None
        for key, value in kwargs.items():
            if hasattr(signal, key):
                setattr(signal, key, value)
        if "subject" in kwargs or "body" in kwargs:
            self._align(signal)
        self._conn.execute(
            "UPDATE signals SET channel=?, channel_ref=?, subject=?, body=?, "
            "sender=?, state=?, task_id=?, matches=?, drop_reason=?, "
            "aligned_subject=?, aligned_body=?, style=? WHERE id=?",
            (
                signal.channel, signal.channel_ref, signal.subject, signal.body,
                signal.sender, signal.state, signal.task_id,
                json.dumps(signal.matches), signal.drop_reason,
                signal.aligned_subject, signal.aligned_body,
                json.dumps(signal.style), signal_id,
            ),
        )
        self._conn.commit()
        return signal

    def close(self) -> None:
        self._conn.close()
