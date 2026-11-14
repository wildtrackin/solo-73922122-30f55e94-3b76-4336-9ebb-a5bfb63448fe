"""Scenario sample used by the docker compose demo.

Runs entirely against the public HTTP API of both processes, so it doubles as
a black-box acceptance check of the delivered environment:

1. shared points + roads are ingested through API A;
2. a referenced point goes missing -> no partial line, missing ids reported;
3. the point is re-supplied -> the line restores automatically;
4. a correction of one point fans out to every road referencing it;
5. a deliberately paused batch on A (assembly outside any write lock) runs
   while B keeps ingesting and repointing roads;
6. a failed batch rolls back; the final state is then re-read from B, and
   snapshots are validated against the persisted values;
7. the spatial catalog serves viewport range queries (roads piercing the
   window with all vertices outside are hit; incomplete roads are excluded);
8. the catalog is rebuilt with the scan paused: lines keep changing via the
   other process, range queries are rejected (409) until the backfill
   finishes, and the resumed scan indexes the NEW committed lines.

Run locally::

    python -m examples.run_demo --base-a http://localhost:8001 \\
                                --base-b http://localhost:8002
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time

import httpx

POINTS = {
    "PT-001": (116.397428, 39.909230),
    "PT-002": (116.407428, 39.913000),
    "PT-003": (116.417428, 39.918000),
    "PT-004": (116.427428, 39.921000),
    "PT-005": (116.437428, 39.926000),
    "PT-006": (116.447428, 39.930000),
}


def pretty(title: str, payload) -> None:
    print(f"\n=== {title} " + "=" * max(0, 66 - len(title)))
    print(json.dumps(payload, ensure_ascii=False, indent=2))


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.http = httpx.Client(base_url=self.base, timeout=60.0)

    def batch(self, body: dict, **params):
        r = self.http.post("/api/v1/batches", json=body, params=params)
        r.raise_for_status()
        return r.json()

    def road(self, road_id: str) -> dict:
        r = self.http.get(f"/api/v1/roads/{road_id}")
        r.raise_for_status()
        return r.json()

    def state(self) -> dict:
        r = self.http.get("/api/v1/state")
        r.raise_for_status()
        return r.json()

    def spatial(self, min_lon, min_lat, max_lon, max_lat, expect=200) -> dict:
        r = self.http.get(
            "/api/v1/spatial/roads",
            params={
                "min_lon": min_lon,
                "min_lat": min_lat,
                "max_lon": max_lon,
                "max_lat": max_lat,
            },
        )
        assert r.status_code == expect, f"{r.status_code}: {r.text}"
        return r.json()

    def index_status(self) -> dict:
        r = self.http.get("/api/v1/index/status")
        r.raise_for_status()
        return r.json()

    def index_pause(self, paused: bool) -> dict:
        r = self.http.post("/api/v1/index/pause", params={"paused": str(paused).lower()})
        r.raise_for_status()
        return r.json()

    def index_rebuild(self, pause: bool) -> dict:
        r = self.http.post("/api/v1/index/rebuild", params={"pause": str(pause).lower()})
        r.raise_for_status()
        return r.json()


def wait_ready(api: Api, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    st = None
    while time.time() < deadline:
        st = api.index_status()
        if st["ready"]:
            return st
        time.sleep(0.2)
    raise SystemExit(f"spatial catalog did not become ready: {st}")


def _run(args) -> None:
    a = Api(args.base_a)
    b = Api(args.base_b)
    print(f"API A = {a.base}\nAPI B = {b.base}  (both share /data/gis.db)")

    # -- 1 --------------------------------------------------------------
    points = [{"id": pid, "lon": lon, "lat": lat} for pid, (lon, lat) in POINTS.items()]
    roads = [
        {"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]},
        {"id": "RD-102", "point_ids": ["PT-003", "PT-004", "PT-005"]},
        {"id": "RD-103", "point_ids": ["PT-005", "PT-006"]},
    ]
    batch = a.batch({"points": points, "roads": roads})
    pretty("1. 批量入库（API A）: 测点 + 路段，线形已持久化", batch)

    # -- 2 --------------------------------------------------------------
    missing = a.batch({"delete_points": ["PT-004"]})
    pretty("2. 删除被引用测点 PT-004：不输出残线，列明缺失 ID", missing)
    assert missing["roads"][0]["geometry"] is None
    assert missing["roads"][0]["missing_point_ids"] == ["PT-004"]

    # -- 3 --------------------------------------------------------------
    restored = a.batch({"points": [{"id": "PT-004", "lon": POINTS["PT-004"][0], "lat": POINTS["PT-004"][1]}]})
    pretty("3. 补点后自动恢复线形（无需额外调用）", restored)
    assert restored["roads"][0]["status"] == "ok"

    # -- 4 --------------------------------------------------------------
    corrected = b.batch(
        {"points": [{"id": "PT-003", "lon": 116.417800, "lat": 39.919200}]}
    )
    pretty("4. 更正 PT-003（改由 API B 提交）：两条引用它的路段同时生效", corrected)
    # RD-101/RD-102 must be fan-out updated; later demo steps add RD-PAUSED,
    # which also references PT-003, so re-runs may list it as well
    assert {"RD-101", "RD-102"} <= set(corrected["affected_road_ids"])

    # -- 5 --------------------------------------------------------------
    # Paused batch on A: it reports affected roads based on the snapshot it
    # plans from, and commits only after B has already written.  Its commit
    # re-validates the snapshot, so it either waits or recomputes - but it can
    # never overwrite B's newer coordinates with its old ones.
    print("\n=== 5. 可控暂停下并发改线 " + "=" * 44)
    results: dict[str, object] = {}

    def paused_batch():
        t0 = time.time()
        results["a"] = a.batch(
            {
                "points": [{"id": "PT-006", "lon": 116.447900, "lat": 39.930400}],
                "roads": [{"id": "RD-104", "point_ids": ["PT-006", "PT-001"]}],
            },
            pause_ms=2000,
        )
        results["a_elapsed"] = round(time.time() - t0, 2)

    t = threading.Thread(target=paused_batch)
    t.start()
    time.sleep(0.3)  # the paused batch is now in its 2s assembly pause

    t0 = time.time()
    b_result = b.batch(
        {
            "points": [{"id": "PT-006", "lon": 116.447950, "lat": 39.930900}],
            "roads": [{"id": "RD-105", "point_ids": ["PT-006", "PT-002"]}],
        }
    )
    b_elapsed = round(time.time() - t0, 2)
    t.join()
    pretty(
        f"B 在 A 暂停期间完成入库，用时 {b_elapsed}s（未等待 A 的写锁）",
        b_result,
    )
    pretty(f"A 的暂停批次完成后返回，总用时 {results['a_elapsed']}s", results["a"])

    final = b.state()
    pretty("并发结束后（API B 读取）: 两条引用 PT-006 的路段都采用最后一次提交的值", {
        "PT-006": next(p for p in final["points"] if p["id"] == "PT-006"),
        "RD-104": b.road("RD-104"),
        "RD-105": b.road("RD-105"),
    })

    # -- 6 --------------------------------------------------------------
    before = b.state()
    try:
        a.batch(
            {
                "points": [
                    {"id": "PT-001", "lon": 1.0, "lat": 2.0},
                    {"id": "PT-999", "lon": 1.0, "lat": 3.0},
                ],
                "roads": [{"id": "RD-101", "point_ids": ["PT-999", "PT-002"]}],
            },
            fail=True,
        )
        raise SystemExit("failure injection did not fail")
    except httpx.HTTPStatusError as exc:
        pretty("6. 批内失败（写后注入异常）：整体回滚", exc.response.json())

    after = b.state()
    assert after == before, "failed batch leaked changes"
    pretty("回滚后状态与失败前完全一致", {"generation": after["generation"]})

    # consistency check on the whole snapshot
    coords = {p["id"]: [p["lon"], p["lat"]] for p in after["points"]}
    for road in after["roads"]:
        for pid, pos in zip(road["point_ids"], road["geometry"]["coordinates"]):
            assert pos == coords[pid], f"{road['id']} geometry does not match point {pid}"

    pretty(
        "最终一致性校验通过：每条线形的每个坐标都等于同一快照里的测点原值",
        {
            "generation": after["generation"],
            "points": len(after["points"]),
            "roads": {
                r["id"]: ("ok" if r["status"] == "ok" else f"缺 {r['missing_point_ids']}")
                for r in after["roads"]
            },
        },
    )

    # -- 7 --------------------------------------------------------------
    st = wait_ready(a)
    pretty("7. 空间目录后台补建完成（GET /api/v1/index/status）", st)

    # a viewport window over the PT-001..PT-003 corridor
    win = a.spatial(116.390, 39.905, 116.420, 39.920)
    pretty(
        "范围查询：视窗 [116.390,39.905 ~ 116.420,39.920] 内命中的完整路段",
        {"total": win["total"], "ids": [r["id"] for r in win["roads"]]},
    )
    assert "RD-101" in {r["id"] for r in win["roads"]}

    # a road that pierces the window with ALL vertices outside must still hit
    a.batch(
        {
            "points": [
                {"id": "PT-X1", "lon": 116.390, "lat": 39.900},
                {"id": "PT-X2", "lon": 116.460, "lat": 39.940},
            ],
            "roads": [{"id": "RD-CROSS", "point_ids": ["PT-X1", "PT-X2"]}],
        }
    )
    cross = a.spatial(116.410, 39.912, 116.430, 39.922)
    pretty(
        "RD-CROSS 斜穿小视窗、两个端点都在窗外：依然命中（线段穿越判定）",
        {"window": cross["window"], "ids": [r["id"] for r in cross["roads"]]},
    )
    assert "RD-CROSS" in {r["id"] for r in cross["roads"]}

    # incomplete roads never leave the catalog
    a.batch({"delete_points": ["PT-X2"]})
    gone = a.spatial(116.410, 39.912, 116.430, 39.922)
    assert "RD-CROSS" not in {r["id"] for r in gone["roads"]}
    pretty("删除 PT-X2 后 RD-CROSS 转为 incomplete：范围查询立即排除", {"ids": [r["id"] for r in gone["roads"]]})
    a.batch({"points": [{"id": "PT-X2", "lon": 116.460, "lat": 39.940}]})
    back = a.spatial(116.410, 39.912, 116.430, 39.922)
    assert "RD-CROSS" in {r["id"] for r in back["roads"]}
    print("    补回 PT-X2 后 RD-CROSS 自动恢复并重新进入目录。")

    # -- 8 --------------------------------------------------------------
    print("\n=== 8. 重建目录并暂停扫描：演示“扫描暂停时改线” " + "=" * 20)
    st = a.index_rebuild(pause=True)
    pretty("POST /api/v1/index/rebuild?pause=true：目录回到 building 并暂停扫描", st)

    blocked = a.spatial(116.390, 39.905, 116.460, 39.940, expect=409)
    pretty("目录未就绪：范围查询被拒绝（409）", blocked)

    # the other process keeps editing lines while the scan is paused
    moved = b.batch(
        {
            "points": [{"id": "PT-003", "lon": 116.418500, "lat": 39.920500}],
            "roads": [{"id": "RD-PAUSED", "point_ids": ["PT-003", "PT-X1"]}],
        }
    )
    pretty(
        "暂停期间 B 改线（PT-003 移动）并新建 RD-PAUSED：入库照常，"
        f"变更进入补建队列（index_mode={moved['index_mode']}）",
        {"affected_road_ids": moved["affected_road_ids"], "index_mode": moved["index_mode"]},
    )
    st = a.index_status()
    pretty("进度查询：扫描暂停中，变更待复核", st)
    assert st["status"] == "building" and st["pending_changes"] >= 2

    a.index_pause(False)
    st = wait_ready(a)
    pretty("恢复扫描后补建自动接续直至就绪", st)

    hit = b.spatial(116.415, 39.918, 116.425, 39.925)
    ids = {r["id"] for r in hit["roads"]}
    pretty(
        "就绪后范围查询：改过的线形按新坐标命中，新建路段也在目录中",
        {"window": hit["window"], "ids": sorted(ids)},
    )
    assert "RD-101" in ids and "RD-PAUSED" in ids
    rd101 = next(r for r in hit["roads"] if r["id"] == "RD-101")
    assert [116.4185, 39.9205] in rd101["geometry"]["coordinates"], "旧扫描覆盖了新线形！"

    print("\n演示完成。重启 api-a / api-b 后上述数据与空间目录仍在（SQLite WAL 持久化）。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-a", default="http://localhost:8001")
    ap.add_argument("--base-b", default="http://localhost:8002")
    _run(ap.parse_args(argv))
    return 0


if __name__ == "__main__":
    sys.exit(main())
