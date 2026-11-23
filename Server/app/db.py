"""SQLite storage. One shared connection guarded by a lock (small office scale)."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    department  TEXT    NOT NULL DEFAULT '',
    email       TEXT    NOT NULL DEFAULT '',
    active      INTEGER NOT NULL DEFAULT 1,
    created_ts  REAL    NOT NULL
);

-- An off-the-shelf BLE tag, matched by MAC address and/or iBeacon ID.
CREATE TABLE IF NOT EXISTS tags (
    id          INTEGER PRIMARY KEY,
    employee_id INTEGER REFERENCES employees(id) ON DELETE SET NULL,
    mac         TEXT UNIQUE,
    ib_uuid     TEXT,
    ib_major    INTEGER,
    ib_minor    INTEGER,
    label       TEXT    NOT NULL DEFAULT '',
    created_ts  REAL    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS tags_ibeacon
    ON tags(ib_uuid, ib_major, ib_minor) WHERE ib_uuid IS NOT NULL;

-- ESP32-C5 checkpoints; id is the device's Wi-Fi MAC.
CREATE TABLE IF NOT EXISTS checkpoints (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    location       TEXT NOT NULL DEFAULT '',
    fw             TEXT NOT NULL DEFAULT '',
    ip             TEXT NOT NULL DEFAULT '',
    last_report_ts REAL
);

-- Raw sightings of registered tags (pruned after the retention period).
CREATE TABLE IF NOT EXISTS sightings (
    id            INTEGER PRIMARY KEY,
    ts            REAL    NOT NULL,
    checkpoint_id TEXT    NOT NULL,
    tag_id        INTEGER NOT NULL,
    rssi          INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sightings_ts ON sightings(ts);
CREATE INDEX IF NOT EXISTS sightings_tag_ts ON sightings(tag_id, ts);

-- A continuous stretch of presence. end_ts is NULL while the person is present.
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY,
    employee_id   INTEGER NOT NULL REFERENCES employees(id) ON DELETE CASCADE,
    start_ts      REAL    NOT NULL,
    last_seen_ts  REAL    NOT NULL,
    end_ts        REAL,
    checkpoint_id TEXT,
    best_rssi     INTEGER
);
CREATE INDEX IF NOT EXISTS sessions_employee ON sessions(employee_id, start_ts);
CREATE INDEX IF NOT EXISTS sessions_open ON sessions(employee_id) WHERE end_ts IS NULL;
"""


class Database:
    def __init__(self, path: Path | str):
        path = str(path)
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.lock = threading.RLock()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(employees)")}
        if "email" not in cols:
            self.conn.execute("ALTER TABLE employees ADD COLUMN email TEXT NOT NULL DEFAULT ''")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Run a block of statements as one transaction."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def all(self, sql: str, args: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self.lock:
            self.conn.close()
