"""SQLite state store: idempotence + mapping memory for the sync engine."""

from __future__ import annotations

import contextlib
import logging
import os
import sqlite3
from pathlib import Path

from edupagetasks.models import MapRow

logger = logging.getLogger("edupagetasks.state")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS homework_map (
  edu_userid TEXT NOT NULL,
  timelineid INTEGER NOT NULL,
  vikunja_task_id INTEGER,
  vikunja_project_id INTEGER NOT NULL,
  content_fingerprint TEXT NOT NULL,
  done_state INTEGER NOT NULL DEFAULT 0,
  school_year INTEGER NOT NULL,
  last_seen_due_date TEXT,
  last_seen_at TEXT NOT NULL,
  closed_at TEXT,
  pending_ops TEXT NOT NULL DEFAULT '[]',
  retry_count INTEGER NOT NULL DEFAULT 0,
  sync_state TEXT NOT NULL,
  anchor_label_id INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (edu_userid, timelineid)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
PRAGMA user_version = 1;
"""

_UNSET = object()


def _row_to_map(row: sqlite3.Row) -> MapRow:
    return MapRow(
        userid=row["edu_userid"],
        timelineid=row["timelineid"],
        vikunja_task_id=row["vikunja_task_id"],
        vikunja_project_id=row["vikunja_project_id"],
        content_fingerprint=row["content_fingerprint"],
        done_state=bool(row["done_state"]),
        school_year=row["school_year"],
        last_seen_due_date=row["last_seen_due_date"],
        last_seen_at=row["last_seen_at"],
        closed_at=row["closed_at"],
        pending_ops=row["pending_ops"],
        retry_count=row["retry_count"],
        sync_state=row["sync_state"],
        anchor_label_id=row["anchor_label_id"],
        updated_at=row["updated_at"],
    )


class StateStore:
    """Persistent (edu_userid, timelineid) -> Vikunja task mapping."""

    def __init__(self, path: str, *, project_default: int, read_only: bool = False) -> None:
        self.path = path
        self.project_default = project_default
        self.read_only = read_only
        self._conn: sqlite3.Connection | None = None
        self._migrated = False
        self._txn_depth = 0
        self.open_and_migrate()

    @property
    def connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("state store is closed")
        return self._conn

    def _chmod_all(self) -> None:
        """Lock down the db plus any WAL sidecars to 0600.

        SQLite creates ``state.db-wal``/``state.db-shm`` with the default umask
        (0644), leaking homework/done data; force 0600 whenever they exist.
        """
        for name in (self.path, f"{self.path}-wal", f"{self.path}-shm"):
            try:
                os.chmod(name, 0o600)
            except OSError:
                pass

    def open_and_migrate(self) -> None:
        if self._migrated and self._conn is not None:
            return
        if self.read_only:
            # A dry run needs the current mapping but must not create the DB,
            # migrate its schema, or update its metadata. Work from a writable
            # memory snapshot so reconciliation can be simulated safely.
            conn = sqlite3.connect(":memory:", isolation_level=None)
            conn.row_factory = sqlite3.Row
            if os.path.exists(self.path):
                source = sqlite3.connect(
                    f"{Path(self.path).resolve().as_uri()}?mode=ro", uri=True
                )
                try:
                    source.backup(conn)
                finally:
                    source.close()
            else:
                conn.executescript(_SCHEMA)
            self._conn = conn
            self._migrated = True
            return
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.executescript(_SCHEMA)
        self._chmod_all()
        self._conn = conn
        self._migrated = True

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            if not self.read_only:
                self._chmod_all()
            self._conn = None
            self._migrated = False

    @contextlib.contextmanager
    def in_transaction(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        if self._txn_depth:
            raise RuntimeError("nested in_transaction() calls are not supported")
        self._txn_depth += 1
        conn = self.connection
        try:
            conn.execute("BEGIN")
            yield conn
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except Exception as rollback_exc:  # noqa: BLE001 - keep the original error
                logger.warning(
                    "rollback after transaction failure failed: %s", rollback_exc
                )
            raise
        else:
            conn.execute("COMMIT")
        finally:
            self._txn_depth -= 1

    def upsert_row(self, row: MapRow) -> None:
        self.connection.execute(
            """
            INSERT INTO homework_map (
              edu_userid, timelineid, vikunja_task_id, vikunja_project_id,
              content_fingerprint, done_state, school_year, last_seen_due_date,
              last_seen_at, closed_at, pending_ops, retry_count, sync_state,
              anchor_label_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(edu_userid, timelineid) DO UPDATE SET
              vikunja_task_id=excluded.vikunja_task_id,
              vikunja_project_id=excluded.vikunja_project_id,
              content_fingerprint=excluded.content_fingerprint,
              done_state=excluded.done_state,
              school_year=excluded.school_year,
              last_seen_due_date=excluded.last_seen_due_date,
              last_seen_at=excluded.last_seen_at,
              closed_at=excluded.closed_at,
              pending_ops=excluded.pending_ops,
              retry_count=excluded.retry_count,
              sync_state=excluded.sync_state,
              anchor_label_id=excluded.anchor_label_id,
              updated_at=excluded.updated_at
            """,
            (
                row.userid,
                row.timelineid,
                row.vikunja_task_id,
                row.vikunja_project_id,
                row.content_fingerprint,
                int(row.done_state),
                row.school_year,
                row.last_seen_due_date,
                row.last_seen_at,
                row.closed_at,
                row.pending_ops,
                row.retry_count,
                row.sync_state,
                row.anchor_label_id,
                row.updated_at,
            ),
        )

    def get_row(self, userid: str, timelineid: int) -> MapRow | None:
        row = self.connection.execute(
            "SELECT * FROM homework_map WHERE edu_userid=? AND timelineid=?",
            (userid, timelineid),
        ).fetchone()
        return None if row is None else _row_to_map(row)

    def rows(self) -> list[MapRow]:
        rows = self.connection.execute(
            "SELECT * FROM homework_map ORDER BY edu_userid, timelineid"
        ).fetchall()
        return [_row_to_map(r) for r in rows]

    def rows_by_state(self, state: object) -> list[MapRow]:
        rows = self.connection.execute(
            "SELECT * FROM homework_map WHERE sync_state=? ORDER BY edu_userid, timelineid",
            (str(state),),
        ).fetchall()
        return [_row_to_map(r) for r in rows]

    def delete_row(self, userid: str, timelineid: int) -> None:
        self.connection.execute(
            "DELETE FROM homework_map WHERE edu_userid=? AND timelineid=?",
            (userid, timelineid),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_meta_int(self, key: str, default: int) -> int:
        value = self.get_meta(key)
        return default if value is None else int(value)

    def meta_counters_tap(self, key: str) -> int:
        with self.in_transaction() as conn:
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, '1')"
                " ON CONFLICT(key) DO UPDATE SET"
                " value=CAST(CAST(meta.value AS INTEGER) + 1 AS TEXT)",
                (key,),
            )
            row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            raise RuntimeError(
                f"counter '{key}' did not persist (project_default={self.project_default})"
            )
        return int(row[0])
