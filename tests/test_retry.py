"""Deterministic test of the optimistic commit guard.

Simulates "another API process committed while this batch was assembling" by
bumping the store generation from a second connection between the snapshot and
the commit.  The stale plan must be discarded, not written.
"""
from __future__ import annotations

import sqlite3

import pytest

from app import config, store
from app.db import init_db
from app.schemas import BatchIn


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "gis.db")
    monkeypatch.setattr(config.settings, "db_path", path)
    init_db()
    return path


def _batch():
    return BatchIn.model_validate(
        {
            "points": [
                {"id": "A", "lon": 1.0, "lat": 1.0},
                {"id": "B", "lon": 2.0, "lat": 2.0},
            ],
            "roads": [{"id": "R", "point_ids": ["A", "B"]}],
        }
    )


def test_stale_plan_is_recomputed_not_written(db, monkeypatch):
    # seed a road that references A (which will move concurrently)
    store.execute_batch(_batch())

    real_build = store._build_plan
    state = {"calls": 0}

    def build_with_concurrent_write(batch, snap):
        state["calls"] += 1
        if state["calls"] == 1:
            # another process commits A = (9,9) while we are still planning
            conn = sqlite3.connect(db, isolation_level=None)
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE points SET lon = 9, lat = 9 WHERE id = 'A'")
            conn.execute("UPDATE store_meta SET generation = generation + 1")
            conn.execute("COMMIT")
            conn.close()
        return real_build(batch, snap)

    monkeypatch.setattr(store, "_build_plan", build_with_concurrent_write)

    result, attempts = store.execute_batch(
        BatchIn.model_validate({"roads": [{"id": "R", "point_ids": ["A", "B"]}]})
    )
    assert attempts >= 2, "stale plan was not retried"
    # the committed geometry must use the OTHER process's coordinate, not the
    # coordinate this batch saw in its first snapshot
    assert result.roads[0].geometry["coordinates"][0] == [9.0, 9.0]
    assert result.roads[0].point_ids == ["A", "B"]


def test_failed_commit_does_not_bump_generation(db):
    conn = sqlite3.connect(db, isolation_level=None)
    before = conn.execute("SELECT generation FROM store_meta").fetchone()[0]
    conn.close()

    with pytest.raises(store.DemoFailure):
        store.execute_batch(
            BatchIn.model_validate({"points": [{"id": "C", "lon": 3, "lat": 3}]}),
            inject_failure=True,
        )

    conn = sqlite3.connect(db, isolation_level=None)
    after = conn.execute("SELECT generation FROM store_meta").fetchone()[0]
    has_c = conn.execute("SELECT count(*) FROM points WHERE id='C'").fetchone()[0]
    conn.close()
    assert after == before
    assert has_c == 0
