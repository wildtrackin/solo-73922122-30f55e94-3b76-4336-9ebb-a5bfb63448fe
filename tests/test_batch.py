"""Functional tests: assembly, incompleteness, atomic batches, rollback."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # the settings object is created at import time, so patch the live setting
    from app import config

    monkeypatch.setattr(config.settings, "db_path", str(tmp_path / "gis.db"))
    with TestClient(app) as c:
        yield c


def post(client, body, **params):
    return client.post("/api/v1/batches", json=body, params=params)


P_ABC = [
    {"id": "A", "lon": 120.1000, "lat": 30.2000},
    {"id": "B", "lon": 120.2000, "lat": 30.3000},
    {"id": "C", "lon": 120.3000, "lat": 30.4000},
]


def test_assembly_and_persisted_geometry(client):
    r = post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B", "C"]}]})
    assert r.status_code == 201, r.text
    road = r.json()["roads"][0]
    assert road["status"] == "ok"
    assert road["geometry"] == {
        "type": "LineString",
        "coordinates": [[120.1, 30.2], [120.2, 30.3], [120.3, 30.4]],
    }
    assert road["missing_point_ids"] == []
    assert client.get("/api/v1/roads/R1").json()["geometry"] == road["geometry"]


def test_geometry_is_persisted_not_assembled_on_read(client, tmp_path):
    post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B", "C"]}]})
    import sqlite3

    conn = sqlite3.connect(tmp_path / "gis.db")
    stored = conn.execute("SELECT geometry FROM roads WHERE id = 'R1'").fetchone()[0]
    conn.close()
    assert stored and "LineString" in stored


def test_missing_point_yields_no_geometry_and_reports_ids(client):
    post(
        client,
        {
            "points": P_ABC[:2],
            "roads": [{"id": "R1", "point_ids": ["A", "B", "C"]}],
        },
    )
    road = client.get("/api/v1/roads/R1").json()
    assert road["status"] == "incomplete"
    assert road["geometry"] is None
    assert road["missing_point_ids"] == ["C"]

    # re-supplying restores the line with no extra call
    post(client, {"points": [P_ABC[2]]})
    road = client.get("/api/v1/roads/R1").json()
    assert road["status"] == "ok"
    assert road["missing_point_ids"] == []
    assert road["geometry"]["coordinates"][2] == [120.3, 30.4]


def test_deleting_referenced_point_invalidates_all_roads(client):
    post(
        client,
        {
            "points": P_ABC,
            "roads": [
                {"id": "R1", "point_ids": ["A", "B"]},
                {"id": "R2", "point_ids": ["B", "C"]},
                {"id": "R3", "point_ids": ["A", "C"]},
            ],
        },
    )
    r = post(client, {"delete_points": ["B"]})
    body = r.json()
    assert sorted(body["affected_road_ids"]) == ["R1", "R2"]
    states = {x["id"]: x for x in body["roads"]}
    assert states["R1"]["status"] == "incomplete" and states["R1"]["missing_point_ids"] == ["B"]
    assert states["R2"]["status"] == "incomplete"
    assert client.get("/api/v1/roads/R3").json()["status"] == "ok"
    assert client.get("/api/v1/points/B").status_code == 404


def test_correcting_point_updates_every_road(client):
    post(
        client,
        {
            "points": P_ABC,
            "roads": [
                {"id": "R1", "point_ids": ["A", "B"]},
                {"id": "R2", "point_ids": ["B", "C"]},
            ],
        },
    )
    r = post(client, {"points": [{"id": "B", "lon": 120.2500, "lat": 30.3500}]})
    body = r.json()
    assert sorted(body["affected_road_ids"]) == ["R1", "R2"]
    for road in body["roads"]:
        assert [120.25, 30.35] in road["geometry"]["coordinates"]
        assert road["revision"] == 2
    # untouched road keeps its revision
    assert client.get("/api/v1/roads/R1").json()["revision"] == 2


def test_changing_references_rewrites_line(client):
    post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B"]}]})
    r = post(client, {"roads": [{"id": "R1", "point_ids": ["C", "A", "B"]}]})
    road = r.json()["roads"][0]
    assert road["point_ids"] == ["C", "A", "B"]
    assert road["geometry"]["coordinates"] == [[120.3, 30.4], [120.1, 30.2], [120.2, 30.3]]


def test_unrelated_batch_does_not_touch_other_roads(client):
    post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B"]}]})
    before = client.get("/api/v1/roads/R1").json()
    r = post(client, {"points": [{"id": "D", "lon": 121.0, "lat": 31.0}]})
    assert r.json()["affected_road_ids"] == []
    after = client.get("/api/v1/roads/R1").json()
    assert after == before


def test_failed_batch_rolls_back_everything(client):
    post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B"]}]})
    before_state = client.get("/api/v1/state").json()

    r = post(
        client,
        {
            "points": [
                {"id": "B", "lon": 9.9, "lat": 9.9},
                {"id": "Z", "lon": 9.8, "lat": 9.8},
            ],
            "roads": [{"id": "R1", "point_ids": ["Z", "B"]}],
        },
        fail=True,
    )
    assert r.status_code == 500
    assert client.get("/api/v1/state").json() == before_state
    assert client.get("/api/v1/points/Z").status_code == 404


def test_internal_error_midbatch_is_atomic(client, monkeypatch):
    """An exception raised by the store after partial writes must not leak."""
    post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B"]}]})
    before = client.get("/api/v1/state").json()

    from app import store

    real_apply = store._apply

    def boom(conn, plan, stamp, inject):
        real_apply(conn, plan, stamp, False)
        raise RuntimeError("crash after writes")

    monkeypatch.setattr(store, "_apply", boom)
    with pytest.raises(RuntimeError, match="crash after writes"):
        post(client, {"points": [{"id": "Q", "lon": 5, "lat": 5}]})
    assert client.get("/api/v1/state").json() == before


def test_validation_rejects_bad_roads(client):
    r = post(client, {"points": P_ABC[:2], "roads": [{"id": "R", "point_ids": ["A"]}]})
    assert r.status_code == 422
    r = post(client, {"points": P_ABC[:2], "roads": [{"id": "R", "point_ids": ["A", "A"]}]})
    assert r.status_code == 422
    r = post(client, {"points": [{"id": "A", "lon": 200, "lat": 0}]})
    assert r.status_code == 422


def test_batch_with_disjoint_delete_and_upsert_rejected(client):
    r = post(client, {"points": [P_ABC[0]], "delete_points": ["A"]})
    assert r.status_code == 422


def test_blank_point_id_rejected(client):
    r = post(client, {"points": [{"id": "  ", "lon": 1, "lat": 1}]})
    assert r.status_code == 422


def test_delete_road_removes_refs(client):
    post(client, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B"]}]})
    r = post(client, {"delete_roads": ["R1"]})
    assert r.json()["roads_deleted"] == 1
    assert client.get("/api/v1/roads/R1").status_code == 404
    assert client.get("/api/v1/points/A").json()["id"] == "A"
    # a new road can reuse the id
    r = post(client, {"roads": [{"id": "R1", "point_ids": ["C", "A"]}]})
    assert r.json()["roads"][0]["revision"] == 1


def test_state_payload_is_single_snapshot(client):
    post(
        client,
        {
            "points": P_ABC,
            "roads": [{"id": "R1", "point_ids": ["A", "B"]}],
        },
    )
    state = client.get("/api/v1/state").json()
    coords = {p["id"]: [p["lon"], p["lat"]] for p in state["points"]}
    for road in state["roads"]:
        for pid, pos in zip(road["point_ids"], road["geometry"]["coordinates"]):
            assert pos == coords[pid]


def test_restart_preserves_data(tmp_path, monkeypatch):
    from app import config

    db = str(tmp_path / "gis.db")
    monkeypatch.setattr(config.settings, "db_path", db)
    with TestClient(app) as c1:
        post(c1, {"points": P_ABC, "roads": [{"id": "R1", "point_ids": ["A", "B", "C"]}]})
        expected = c1.get("/api/v1/state").json()
    with TestClient(app) as c2:
        assert c2.get("/api/v1/state").json() == expected
