"""Black-box tests against TWO real API processes sharing one SQLite file."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def two_apis(db_path):
    procs = []
    ports = []
    env = dict(os.environ, GIS_DB_PATH=str(db_path), GIS_BUSY_TIMEOUT_S="30")
    stop_file = str(db_path) + ".stop"
    try:
        for _ in range(2):
            port = _free_port()
            ports.append(port)
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        os.path.join(os.path.dirname(__file__), "server_runner.py"),
                        str(port),
                        stop_file,
                    ],
                    cwd=ROOT,
                    env=env,
                )
            )
        bases = [f"http://127.0.0.1:{p}" for p in ports]
        deadline = time.time() + 30
        for base in bases:
            while True:
                try:
                    if httpx.get(base + "/health", timeout=1).status_code == 200:
                        break
                except Exception:
                    if time.time() > deadline:
                        raise RuntimeError(f"{base} did not start")
                    time.sleep(0.1)
        yield bases
    finally:
        open(stop_file, "w").close()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:  # pragma: no cover
                p.kill()
                p.wait(timeout=5)


@pytest.fixture(scope="module")
def apis(tmp_path_factory):
    db = tmp_path_factory.mktemp("shared") / "gis.db"
    with two_apis(db) as bases:
        yield bases


PTS = [
    {"id": "P1", "lon": 116.100, "lat": 39.800},
    {"id": "P2", "lon": 116.200, "lat": 39.850},
    {"id": "P3", "lon": 116.300, "lat": 39.900},
]


def test_both_processes_share_the_same_database(apis):
    a, b = apis
    r = httpx.post(
        a + "/api/v1/batches",
        json={"points": PTS, "roads": [{"id": "R_SHARED", "point_ids": ["P1", "P2", "P3"]}]},
        timeout=10,
    )
    assert r.status_code == 201, r.text
    got = httpx.get(b + "/api/v1/roads/R_SHARED", timeout=10).json()
    assert got["geometry"]["coordinates"] == [[116.1, 39.8], [116.2, 39.85], [116.3, 39.9]]

    r = httpx.post(
        b + "/api/v1/batches",
        json={"points": [{"id": "P1", "lon": 116.150, "lat": 39.810}]},
        timeout=10,
    )
    assert r.status_code == 201
    assert r.json()["affected_road_ids"] == ["R_SHARED"]
    got = httpx.get(a + "/api/v1/roads/R_SHARED", timeout=10).json()
    assert got["geometry"]["coordinates"][0] == [116.15, 39.81]


def test_paused_assembly_does_not_hold_write_lock(apis):
    """A slow assembly on A must not block an ingest on B."""
    a, b = apis
    result = {}

    def slow_batch():
        result["r"] = httpx.post(
            a + "/api/v1/batches",
            params={"pause_ms": 1500},
            json={"points": [{"id": "P4", "lon": 116.400, "lat": 39.950}]},
            timeout=20,
        )

    t = threading.Thread(target=slow_batch)
    t.start()
    time.sleep(0.15)  # let the paused batch enter its assembly pause

    t0 = time.time()
    r = httpx.post(
        b + "/api/v1/batches",
        json={"roads": [{"id": "R_CONCURRENT", "point_ids": ["P4", "P2"]}]},
        timeout=10,
    )
    elapsed = time.time() - t0
    assert r.status_code == 201, r.text
    assert elapsed < 1.2, f"ingest was blocked by the paused batch ({elapsed:.2f}s)"

    t.join(timeout=20)
    assert result["r"].status_code == 201
    # R_CONCURRENT was created while P4 did not exist yet at A's snapshot
    road = httpx.get(a + "/api/v1/roads/R_CONCURRENT", timeout=10).json()
    assert road["status"] == "ok", road
    assert road["geometry"]["coordinates"][0] == [116.4, 39.95]

    # ... and A's batch, which committed later, still saw the new road
    assert "R_CONCURRENT" in result["r"].json()["affected_road_ids"]


def test_concurrent_reference_and_correction_no_lost_update(apis):
    """Race point corrections against road creation/repointing on both APIs."""
    a, b = apis
    httpx.post(
        a + "/api/v1/batches",
        json={"points": [{"id": f"Q{i}", "lon": 1.0 + i, "lat": 2.0 + i} for i in range(4)]},
        timeout=10,
    )

    errors: list[str] = []
    lock = threading.Lock()

    def worker(base, i):
        try:
            if i % 3 == 0:
                # correct a referenced point
                r = httpx.post(
                    base + "/api/v1/batches",
                    params={"pause_ms": 0},
                    json={"points": [{"id": "Q0", "lon": 1.0 + i / 100.0, "lat": 2.0}]},
                    timeout=30,
                )
            elif i % 3 == 1:
                # add / repoint a road that references Q0
                r = httpx.post(
                    base + "/api/v1/batches",
                    json={
                        "roads": [
                            {"id": f"RQ{i}", "point_ids": ["Q0", f"Q{1 + i % 3}"]}
                        ]
                    },
                    timeout=30,
                )
            else:
                # touch an unrelated point + delete a referenced one and restore
                r = httpx.post(
                    base + "/api/v1/batches",
                    json={"points": [{"id": f"Q{i % 4}", "lon": 1.0 + i, "lat": 2.0 + i}]},
                    timeout=30,
                )
            if r.status_code != 201:
                raise AssertionError(f"{r.status_code} {r.text}")
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(f"{base}: {exc}")

    threads = [
        threading.Thread(target=worker, args=(base, i))
        for i, base in enumerate([a, b] * 8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    state = httpx.get(a + "/api/v1/state", timeout=10).json()
    points = {p["id"]: [p["lon"], p["lat"]] for p in state["points"]}
    for road in state["roads"]:
        assert road["status"] == "ok", road
        for pid, pos in zip(road["point_ids"], road["geometry"]["coordinates"]):
            assert pos == points[pid], f"{road['id']}: stale coordinate for {pid}"

    # every road referencing Q0 must carry the FINAL committed coordinate
    final_q0 = points["Q0"]
    for road in state["roads"]:
        if "Q0" in road["point_ids"]:
            idx = road["point_ids"].index("Q0")
            assert road["geometry"]["coordinates"][idx] == final_q0


def test_state_endpoint_is_consistent_under_writes(apis):
    a, b = apis
    stop = threading.Event()
    failures: list[str] = []

    def mutate():
        n = 0
        while not stop.is_set():
            try:
                r = httpx.post(
                    a + "/api/v1/batches",
                    json={
                        "points": [{"id": "Q1", "lon": 10.0 + (n % 50) / 100.0, "lat": 20.0}],
                        "roads": [{"id": "RQ1", "point_ids": ["Q0", "Q1"]}],
                    },
                    timeout=30,
                )
                if r.status_code != 201:
                    failures.append(f"write {r.status_code} {r.text}")
            except Exception as exc:  # pragma: no cover
                failures.append(str(exc))
            n += 1

    t = threading.Thread(target=mutate)
    t.start()
    try:
        for _ in range(40):
            state = httpx.get(b + "/api/v1/state", timeout=10).json()
            coords = {p["id"]: [p["lon"], p["lat"]] for p in state["points"]}
            for road in state["roads"]:
                if road["geometry"] is None:
                    continue
                for pid, pos in zip(road["point_ids"], road["geometry"]["coordinates"]):
                    assert pos == coords[pid], f"mixed snapshot for {road['id']}/{pid}"
    finally:
        stop.set()
        t.join(timeout=20)
    assert not failures, failures
