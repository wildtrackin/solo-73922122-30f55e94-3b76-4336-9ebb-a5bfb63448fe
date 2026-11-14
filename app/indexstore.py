"""Persistent spatial catalog (SQLite RTree) and its background backfill.

Layout
------
* ``road_rtree``  - RTree virtual table keyed by an integer ``entry_id``
  holding each road's bounding box.  This is the spatial catalog used to
  pre-filter candidates for range queries.
* ``road_index_map`` - road id  <-> ``entry_id`` mapping (rtree ids are
  integers, road ids are text).
* ``index_dirty`` - ids of roads that changed while the catalog was still
  being built.  Batches write here instead of touching the rtree when the
  catalog is not ready, so the backfill never misses a concurrent change.
* ``index_meta``  - one row of backfill state: status (building/ready),
  scan cursor, progress counters, pause flag and the worker lease.

Consistency rules
-----------------
* Once ``status = 'ready'`` every batch updates the rtree **inside the same
  write transaction** as the point/road mutation, so point values, geometry
  and catalog always commit or roll back together.
* While ``status = 'building'`` batches only append to ``index_dirty`` (same
  transaction as the mutation); the backfill reconciles those entries against
  the current committed state.
* The backfill scans and computes bounding boxes on short read snapshots -
  it never holds the write lock while scanning.  Its write steps re-read the
  persisted geometry of each road inside the write transaction and apply an
  entry only when the row is byte-identical to what the scan saw, so a stale
  scan result can never overwrite a newer committed state.
* ``ready`` is flipped only inside a write transaction that proves the cursor
  scan reached the end of ``roads`` and ``index_dirty`` is empty.  Because
  SQLite allows a single writer, no batch can sneak a change past that check.
  The flag is persisted, so an interrupted backfill resumes after restart and
  can never report ready early.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .config import settings
from .db import read_tx, write_tx
from .spatial import Rect, geometry_bbox

log = logging.getLogger("gis.backfill")

STATUS_BUILDING = "building"
STATUS_READY = "ready"


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


# ---------------------------------------------------------------------------
# Catalog maintenance (called inside a batch write transaction)
# ---------------------------------------------------------------------------


def upsert_entry(conn, road_id: str, bbox: Rect) -> None:
    """Insert or refresh one road's rtree entry."""
    min_lon, min_lat, max_lon, max_lat = bbox
    row = conn.execute(
        "SELECT entry_id FROM road_index_map WHERE road_id = ?", (road_id,)
    ).fetchone()
    if row is None:
        cur = conn.execute(
            "INSERT INTO road_rtree(entry_id, min_lon, max_lon, min_lat, max_lat) "
            "VALUES (NULL, ?, ?, ?, ?)",
            (min_lon, max_lon, min_lat, max_lat),
        )
        conn.execute(
            "INSERT INTO road_index_map(road_id, entry_id) VALUES (?, ?)",
            (road_id, cur.lastrowid),
        )
    else:
        conn.execute(
            "UPDATE road_rtree SET min_lon = ?, max_lon = ?, min_lat = ?, max_lat = ? "
            "WHERE entry_id = ?",
            (min_lon, max_lon, min_lat, max_lat, row["entry_id"]),
        )


def delete_entry(conn, road_id: str) -> None:
    """Drop a road's rtree entry (road deleted or became incomplete)."""
    row = conn.execute(
        "SELECT entry_id FROM road_index_map WHERE road_id = ?", (road_id,)
    ).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM road_rtree WHERE entry_id = ?", (row["entry_id"],))
    conn.execute("DELETE FROM road_index_map WHERE road_id = ?", (road_id,))


def apply_geometry(conn, road_id: str, geometry: dict | None) -> None:
    """Reflect a road's persisted geometry in the catalog (ready mode)."""
    if geometry is None:
        delete_entry(conn, road_id)
    else:
        upsert_entry(conn, road_id, geometry_bbox(geometry))


def mark_dirty(conn, road_ids: Iterable[str]) -> None:
    """Record roads that changed while the catalog was still building."""
    ids = [(rid,) for rid in road_ids]
    if ids:
        conn.executemany("INSERT OR IGNORE INTO index_dirty(road_id) VALUES (?)", ids)


def catalog_status(conn) -> str:
    return str(conn.execute("SELECT status FROM index_meta WHERE id = 1").fetchone()[0])


# ---------------------------------------------------------------------------
# Range query (read snapshot only)
# ---------------------------------------------------------------------------


def candidate_road_ids(conn, rect: Rect) -> list[str]:
    """RTree pre-filter: roads whose bbox overlaps the window."""
    min_lon, min_lat, max_lon, max_lat = rect
    rows = conn.execute(
        "SELECT m.road_id AS road_id FROM road_rtree r "
        "JOIN road_index_map m ON m.entry_id = r.entry_id "
        "WHERE r.min_lon <= ? AND r.max_lon >= ? "
        "  AND r.min_lat <= ? AND r.max_lat >= ? "
        "ORDER BY m.road_id",
        (max_lon, min_lon, max_lat, min_lat),
    ).fetchall()
    return [r["road_id"] for r in rows]


# ---------------------------------------------------------------------------
# Backfill state / progress
# ---------------------------------------------------------------------------


def get_status(conn) -> dict:
    meta = conn.execute("SELECT * FROM index_meta WHERE id = 1").fetchone()
    pending = conn.execute("SELECT count(*) FROM index_dirty").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM roads").fetchone()[0]
    return {
        "status": meta["status"],
        "ready": meta["status"] == STATUS_READY,
        "paused": bool(meta["paused"]),
        "scanned": int(meta["scanned"]),
        "pending_changes": int(pending),
        "total_roads": int(total),
        "scan_cursor": meta["scan_cursor"],
        "owner": meta["owner"],
        "started_at": meta["started_at"] or None,
        "updated_at": meta["updated_at"] or None,
        "ready_at": meta["ready_at"],
    }


def set_paused(paused: bool) -> None:
    with write_tx() as conn:
        conn.execute(
            "UPDATE index_meta SET paused = ?, updated_at = ? WHERE id = 1",
            (1 if paused else 0, _now()),
        )


def request_rebuild(pause: bool) -> None:
    """Reset the catalog to 'building' so the backfill re-verifies everything.

    Existing rtree rows are kept and re-validated by the scan; roads changed
    while rebuilding are tracked through ``index_dirty`` as usual.
    """
    with write_tx() as conn:
        conn.execute(
            "UPDATE index_meta SET status = ?, scan_cursor = '', scanned = 0, "
            "paused = ?, ready_at = NULL, started_at = NULL, updated_at = ? "
            "WHERE id = 1",
            (STATUS_BUILDING, 1 if pause else 0, _now()),
        )


# ---------------------------------------------------------------------------
# Backfill worker
# ---------------------------------------------------------------------------


@dataclass
class ScanEntry:
    """What a lock-free read snapshot saw for one road."""

    road_id: str
    existed: bool                 # False = road absent in the read snapshot
    geometry_text: str | None     # persisted geometry column as read
    bbox: Rect | None             # None -> road must not appear in the catalog


def _apply_entries(conn, entries: list[ScanEntry]) -> list[str]:
    """Apply scanned entries, guarded against concurrent commits.

    Runs inside a write transaction.  An entry is written only when the
    road's persisted geometry is still byte-identical to what the lock-free
    scan read; otherwise the batch that changed it has left an ``index_dirty``
    row and the reconcile pass will index the road from its newer state.
    Returns the ids that were applied.
    """
    applied: list[str] = []
    for entry in entries:
        row = conn.execute(
            "SELECT geometry FROM roads WHERE id = ?", (entry.road_id,)
        ).fetchone()
        if not entry.existed:
            if row is not None:
                continue  # re-created since the read; dirty row covers it
            delete_entry(conn, entry.road_id)
            applied.append(entry.road_id)
            continue
        if row is None:
            continue  # deleted since the read; dirty row covers it
        if row["geometry"] != entry.geometry_text:
            continue  # changed since the read; dirty row covers it
        if entry.bbox is None:
            delete_entry(conn, entry.road_id)
        else:
            upsert_entry(conn, entry.road_id, entry.bbox)
        applied.append(entry.road_id)
    return applied


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _claim_lease(owner: str) -> bool:
    """Take or renew the backfill lease; only one of the API processes works."""
    now = time.time()
    with write_tx() as conn:
        row = conn.execute(
            "SELECT status, owner, lease_until FROM index_meta WHERE id = 1"
        ).fetchone()
        if row["status"] == STATUS_READY:
            return False
        if row["lease_until"] > now and row["owner"] != owner:
            return False
        conn.execute(
            "UPDATE index_meta SET owner = ?, lease_until = ?, updated_at = ?, "
            "started_at = CASE WHEN started_at IS NULL OR started_at = '' "
            "THEN ? ELSE started_at END WHERE id = 1",
            (owner, now + settings.backfill_lease_s, _now(), _now()),
        )
    return True


def scan_step(chunk: int | None = None) -> bool:
    """Advance the cursor scan by one chunk.  Returns True if rows were seen.

    The read (and the bbox computation) happen outside any write lock; the
    write step only verifies and stores the results.
    """
    chunk = chunk or settings.backfill_chunk
    with read_tx() as conn:
        cursor = conn.execute(
            "SELECT scan_cursor FROM index_meta WHERE id = 1"
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT id, geometry FROM roads WHERE id > ? ORDER BY id LIMIT ?",
            (cursor, chunk),
        ).fetchall()
    if not rows:
        return False
    entries = [
        ScanEntry(
            road_id=r["id"],
            existed=True,
            geometry_text=r["geometry"],
            bbox=geometry_bbox(json.loads(r["geometry"]))
            if r["geometry"] is not None
            else None,
        )
        for r in rows
    ]
    last_id = entries[-1].road_id
    with write_tx() as conn:
        if catalog_status(conn) == STATUS_READY:
            return False  # flipped while we were scanning; nothing to do
        _apply_entries(conn, entries)
        conn.execute(
            "UPDATE index_meta SET scan_cursor = ?, scanned = scanned + ?, "
            "updated_at = ? WHERE id = 1",
            (last_id, len(entries), _now()),
        )
    return True


def reconcile_step(chunk: int | None = None) -> bool:
    """Apply one chunk of ``index_dirty`` changes.  Returns True if any existed."""
    chunk = chunk or settings.backfill_chunk
    with read_tx() as conn:
        ids = [
            r["road_id"]
            for r in conn.execute(
                "SELECT road_id FROM index_dirty ORDER BY road_id LIMIT ?", (chunk,)
            )
        ]
        if not ids:
            return False
        marks = ",".join("?" * len(ids))
        rows = {
            r["id"]: r["geometry"]
            for r in conn.execute(
                f"SELECT id, geometry FROM roads WHERE id IN ({marks})", ids
            )
        }
    entries = [
        ScanEntry(
            road_id=rid,
            existed=rid in rows,
            geometry_text=rows.get(rid),
            bbox=geometry_bbox(json.loads(rows[rid]))
            if rows.get(rid) is not None
            else None,
        )
        for rid in ids
    ]
    with write_tx() as conn:
        if catalog_status(conn) == STATUS_READY:
            return False
        applied = _apply_entries(conn, entries)
        if applied:
            conn.executemany(
                "DELETE FROM index_dirty WHERE road_id = ?", [(r,) for r in applied]
            )
        conn.execute("UPDATE index_meta SET updated_at = ? WHERE id = 1", (_now(),))
    return True


def try_finish() -> bool:
    """Flip to ready when the scan reached the end and no change is pending.

    The checks and the flip happen in one write transaction; SQLite's single
    writer slot guarantees no batch can commit a change in between.
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT status, scan_cursor FROM index_meta WHERE id = 1"
        ).fetchone()
        if row["status"] == STATUS_READY:
            return True
        leftover = conn.execute(
            "SELECT 1 FROM roads WHERE id > ? LIMIT 1", (row["scan_cursor"],)
        ).fetchone()
        dirty = conn.execute("SELECT 1 FROM index_dirty LIMIT 1").fetchone()
        if leftover is not None or dirty is not None:
            return False
        conn.execute(
            "UPDATE index_meta SET status = ?, ready_at = ?, updated_at = ?, "
            "owner = NULL, lease_until = 0 WHERE id = 1",
            (STATUS_READY, _now(), _now()),
        )
    log.info("spatial catalog backfill complete -> ready")
    return True


def _status_flags() -> tuple[str, bool]:
    with read_tx() as conn:
        row = conn.execute(
            "SELECT status, paused FROM index_meta WHERE id = 1"
        ).fetchone()
    return str(row["status"]), bool(row["paused"])


def run_worker(stop: threading.Event) -> None:
    """Background loop: claim the lease, scan, reconcile, finish.  Resumable.

    Every step is small and its state lives in the database, so killing the
    process at any point simply resumes from the persisted cursor after a
    restart (or on the other API process once the lease expires).
    """
    owner = _lease_owner()
    interval = settings.backfill_step_interval_ms / 1000.0
    while not stop.is_set():
        try:
            status, paused = _status_flags()
            if status == STATUS_READY:
                stop.wait(2.0)  # idle; a rebuild request flips us back
                continue
            if paused:
                stop.wait(0.3)
                continue
            if not _claim_lease(owner):
                stop.wait(0.3)
                continue
            worked = scan_step()
            worked = reconcile_step() or worked
            if not worked:
                try_finish()
            stop.wait(interval)
        except Exception:  # pragma: no cover - defensive; worker must not die
            log.exception("backfill step failed; retrying")
            stop.wait(1.0)
