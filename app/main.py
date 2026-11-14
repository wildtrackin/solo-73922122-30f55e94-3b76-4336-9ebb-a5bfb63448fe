"""FastAPI application: batch ingest + persisted-geometry queries + spatial range query."""
from __future__ import annotations

import sqlite3
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from . import indexstore, store
from .config import settings
from .db import init_db, read_tx
from .schemas import (
    BatchIn,
    BatchResult,
    HealthResponse,
    IndexStatusResponse,
    PointIn,
    PointsResponse,
    RoadOut,
    RoadsResponse,
    SpatialRoadsResponse,
    SpatialWindow,
    StateResponse,
)
from .spatial import linestring_intersects_rect

DESCRIPTION = """
测绘队共享测点 -> 路段线形加工服务，带持久化空间目录的地图视窗范围查询。

* `POST /api/v1/batches` 一次事务内完成测点/路段的**批量新增、更正、删除**。
* 线形在提交时按路段引用的测点顺序组装并**持久保存**为 GeoJSON
  `LineString`；查询接口只读已持久化的表，不现场组线。
* 任一被引用测点缺失时**不输出残线**，在 `missing_point_ids` 里列明缺失
  的测点 ID；补齐测点后下一次批次自动恢复线形。
* `GET /api/v1/spatial/roads` 按**不跨 180° 经线**的 WGS84 闭矩形查询相交
  路段：先用持久化的 SQLite RTree 空间目录筛候选，再对已持久化线形做精确
  相交判定（经纬度平面）。道路穿过窗口但顶点均在窗外也会命中；缺点路段
  （incomplete）不参与范围查询。
* 空间目录由后台补建（`GET /api/v1/index/status` 查进度）；目录**未就绪时
  范围查询返回 409**。补建期间入库/改线/删除照常可用，变更经 `index_dirty`
  队列复核，旧扫描不会覆盖新状态；就绪后每批次的原值、线形与目录在**同一
  事务**提交或回滚。
* `GET /api/v1/state` 在**一个读快照**里返回全部测点原值与已持久化线形。

组线与目录扫描是唯一可能较慢的步骤，它们都发生在写事务之外；提交只是很短
的 `BEGIN IMMEDIATE`。两个 API 进程共用同一个 SQLite 文件（WAL 模式），
写者互斥、读者不阻塞；补建进程间用数据库租约协调，中断后重启自动接续，
不会误报就绪。
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Background spatial-catalog backfill.  Both API processes run this loop;
    # a database lease makes sure only one of them works at a time, and all
    # progress is persisted so a restart resumes where it stopped.
    stop = threading.Event()
    worker = threading.Thread(
        target=indexstore.run_worker,
        args=(stop,),
        name="spatial-index-backfill",
        daemon=True,
    )
    worker.start()
    try:
        yield
    finally:
        stop.set()
        worker.join(timeout=5)


app = FastAPI(
    title="Municipal Road Geometry Service",
    description=DESCRIPTION,
    version="1.1.0",
    lifespan=lifespan,
)


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    with read_tx() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.execute("SELECT count(*) FROM points").fetchone()
    return HealthResponse(status="ok", db=settings.db_path, journal_mode=mode)


@app.post("/api/v1/batches", response_model=BatchResult, status_code=201)
def post_batch(
    batch: BatchIn,
    pause_ms: int = Query(
        default=0,
        ge=0,
        le=30_000,
        description="演示用：规划完成后、进入写事务前暂停指定毫秒；证明组线期间"
        "不持写锁，另一进程仍可入库。",
    ),
    fail: bool = Query(
        default=False, description="演示用：在全部写入之后、提交之前注入异常以验证整批回滚。"
    ),
):
    try:
        result, attempts = store.execute_batch(
            batch, pause_ms=pause_ms, inject_failure=fail
        )
    except store.ConcurrentConflict as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except store.DemoFailure as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except sqlite3.OperationalError as exc:
        raise HTTPException(status_code=503, detail=f"database busy: {exc}") from exc
    return result


@app.get("/api/v1/state", response_model=StateResponse)
def get_state() -> StateResponse:
    """全部测点原值与全部已持久化线形，来自**同一个读快照**。

    返回的每个坐标与每条线形都属于同一已提交状态，不会出现“新坐标 + 旧线形”。
    """
    with read_tx() as conn:
        generation = store.get_generation(conn)
        points = store.list_points(conn)
        roads = store.list_roads(conn)
    return StateResponse(generation=generation, points=points, roads=roads)


@app.get("/api/v1/points", response_model=PointsResponse)
def get_points() -> PointsResponse:
    with read_tx() as conn:
        points = store.list_points(conn)
    return PointsResponse(points=points, total=len(points))


@app.get("/api/v1/points/{point_id}", response_model=PointIn)
def get_point(point_id: str) -> PointIn:
    with read_tx() as conn:
        point = store.get_point(conn, point_id)
    if point is None:
        raise HTTPException(status_code=404, detail=f"point {point_id!r} not found")
    return point


@app.get("/api/v1/roads", response_model=RoadsResponse)
def get_roads() -> RoadsResponse:
    with read_tx() as conn:
        roads = store.list_roads(conn)
    return RoadsResponse(roads=roads, total=len(roads))


@app.get("/api/v1/roads/{road_id}", response_model=RoadOut)
def get_road(road_id: str) -> RoadOut:
    with read_tx() as conn:
        roads = store.list_roads(conn, road_id=road_id)
    if not roads:
        raise HTTPException(status_code=404, detail=f"road {road_id!r} not found")
    return roads[0]


# ---------------------------------------------------------------------------
# Spatial range query + catalog backfill
# ---------------------------------------------------------------------------


@app.get("/api/v1/spatial/roads", response_model=SpatialRoadsResponse)
def get_roads_in_window(
    min_lon: float = Query(ge=-180.0, le=180.0, description="窗口西边界经度（含）"),
    min_lat: float = Query(ge=-90.0, le=90.0, description="窗口南边界纬度（含）"),
    max_lon: float = Query(ge=-180.0, le=180.0, description="窗口东边界经度（含）"),
    max_lat: float = Query(ge=-90.0, le=90.0, description="窗口北边界纬度（含）"),
) -> SpatialRoadsResponse:
    """按地图视窗加载路段：返回与**闭矩形**相交的完整路段（GeoJSON + ID）。

    * 窗口为不跨 180° 经线的 WGS84 矩形（`min_lon ≤ max_lon`、`min_lat ≤ max_lat`），
      按经纬度平面判定相交。
    * 先用持久化的 RTree 空间目录筛候选，再对候选的**已持久化线形**做精确
      相交判定——不扫描全路网、不临时组线。
    * 道路穿过窗口但所有顶点都在窗外也命中；线形压到窗口边界（闭矩形）也命中。
    * 缺点路段（incomplete）不进入目录，不会被返回。
    * 空间目录未就绪（后台补建中）时返回 **409**，请先查看
      `GET /api/v1/index/status`。
    """
    if min_lon > max_lon:
        raise HTTPException(
            status_code=422,
            detail="min_lon must be <= max_lon (the window must not cross the "
            "180° meridian; query the two halves separately instead)",
        )
    if min_lat > max_lat:
        raise HTTPException(status_code=422, detail="min_lat must be <= max_lat")
    rect = (min_lon, min_lat, max_lon, max_lat)
    with read_tx() as conn:
        status = indexstore.get_status(conn)
        if not status["ready"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "spatial catalog is not ready; range queries are "
                    "rejected until the backfill completes",
                    "status": status["status"],
                    "scanned": status["scanned"],
                    "pending_changes": status["pending_changes"],
                    "total_roads": status["total_roads"],
                },
            )
        # one read snapshot: candidates and geometries are the same committed state
        candidates = indexstore.candidate_road_ids(conn, rect)
        roads = store.list_roads_by_ids(conn, candidates)
    hits = [
        r
        for r in roads
        if r.geometry is not None
        and linestring_intersects_rect(r.geometry["coordinates"], rect)
    ]
    return SpatialRoadsResponse(
        window=SpatialWindow(
            min_lon=min_lon, min_lat=min_lat, max_lon=max_lon, max_lat=max_lat
        ),
        roads=hits,
        total=len(hits),
    )


@app.get("/api/v1/index/status", response_model=IndexStatusResponse)
def get_index_status() -> IndexStatusResponse:
    """空间目录补建进度：状态、已扫描数、待复核变更数、持有租约的进程等。"""
    with read_tx() as conn:
        return IndexStatusResponse(**indexstore.get_status(conn))


@app.post("/api/v1/index/pause", response_model=IndexStatusResponse)
def pause_index(
    paused: bool = Query(description="true=暂停后台补建扫描；false=继续"),
) -> IndexStatusResponse:
    """演示/运维开关：暂停或恢复后台补建扫描。

    暂停期间入库、改线、删除照常工作：变更被记入 `index_dirty` 队列，
    恢复后由后台复核补齐；目录未就绪前范围查询仍返回 409。
    """
    indexstore.set_paused(paused)
    with read_tx() as conn:
        return IndexStatusResponse(**indexstore.get_status(conn))


@app.post("/api/v1/index/rebuild", response_model=IndexStatusResponse)
def rebuild_index(
    pause: bool = Query(
        default=False,
        description="演示用：重建前先暂停扫描，便于演示“扫描暂停时改线”。",
    ),
) -> IndexStatusResponse:
    """运维/演示：把空间目录重置为 building 并整体重建（不丢已入库数据）。

    重建期间范围查询返回 409；批次变更走 `index_dirty` 队列复核，
    旧扫描不会覆盖新状态。就绪前已持久化的目录行会被逐条复核。
    """
    indexstore.request_rebuild(pause)
    with read_tx() as conn:
        return IndexStatusResponse(**indexstore.get_status(conn))
