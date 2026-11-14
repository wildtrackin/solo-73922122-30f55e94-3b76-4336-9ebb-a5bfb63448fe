"""SQLite access layer.

Design notes
------------
* One short-lived connection per unit of work.  Every request (read or write)
  grabs its own connection, so no lock is held across segment assembly or
  across an HTTP response.
* Writers run inside ``BEGIN IMMEDIATE`` and therefore serialise on SQLite's
  single writer slot.  Because ``busy_timeout`` is set and the transaction is
  short, a second API process simply waits instead of failing.
* Readers run inside a deferred transaction and read one consistent snapshot
  of the committed state for the whole request.  In WAL mode readers never
  block the writer and the writer never blocks readers.
* Geometry is recomputed and persisted inside the same transaction that
  mutates points or roads.  Nothing is deferred, so a commit can never leave
  stale or partially updated geometry behind.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS points (
    id          TEXT PRIMARY KEY,
    lon         REAL NOT NULL,
    lat         REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS roads (
    id                TEXT PRIMARY KEY,
    geometry          TEXT,             -- materialised GeoJSON LineString, NULL = incomplete
    missing_point_ids TEXT NOT NULL DEFAULT '[]',
    revision          INTEGER NOT NULL DEFAULT 0,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS road_points (
    road_id     TEXT NOT NULL REFERENCES roads(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    -- deliberately NOT a foreign key to points: a dangling reference is a
    -- representable state (the road is incomplete and reports the ids) so
    -- that re-supplying the point restores the road automatically
    point_id    TEXT NOT NULL,
    PRIMARY KEY (road_id, seq),
    UNIQUE (road_id, point_id)
);
CREATE INDEX IF NOT EXISTS idx_road_points_point ON road_points(point_id);

CREATE TABLE IF NOT EXISTS store_meta (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    generation  INTEGER NOT NULL
);
INSERT OR IGNORE INTO store_meta(id, generation) VALUES (1, 0);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        settings.db_path,
        timeout=settings.busy_timeout_s,
        isolation_level=None,          # transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = %d" % int(settings.busy_timeout_s * 1000))
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def init_db() -> None:
    """Create the schema; safe when both API processes start at once."""
    last: Exception | None = None
    for _ in range(10):
        conn = _connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            return
        except sqlite3.OperationalError as exc:  # pragma: no cover - startup race
            last = exc
            time.sleep(0.1)
        finally:
            conn.close()
    raise last  # pragma: no cover


@contextmanager
def read_tx() -> Iterator[sqlite3.Connection]:
    """Deferred transaction -> one consistent committed snapshot per request."""
    conn = _connect()
    try:
        conn.execute("BEGIN")
        yield conn
    finally:
        try:
            conn.execute("COMMIT")
        except sqlite3.Error:  # pragma: no cover - read-only, nothing to undo
            pass
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Immediate transaction -> single writer, short critical section."""
    conn = _connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()
