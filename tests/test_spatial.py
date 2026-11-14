"""Spatial range query + persistent RTree catalog + background backfill tests."""
from __future__ import annotations

import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

# ---------------------------------------------------------------------------
# pure geometry math
# ---------------------------------------------------------------------------

from app.spatial import (
    linestring_intersects_rect,
    point_in_rect,
    segment_intersects_rect,
    segments_intersect,
)

RECT = (10.0, 10.0, 20.0, 20.0)  # min_lon, min_lat, max_lon, max_lat


def test_point_in_rect_is_closed():
    assert point_in_rect(15, 15, RECT)
    assert point_in_rect(10, 10, RECT)  # corner
    assert point_in_rect(20, 15, RECT)  # on the edge
    assert not point_in_rect(20.0001, 15, RECT)
    assert not point_in_rect(9.9999, 15, RECT)


def test_segments_intersect_basic():
    assert segments_intersect((0, 0), (4, 4), (0, 4), (4, 0))
    assert not segments_intersect((0, 0), (1, 1), (0, 4), (4, 0))
    # collinear overlap
    assert segments_intersect((0, 0), (4, 0), (2, 0), (6, 0))
    assert not segments_intersect((0, 0), (1, 0), (2, 0), (6, 0))
    # shared endpoint
    assert segments_intersect((0, 0), (2, 2), (2, 2), (5, 0))


def test_segment_crosses_rect_with_both_endpoints_outside():
    # straight horizontal pass through the window
    assert segment_intersects_rect((0, 15), (30, 15), RECT)
    # diagonal pass
    assert segment_intersects_rect((0, 0), (30, 30), RECT)
    # vertical pass
    assert segment_intersects_rect((15, 0), (15, 30), RECT)


def test_segment_on_rect_boundary_hits():
    # collinear with the bottom edge
    assert segment_intersects_rect((5, 10), (25, 10), RECT)
    # touches one corner only
    assert segment_intersects_rect((0, 0), (10, 10), RECT)


def test_segment_outside_misses():
    assert not segment_intersects_rect((0, 5), (5, 5), RECT)
    assert not segment_intersects_rect((0, 0), (9, 9), RECT)
    assert not segment_intersects_rect((21, 0), (30, 40), RECT)


def test_linestring_all_vertices_outside_still_hits():
    # a polyline that skirts around and pierces the window
    line = [[0, 15], [5, 15], [30, 15], [40, 0]]
    assert linestring_intersects_rect(line, RECT)
    # completely outside
    assert not linestring_intersects_rect([[0, 0], [5, 5], [9, 1]], RECT)
    # vertex inside
    assert linestring_intersects_rect([[0, 0], [15, 15], [30, 0]], RECT)


# ---------------------------------------------------------------------------
# API fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "gis.db"))
    with TestClient(app) as c:
        yield c


def wait_ready(client, timeout=20.0):
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = client.get("/api/v1/index/status").json()
        if st["ready"]:
            return st
        time.sleep(0.05)
    raise AssertionError(f"spatial catalog did not become ready: {st}")


def post(client, body, **params):
    r = client.post("/api/v1/batches", json=body, params=params)
    assert r.status_code == 201, r.text
    return r.json()


def spatial(client, min_lon, min_lat, max_lon, max_lat, expect=200):
    r = client.get(
        "/api/v1/spatial/roads",
        params={
            "min_lon": min_lon,
            "min_lat": min_lat,
            "max_lon": max_lon,
            "max_lat": max_lat,
        },
    )
    assert r.status_code == expect, r.text
    return r.json()


# grid of points: PT-i-j at (100 + i, 30 + j)
def grid_batch():
    points = [
        {"id": f"PT-{i}-{j}", "lon": 100.0 + i, "lat": 30.0 + j}
        for i in range(6)
        for j in range(3)
    ]
    roads = [
        {"id": "RD-EW", "point_ids": ["PT-0-1", "PT-5-1"]},          # crosses x=1..4 windows
        {"id": "RD-NS", "point_ids": ["PT-2-0", "PT-2-2"]},          # vertical at x=102
        {"id": "RD-FAR", "point_ids": ["PT-4-2", "PT-5-2"]},         # east, away from window
    ]
    return {"points": points, "roads": roads}


# ---------------------------------------------------------------------------
# range query behaviour
# ---------------------------------------------------------------------------


def test_range_query_returns_full_geometry_of_intersecting_roads(client):
    wait_ready(client)
    post(client, grid_batch())

    # window [101,101.5]x[30,31]: RD-EW crosses it (vertices at x=100/105 are
    # both outside!), RD-NS passes through x=102... no: 102 > 101.5 -> only RD-EW
    got = spatial(client, 101.0, 30.0, 101.5, 31.0)
    assert [r["id"] for r in got["roads"]] == ["RD-EW"]
    road = got["roads"][0]
    # complete, unclipped geometry is returned
    assert road["geometry"] == {
        "type": "LineString",
        "coordinates": [[100.0, 31.0], [105.0, 31.0]],
    }
    assert road["status"] == "ok"
    assert got["total"] == 1
    assert got["window"] == {"min_lon": 101.0, "min_lat": 30.0, "max_lon": 101.5, "max_lat": 31.0}
    # identical to the committed state from the plain read API
    assert client.get("/api/v1/roads/RD-EW").json()["geometry"] == road["geometry"]


def test_road_crossing_window_with_all_vertices_outside_hits(client):
    wait_ready(client)
    post(client, grid_batch())
    # window pierced by RD-EW (horizontal, endpoints at x=100/105) and by
    # RD-NS (vertical at x=102, endpoints at y=30/32): all vertices outside
    got = spatial(client, 102.0, 30.9, 103.0, 31.1)
    assert {r["id"] for r in got["roads"]} == {"RD-EW", "RD-NS"}


def test_window_on_boundary_is_closed(client):
    wait_ready(client)
    post(client, grid_batch())
    # window edge exactly on the RD-EW line (lat 31)
    got = spatial(client, 101.0, 31.0, 102.0, 32.0)
    assert "RD-EW" in {r["id"] for r in got["roads"]}


def test_disjoint_window_returns_empty(client):
    wait_ready(client)
    post(client, grid_batch())
    got = spatial(client, -50.0, -40.0, -40.0, -30.0)  # valid but far away
    assert got["roads"] == [] and got["total"] == 0


def test_incomplete_road_is_excluded_then_restored(client):
    wait_ready(client)
    batch = grid_batch()
    batch["roads"].append({"id": "RD-BROKEN", "point_ids": ["PT-1-0", "PT-GONE"]})
    post(client, batch)
    assert client.get("/api/v1/roads/RD-BROKEN").json()["status"] == "incomplete"

    got = spatial(client, 100.0, 29.0, 102.0, 31.0)
    assert "RD-BROKEN" not in {r["id"] for r in got["roads"]}

    # supply the missing point inside the window -> road becomes ok and indexed
    post(client, {"points": [{"id": "PT-GONE", "lon": 101.5, "lat": 30.5}]})
    assert client.get("/api/v1/roads/RD-BROKEN").json()["status"] == "ok"
    got = spatial(client, 100.0, 29.0, 102.0, 31.0)
    assert "RD-BROKEN" in {r["id"] for r in got["roads"]}


def test_failed_batch_rolls_back_catalog_too(client):
    """Points, geometry AND the catalog commit or roll back as one."""
    wait_ready(client)
    post(client, grid_batch())
    win = dict(min_lon=101.0, min_lat=30.0, max_lon=101.5, max_lat=31.0)
    before = spatial(client, **win)
    assert [r["id"] for r in before["roads"]] == ["RD-EW"]

    # move RD-FAR into the window but inject a failure before commit
    r = client.post(
        "/api/v1/batches",
        params={"fail": "true"},
        json={
            "points": [{"id": "PT-4-2", "lon": 101.1, "lat": 30.5},
                       {"id": "PT-5-2", "lon": 101.2, "lat": 30.6}]
        },
    )
    assert r.status_code == 500
    # catalog still reflects the old committed state, not the rolled-back one
    assert spatial(client, **win) == before


def test_window_validation(client):
    wait_ready(client)
    spatial(client, 10, 10, 5, 20, expect=422)        # min_lon > max_lon (would cross 180°)
    spatial(client, 10, 40, 20, 30, expect=422)       # min_lat > max_lat
    spatial(client, -181, 0, 0, 10, expect=422)       # lon out of WGS84 range
    spatial(client, 0, -91, 10, 0, expect=422)        # lat out of range
    spatial(client, 0, 0, 180, 90)                    # full-world window is fine


def test_index_tracks_batches_when_ready(client):
    wait_ready(client)
    post(client, grid_batch())
    win = dict(min_lon=101.0, min_lat=30.0, max_lon=101.5, max_lat=31.0)
    assert [r["id"] for r in spatial(client, **win)["roads"]] == ["RD-EW"]

    # move RD-FAR's points so it enters the window
    post(
        client,
        {"points": [{"id": "PT-4-2", "lon": 101.1, "lat": 30.5},
                    {"id": "PT-5-2", "lon": 101.2, "lat": 30.6}]},
    )
    assert {r["id"] for r in spatial(client, **win)["roads"]} == {"RD-EW", "RD-FAR"}

    # move it back out
    post(
        client,
        {"points": [{"id": "PT-4-2", "lon": 104.0, "lat": 32.0},
                    {"id": "PT-5-2", "lon": 105.0, "lat": 32.0}]},
    )
    assert [r["id"] for r in spatial(client, **win)["roads"]] == ["RD-EW"]

    # delete the road entirely
    post(client, {"delete_roads": ["RD-EW"]})
    assert spatial(client, **win)["roads"] == []

    # delete a referenced point -> RD-NS becomes incomplete and leaves the catalog
    post(client, {"delete_points": ["PT-2-0"]})
    assert client.get("/api/v1/roads/RD-NS").json()["status"] == "incomplete"
    assert "RD-NS" not in {r["id"] for r in spatial(client, 101.5, 30.0, 102.5, 32.0)["roads"]}


# ---------------------------------------------------------------------------
# backfill: gating, pause/resume, no stale overwrite, restart resume
# ---------------------------------------------------------------------------


def test_range_query_rejected_until_catalog_ready(client):
    wait_ready(client)
    r = client.post("/api/v1/index/rebuild", params={"pause": "true"})
    assert r.json()["status"] == "building"

    result = post(client, grid_batch())
    assert result["index_mode"] == "building"

    got = spatial(client, 100.0, 30.0, 103.0, 32.0, expect=409)
    assert got["detail"]["status"] == "building"

    st = client.get("/api/v1/index/status").json()
    assert st["status"] == "building" and st["paused"] is True
    assert st["pending_changes"] >= 3  # the three roads are queued for reconcile

    client.post("/api/v1/index/pause", params={"paused": "false"})
    wait_ready(client)
    st = client.get("/api/v1/index/status").json()
    assert st["pending_changes"] == 0 and st["ready_at"]
    assert {r["id"] for r in spatial(client, 100.0, 30.0, 103.0, 32.0)["roads"]} == {
        "RD-EW",
        "RD-NS",
    }


def test_modify_lines_while_scan_paused(client):
    """The demo scenario: pause the backfill, keep editing, resume, verify."""
    wait_ready(client)
    post(client, grid_batch())
    client.post("/api/v1/index/rebuild", params={"pause": "true"})

    # 1) move RD-FAR into the window while the scan is paused
    post(
        client,
        {"points": [{"id": "PT-4-2", "lon": 101.2, "lat": 30.4},
                    {"id": "PT-5-2", "lon": 101.3, "lat": 30.5}]},
    )
    # 2) create a brand-new road inside the window
    post(client, {"roads": [{"id": "RD-NEW", "point_ids": ["PT-1-0", "PT-1-1"]}]})
    # 3) delete a road that used to cross the window
    post(client, {"delete_roads": ["RD-EW"]})
    # 4) same-id rebuild: delete RD-NS and re-create it elsewhere
    post(client, {"delete_roads": ["RD-NS"]})
    post(client, {"roads": [{"id": "RD-NS", "point_ids": ["PT-4-0", "PT-5-0"]}]})

    st = client.get("/api/v1/index/status").json()
    assert st["status"] == "building" and st["pending_changes"] >= 4
    spatial(client, 100.5, 30.0, 101.5, 31.0, expect=409)

    client.post("/api/v1/index/pause", params={"paused": "false"})
    wait_ready(client)

    got = spatial(client, 100.5, 30.0, 101.5, 31.0)
    ids = {r["id"] for r in got["roads"]}
    assert "RD-FAR" in ids   # moved in while paused
    assert "RD-NEW" in ids   # created while paused
    assert "RD-EW" not in ids  # deleted while paused
    assert "RD-NS" not in ids  # rebuilt with same id outside the window
    # geometry is the NEW committed line, not something a stale scan saw
    far = next(r for r in got["roads"] if r["id"] == "RD-FAR")
    assert far["geometry"]["coordinates"] == [[101.2, 30.4], [101.3, 30.5]]


def test_restart_resumes_backfill_and_never_falsely_ready(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "gis.db"))
    with TestClient(app) as c1:
        wait_ready(c1)
        post(c1, grid_batch())
        c1.post("/api/v1/index/rebuild", params={"pause": "true"})
        st = c1.get("/api/v1/index/status").json()
        assert st["status"] == "building"
    # "restart": a fresh app on the same database file
    with TestClient(app) as c2:
        st = c2.get("/api/v1/index/status").json()
        assert st["status"] == "building" and st["ready"] is False  # not falsely ready
        spatial(c2, 100.0, 30.0, 103.0, 32.0, expect=409)
        c2.post("/api/v1/index/pause", params={"paused": "false"})
        wait_ready(c2)
        assert {r["id"] for r in spatial(c2, 100.0, 30.0, 103.0, 32.0)["roads"]} == {
            "RD-EW",
            "RD-NS",
        }


def test_rebuild_while_ready_reindexes_everything(client):
    wait_ready(client)
    post(client, grid_batch())
    client.post("/api/v1/index/rebuild")  # no pause: rebuild runs through
    wait_ready(client)
    got = spatial(client, 100.0, 30.0, 103.0, 32.0)
    assert {r["id"] for r in got["roads"]} == {"RD-EW", "RD-NS"}


# ---------------------------------------------------------------------------
# backfill internals: stale scan results must not overwrite newer commits
# ---------------------------------------------------------------------------


@pytest.fixture()
def db(tmp_path, monkeypatch):
    from app import config
    from app.db import init_db

    path = str(tmp_path / "gis.db")
    monkeypatch.setattr(config.settings, "db_path", path)
    init_db()
    return path


def _seed_road():
    from app.schemas import BatchIn
    from app import store

    store.execute_batch(
        BatchIn.model_validate(
            {
                "points": [
                    {"id": "A", "lon": 50.0, "lat": 10.0},
                    {"id": "B", "lon": 51.0, "lat": 10.0},
                ],
                "roads": [{"id": "R", "point_ids": ["A", "B"]}],
            }
        )
    )


def test_stale_scan_entry_does_not_overwrite_newer_state(db):
    from app import indexstore, store
    from app.db import read_tx, write_tx
    from app.schemas import BatchIn

    _seed_road()

    # 1. the scan reads R (geometry G1) without any lock
    with read_tx() as conn:
        row = conn.execute("SELECT id, geometry FROM roads WHERE id = 'R'").fetchone()
    stale = indexstore.ScanEntry(
        road_id="R",
        existed=True,
        geometry_text=row["geometry"],
        bbox=(50.0, 10.0, 51.0, 10.0),
    )

    # 2. concurrently a batch moves the road far away (marks it dirty)
    store.execute_batch(
        BatchIn.model_validate({"points": [{"id": "A", "lon": 150.0, "lat": 60.0},
                                           {"id": "B", "lon": 151.0, "lat": 60.0}]})
    )

    # 3. the stale scan result must be skipped, not written
    with write_tx() as conn:
        applied = indexstore._apply_entries(conn, [stale])
    assert applied == []
    with read_tx() as conn:
        assert indexstore.candidate_road_ids(conn, (49, 9, 52, 11)) == []

    # 4. the reconcile pass indexes the NEW committed geometry
    while indexstore.reconcile_step():
        pass
    with read_tx() as conn:
        assert indexstore.candidate_road_ids(conn, (149, 59, 152, 61)) == ["R"]
        assert indexstore.candidate_road_ids(conn, (49, 9, 52, 11)) == []
        assert conn.execute("SELECT count(*) FROM index_dirty").fetchone()[0] == 0


def test_deleted_road_during_scan_leaves_no_entry(db):
    from app import indexstore, store
    from app.db import read_tx, write_tx
    from app.schemas import BatchIn

    _seed_road()
    with read_tx() as conn:
        row = conn.execute("SELECT id, geometry FROM roads WHERE id = 'R'").fetchone()
    stale = indexstore.ScanEntry(
        road_id="R", existed=True, geometry_text=row["geometry"],
        bbox=(50.0, 10.0, 51.0, 10.0),
    )
    store.execute_batch(BatchIn.model_validate({"delete_roads": ["R"]}))
    with write_tx() as conn:
        applied = indexstore._apply_entries(conn, [stale])
    assert applied == []
    while indexstore.reconcile_step():
        pass
    with read_tx() as conn:
        assert indexstore.candidate_road_ids(conn, (0, 0, 180, 80)) == []
        assert conn.execute("SELECT count(*) FROM index_dirty").fetchone()[0] == 0


def test_road_created_behind_cursor_is_not_missed(db):
    from app import indexstore, store
    from app.db import read_tx
    from app.schemas import BatchIn

    _seed_road()  # road R; cursor will move past it
    # simulate the cursor having already passed id 'R'
    from app.db import write_tx

    with write_tx() as conn:
        conn.execute("UPDATE index_meta SET scan_cursor = 'ZZZ' WHERE id = 1")
    # a new road with id < cursor appears while building
    store.execute_batch(
        BatchIn.model_validate(
            {
                "points": [{"id": "C", "lon": 60.0, "lat": 20.0},
                           {"id": "D", "lon": 61.0, "lat": 20.0}],
                "roads": [{"id": "A-NEW", "point_ids": ["C", "D"]}],
            }
        )
    )
    # scan finds nothing beyond the cursor...
    assert indexstore.scan_step() is False
    # ...but the dirty queue holds both changed roads and reconcile indexes them
    while indexstore.reconcile_step():
        pass
    assert indexstore.try_finish() is True
    with read_tx() as conn:
        assert indexstore.candidate_road_ids(conn, (59, 19, 62, 21)) == ["A-NEW"]
        assert indexstore.candidate_road_ids(conn, (49, 9, 52, 11)) == ["R"]
        assert indexstore.catalog_status(conn) == "ready"


def test_try_finish_refuses_while_changes_pending(db):
    from app import indexstore

    _seed_road()  # building mode -> the road is queued as dirty
    assert indexstore.try_finish() is False
    with indexstore.read_tx() as conn:
        assert indexstore.catalog_status(conn) == "building"


def test_rtree_rows_are_persisted_in_the_database_file(db):
    _seed_road()
    from app import indexstore

    while indexstore.scan_step() or indexstore.reconcile_step():
        pass
    assert indexstore.try_finish()
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            "SELECT m.road_id, r.min_lon, r.max_lon, r.min_lat, r.max_lat "
            "FROM road_rtree r JOIN road_index_map m ON m.entry_id = r.entry_id"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [("R", 50.0, 51.0, 10.0, 10.0)]


# ---------------------------------------------------------------------------
# upgrade path: historical data is backfilled in the background
# ---------------------------------------------------------------------------


def test_historical_data_is_backfilled_after_upgrade(tmp_path, monkeypatch):
    """A database written by the old version (no catalog tables) is indexed
    by the background backfill; range queries work once it finishes."""
    from app import config

    db_file = tmp_path / "gis.db"
    # simulate the pre-spatial schema with committed data
    conn = sqlite3.connect(db_file)
    conn.executescript(
        """
        CREATE TABLE points (id TEXT PRIMARY KEY, lon REAL NOT NULL, lat REAL NOT NULL);
        CREATE TABLE roads (
            id TEXT PRIMARY KEY, geometry TEXT,
            missing_point_ids TEXT NOT NULL DEFAULT '[]',
            revision INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        CREATE TABLE road_points (
            road_id TEXT NOT NULL, seq INTEGER NOT NULL, point_id TEXT NOT NULL,
            PRIMARY KEY (road_id, seq)
        );
        CREATE TABLE store_meta (id INTEGER PRIMARY KEY CHECK (id = 1),
                                 generation INTEGER NOT NULL);
        INSERT INTO store_meta VALUES (1, 7);
        INSERT INTO points VALUES ('A', 50.0, 10.0), ('B', 51.0, 10.0);
        INSERT INTO roads VALUES ('OLD-R',
            '{"type":"LineString","coordinates":[[50.0,10.0],[51.0,10.0]]}',
            '[]', 3, '2026-01-01T00:00:00.000Z');
        INSERT INTO road_points VALUES ('OLD-R', 0, 'A'), ('OLD-R', 1, 'B');
        """
    )
    conn.close()

    monkeypatch.setattr(config.settings, "db_path", str(db_file))
    with TestClient(app) as c:
        st = wait_ready(c)  # backfill picks up the historical road
        assert st["total_roads"] == 1 and st["scanned"] >= 1
        got = spatial(c, 49.0, 9.0, 52.0, 11.0)
        assert [r["id"] for r in got["roads"]] == ["OLD-R"]
        assert got["roads"][0]["geometry"]["coordinates"] == [[50.0, 10.0], [51.0, 10.0]]
        # and new batches keep working in ready mode right away
        result = post(c, {"points": [{"id": "A", "lon": 49.0, "lat": 9.0}]})
        assert result["index_mode"] == "ready"
        got = spatial(c, 48.0, 8.0, 49.5, 9.5)
        assert [r["id"] for r in got["roads"]] == ["OLD-R"]
