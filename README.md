# 市政道路线形加工服务

测绘队共享测点 → 路段线形（GeoJSON LineString）加工与入库服务，带**持久化空间目录**的
地图视窗范围查询。FastAPI + SQLite（WAL + RTree），两个 API 进程共用一个数据库文件，
Docker Compose 一键启动。

## 启动

```bash
docker compose up --build -d
```

* API A：<http://localhost:8001>  Swagger UI：<http://localhost:8001/docs>
* API B：<http://localhost:8002>  Swagger UI：<http://localhost:8002/docs>
* 两个进程共用卷 `gis-data:/data/gis.db`（命名卷，`docker compose down` 不会丢数据；要清空请 `docker compose down -v`）。
* `/docs` 里可直接试范围查询（`GET /api/v1/spatial/roads`）与补建进度（`GET /api/v1/index/status`）。

跑一遍样例（可选，会自动做在线演示，见文末）：

```bash
docker compose --profile demo run --rm demo
```

重启验证（数据与空间目录仍在）：

```bash
docker compose restart api-a api-b
curl -s http://localhost:8001/api/v1/state | head -c 400
curl -s http://localhost:8001/api/v1/index/status
```

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/batches` | 一个批次=一个事务：批量新增/更正测点与路段、批量删除；返回受影响路段的最终状态 |
| GET | `/api/v1/state` | **一个读快照**返回全部测点原值 + 全部已持久化线形（含 `generation`） |
| GET | `/api/v1/points` / `/api/v1/points/{id}` | 查询测点 |
| GET | `/api/v1/roads` / `/api/v1/roads/{id}` | 查询路段（只读已持久化的 `geometry`，不现场组线） |
| GET | `/api/v1/spatial/roads` | **范围查询**：不跨 180° 经线的 WGS84 闭矩形 → 相交的完整路段 GeoJSON + ID |
| GET | `/api/v1/index/status` | 空间目录补建进度（状态/已扫描/待复核/持约进程） |
| POST | `/api/v1/index/pause?paused=` | 演示/运维开关：暂停或恢复后台补建扫描 |
| POST | `/api/v1/index/rebuild?pause=` | 运维/演示：整体重建空间目录（可指定先暂停扫描） |
| GET | `/health` | 健康检查（含 WAL 模式） |

### 范围查询（地图视窗加载）

```
GET /api/v1/spatial/roads?min_lon=116.39&min_lat=39.90&max_lon=116.42&max_lat=39.92
```

* 窗口为**不跨 180° 经线**的 WGS84 矩形：`min_lon ≤ max_lon`、`min_lat ≤ max_lat`，
  经纬度平面判定；要查跨 180° 的区域请拆成两个窗口分别查询。
* 返回与**闭矩形**相交的**完整路段**（GeoJSON 不裁剪）及 ID，按 ID 升序；
  边界压线也算命中。
* **道路穿过窗口但顶点均在窗外也命中**：精确判定在线段级（线段与矩形相交），
  不是简单的"顶点在窗口内"。
* **缺点路段（incomplete）不进入目录**，不会被范围查询返回。
* 实现：先用持久化的 **SQLite RTree** 空间目录筛候选，再对候选的**已持久化线形**
  做精确相交判定——不扫描全路网、不临时组线；候选筛选与线形取自**同一个读快照**，
  即同一已提交状态。
* 空间目录**未就绪时返回 409**（响应体含当前进度），请先查 `/api/v1/index/status`。

### 空间目录与后台补建

* 目录 = `road_rtree`（RTree 虚表，存每条完整路段的包围盒）+ `road_index_map`
  （文本路段 ID ↔ RTree 整数 ID）。随库文件持久化，重启不丢。
* **就绪（ready）后**：每个批次的测点原值、线形与目录条目在**同一个写事务**里
  提交或回滚（批次响应里 `index_mode: "ready"`）。
* **历史数据后台补建（building）**：首次升级/重建时，后台线程按 ID 游标分块扫描
  全路网并写入目录；`/api/v1/index/status` 可查 `scanned`、`pending_changes`、
  `total_roads` 等进度。就绪前范围查询一律 409。
* **补建期间入库照常**：批次增/改/删（含同 ID 删后重建）只把路段 ID 记入
  `index_dirty` 队列（`index_mode: "building"`），后台按**当前已提交状态**复核补齐；
  扫描写回前会在写事务内逐条比对持久化线形，**旧扫描不会覆盖新状态、也不会漏记路段**。
* **扫描与相交计算不持写锁**：扫描在短读快照上进行，包围盒计算在事务外；
  写事务只做"校验 + 落条目"，毫秒级。
* **中断自动接续**：游标、队列、状态全部持久化；进程被杀或容器重启后，
  后台线程从断点继续，**未真正完成前绝不置 ready**。
* **双进程协调**：两个 API 进程都跑补建线程，用 `index_meta` 里的租约
  （`owner`/`lease_until`）保证同一时刻只有一个在扫描，另一个待机接管。

批次请求体（所有字段可选，按 `points → roads → delete_roads → delete_points` 顺序生效）：

```json
{
  "points":       [{"id": "PT-001", "lon": 116.397428, "lat": 39.909230}],
  "roads":        [{"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]}],
  "delete_points": ["PT-004"],
  "delete_roads":  ["RD-102"]
}
```

* 路段 `point_ids`：≥ 2 个、有序、批内不可重复；允许引用**尚不存在**的测点（该路段进入 `incomplete`）。
* 已存在 id 视为**更正**（upsert）：测点改坐标、路段改引用顺序/成员。
* 同一 id 不能在同一批次里既 upsert 又 delete（返回 422）。
* 演示用查询参数：`pause_ms`（组线暂停，**事务外**）、`fail=true`（写完后注入异常以演示回滚）。
  `/docs` 的 “Try it out” 可直接选中这两个参数演示。

响应里的路段：

```json
{
  "id": "RD-101",
  "point_ids": ["PT-001", "PT-002", "PT-003"],
  "status": "ok",                 // ok | incomplete
  "geometry": {"type": "LineString", "coordinates": [[116.397428, 39.909230], ...]},
  "missing_point_ids": [],        // incomplete 时按路段顺序列出缺失测点 ID
  "revision": 3,                  // 每受影响一次 +1
  "updated_at": "2026-11-13T07:42:30.316Z"
}
```

## 关键行为

1. **组线即持久化**：线形在提交事务内按 `point_ids` 顺序组装，写入 `roads.geometry`；
   查询只 SELECT 该列，绝不临时组线。
2. **缺点不输出残线**：任一引用测点不存在 → `geometry = null`、`status = incomplete`、
   `missing_point_ids` 列明缺失 ID（按路段顺序去重）。补点后**下一次批次**自动恢复线形，无需额外调用。
3. **影响面完整**：更正/删除测点、改变路段引用时，所有受影响路段（含引用该测点的全部路段）在
   **同一事务**里重算；未受影响的路段不重写（`revision` 不变）。
4. **整批原子**：批次中途任何异常（含写完后注入）整体回滚，`generation` 不前进，库保持原状。
5. **目录随批一致**：就绪后每个批次把测点原值、线形、RTree 目录条目放在**同一个写事务**里
   提交或回滚；范围查询的候选筛选与返回线形取自同一读快照，即同一已提交状态。
6. **重启保留**：SQLite 文件在命名卷里，`docker compose restart` / 重建容器后数据与空间目录仍在。

## 并发模型（两个进程共库）

* 每个请求一条短连接；写事务一律 `BEGIN IMMEDIATE` + `busy_timeout=30s`，两个进程的写入互斥但排队等待，不报 busy。
* **组线在事务之外**：先在一个 WAL 读快照上把批次规划完（含组线），再进入极短的写事务。
  因此 `pause_ms=2000` 的慢批次不会挡住另一进程的入库（演示第 5 步实测 < 1s 完成）。
* **提交前复核快照代号**（`store_meta.generation`）：规划期间若有别的进程提交过，
  本次规划整体作废、从新快照重算（响应里 `commit_attempts` 可见），
  绝不把基于旧坐标算出的线形写回库；并发新增 / 改引用的路段也在重算中被重新纳入，
  不会漏更新。
* **读一致性**：`/api/v1/state` 在一个延迟事务里完成全部读取，返回的测点原值与线形必定来自同一已提交状态。
* **补建不挡入库**：后台补建的扫描与包围盒计算都在写事务之外；写事务只做毫秒级的
  "校验 + 落条目"。范围查询的精确相交判定也发生在读快照上，不持写锁。
* WAL 下读者不阻塞写者、写者不阻塞读者；`synchronous=FULL` 保证已提交数据断电安全。

## 本地运行与测试

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
GIS_DB_PATH=/tmp/gis.db uvicorn app.main:app --port 8001

pytest            # 功能 + 双进程并发（测试会真的起两个 API 进程共库运行）
```

（无 docker 环境时也可直接验证整套并发行为：`tests/test_concurrency.py` 用
`tests/server_runner.py` 起两个独立进程，连接同一个 SQLite 文件。）

`tests/test_concurrency.py` 起两个真实 API 进程共库运行：跨进程一致性、暂停组线不持写锁、
并发改线无漏更新/旧坐标、`/state` 在持续写入下的快照一致性、空间目录跨进程共享与
"重建暂停时改线不丢不旧"；
`tests/test_retry.py` 用可控方式制造"规划后另一进程提交"，断言旧规划被丢弃并重算；
`tests/test_spatial.py` 覆盖闭矩形相交判定（含穿越窗口、压边界）、范围查询就绪门禁、
补建暂停/恢复/重启接续、旧扫描不覆盖新状态、目录随批次回滚。

## 演示样例（要点）

`examples/run_demo.py`（compose profile `demo`）对两个进程逐步演示：

1. 批量入库测点 + 3 条路段，线形已持久化；
2. 删除被引用测点 → `geometry=null`、`missing_point_ids` 列明；
3. 补回该点 → 线形自动恢复；
4. 改由 API B 更正共享测点 → 两条引用它的路段同批生效；
5. **可控暂停**：A 提交一个 `pause_ms=2000` 的批次，B 在暂停期间完成测点更正 + 新建路段
   （用时 < 1s，未被写锁阻塞）；A 提交时发现代号已变，整批重算后 `commit_attempts=2`，
   重算结果把 B 新建的 RD-105 一并纳入（`affected_road_ids` 含 RD-103/104/105），
   两条路段与 PT-006 原值一致；
6. `fail=true` 批次写完后抛错 → 整体回滚，`/state` 与失败前逐字段一致；
7. **范围查询**：视窗内命中完整路段；RD-CROSS 斜穿小视窗、两端点都在窗外也命中；
   删除测点使路段 incomplete 后立即被范围查询排除，补点后自动回到目录；
8. **扫描暂停时改线**：`POST /api/v1/index/rebuild?pause=true` 重建并暂停扫描 →
   范围查询 409 → B 在暂停期间改线 + 新建路段（`index_mode=building`，变更进入复核队列）
   → 恢复扫描 → 补建自动接续至就绪 → 范围查询按**新坐标**命中改过的线形与新建路段。

本地直接跑（不用 compose）：

```bash
uvicorn app.main:app --port 8001 &  uvicorn app.main:app --port 8002 &
python -m examples.run_demo --base-a http://localhost:8001 --base-b http://localhost:8002
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `GIS_DB_PATH` | `/data/gis.db` | SQLite 数据库文件路径 |
| `GIS_BUSY_TIMEOUT_S` | `30` | 写锁等待超时（秒） |
| `GIS_MAX_COMMIT_ATTEMPTS` | `12` | 乐观提交最大重试次数 |
| `GIS_BACKFILL_CHUNK` | `200` | 补建每步处理的路段数 |
| `GIS_BACKFILL_LEASE_S` | `5` | 补建租约时长（秒），宕机后另一进程接管 |
| `GIS_BACKFILL_STEP_INTERVAL_MS` | `50` | 补建步进间隔（毫秒），调大可观察 building 状态 |

## 说明

* 缺测点用 `point_id` 不加外键约束是刻意设计：悬空引用是"可表示状态"，才能稳定报告缺失 ID
  并在补点后自动恢复；路段删除时其引用行由 `ON DELETE CASCADE` 清理。
* 单机 SQLite 的写入吞吐上限是设计边界：本方案保证**正确性**，写入串行化 + 组线在事务外
  使得暂停/慢组线不会拖住其他入库。若未来需要多机横向扩展，应将 `store_meta.generation`
  复核替换为外部分布式锁/版本号。
