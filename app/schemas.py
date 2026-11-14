"""Pydantic contract for the public API."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_ABS_LON = 180.0
MAX_ABS_LAT = 85.0511287798066


class PointIn(BaseModel):
    """共享测点：ID + WGS84 经纬度。"""

    model_config = {
        "json_schema_extra": {
            "examples": [{"id": "PT-001", "lon": 116.397428, "lat": 39.909230}]
        }
    }

    id: str = Field(min_length=1, max_length=64, description="测点 ID（全局唯一，已存在即更正）")
    lon: float = Field(description="WGS84 经度")
    lat: float = Field(description="WGS84 纬度")

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must not be blank")
        return v

    @field_validator("lon")
    @classmethod
    def _lon(cls, v: float) -> float:
        if not (-MAX_ABS_LON <= v <= MAX_ABS_LON):
            raise ValueError("lon out of WGS84 range")
        return v

    @field_validator("lat")
    @classmethod
    def _lat(cls, v: float) -> float:
        if not (-MAX_ABS_LAT <= v <= MAX_ABS_LAT):
            raise ValueError("lat out of WGS84 range")
        return v


class RoadIn(BaseModel):
    """路段：ID + 至少两个有序测点 ID。"""

    model_config = {
        "json_schema_extra": {
            "examples": [{"id": "RD-101", "point_ids": ["PT-001", "PT-002", "PT-003"]}]
        }
    }

    id: str = Field(min_length=1, max_length=64, description="路段 ID（已存在即更正引用）")
    point_ids: list[str] = Field(
        min_length=2,
        description="有序测点 ID，至少两个且互不重复；可引用尚不存在的测点（路段将标记为 incomplete）",
    )

    @field_validator("id")
    @classmethod
    def _clean_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must not be blank")
        return v

    @field_validator("point_ids")
    @classmethod
    def _clean_points(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v]
        if any(not p for p in cleaned):
            raise ValueError("point ids must not be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("point ids must be unique within a road")
        return cleaned


class BatchIn(BaseModel):
    """一个批次 = 一个事务。

    列表均为 upsert：id 已存在即更正，不存在即新增；`delete_*` 在同一事务内
    随后生效。执行顺序：测点 upsert → 路段 upsert → 路段删除 → 测点删除。
    所有受影响路段的线形与缺项在同一事务内整批重算；任一步失败整体回滚。
    """

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "summary": "新增/更正测点与路段",
                    "value": {
                        "points": [
                            {"id": "PT-001", "lon": 116.397428, "lat": 39.90923},
                            {"id": "PT-002", "lon": 116.407428, "lat": 39.913},
                            {"id": "PT-003", "lon": 116.417428, "lat": 39.918},
                        ],
                        "roads": [
                            {
                                "id": "RD-101",
                                "point_ids": ["PT-001", "PT-002", "PT-003"],
                            }
                        ],
                    },
                },
                {
                    "summary": "批量删除路段与测点",
                    "value": {"delete_roads": ["RD-102"], "delete_points": ["PT-004"]},
                },
            ]
        }
    }

    points: list[PointIn] = Field(default_factory=list, description="新增或更正测点")
    roads: list[RoadIn] = Field(default_factory=list, description="新增或更正路段（引用变更即改线）")
    delete_points: list[str] = Field(default_factory=list, description="删除测点（引用它的路段转为 incomplete 或随之恢复）")
    delete_roads: list[str] = Field(default_factory=list, description="删除路段及其引用")

    @field_validator("delete_points", "delete_roads")
    @classmethod
    def _no_dupes(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate id in delete list")
        return v

    @model_validator(mode="after")
    def _disjoint(self) -> "BatchIn":
        pt_upserts = [p.id for p in self.points]
        if len(set(pt_upserts)) != len(pt_upserts):
            raise ValueError("duplicate point id in points list")
        rd_upserts = [r.id for r in self.roads]
        if len(set(rd_upserts)) != len(rd_upserts):
            raise ValueError("duplicate road id in roads list")
        if set(pt_upserts) & set(self.delete_points):
            raise ValueError("a point id cannot be upserted and deleted in one batch")
        if set(rd_upserts) & set(self.delete_roads):
            raise ValueError("a road id cannot be upserted and deleted in one batch")
        return self


class BatchResult(BaseModel):
    points_upserted: int = Field(description="本批次实际写入（新增或坐标变化）的测点数")
    roads_upserted: int = Field(description="本批次新建的路段数")
    roads_updated: int = Field(description="本批次重算并更新线形/缺项/引用的已有路段数")
    points_deleted: int = Field(description="实际删除的测点数")
    roads_deleted: int = Field(description="实际删除的路段数")
    affected_road_ids: list[str] = Field(description="本批次影响到的全部路段 ID（均已持久化到最终状态）")
    roads: list["RoadOut"] = Field(description="受影响路段的最终状态，按 ID 升序")
    commit_attempts: int = Field(default=1, description="提交尝试次数；>1 表示规划期间检测到并发提交并已整体重算")
    index_mode: Literal["ready", "building"] = Field(
        description="空间目录模式：ready=本批已同步更新 RTree 目录；building=变更已记入补建队列，由后台补建复核"
    )


class RoadOut(BaseModel):
    id: str = Field(description="路段 ID")
    point_ids: list[str] = Field(description="持久化的有序测点引用")
    status: Literal["ok", "incomplete"] = Field(description="ok=线形完整；incomplete=缺测点，无残线")
    geometry: dict | None = Field(description="持久化的 GeoJSON LineString；incomplete 时为 null")
    missing_point_ids: list[str] = Field(description="缺失的测点 ID（按路段顺序去重）；补齐后自动恢复")
    revision: int = Field(description="版本号：每次该路段受影响 +1")
    updated_at: str = Field(description="最近持久化时间（UTC, ISO-8601）")


class PointsResponse(BaseModel):
    points: list[PointIn]
    total: int


class RoadsResponse(BaseModel):
    roads: list[RoadOut]
    total: int


class StateResponse(BaseModel):
    """Points and persisted geometry read from one committed snapshot."""

    generation: int
    points: list[PointIn]
    roads: list[RoadOut]


class SpatialWindow(BaseModel):
    """不跨 180° 经线的 WGS84 闭矩形查询窗口。"""

    min_lon: float = Field(description="窗口西边界经度（含）")
    min_lat: float = Field(description="窗口南边界纬度（含）")
    max_lon: float = Field(description="窗口东边界经度（含）")
    max_lat: float = Field(description="窗口北边界纬度（含）")


class SpatialRoadsResponse(BaseModel):
    """与闭矩形相交的完整路段（GeoJSON 不裁剪），来自同一已提交快照。"""

    window: SpatialWindow
    roads: list[RoadOut] = Field(description="与窗口相交的完整路段（含完整线形，按 ID 升序）")
    total: int


class IndexStatusResponse(BaseModel):
    """空间目录补建进度。"""

    status: Literal["building", "ready"] = Field(
        description="building=后台补建中（范围查询被拒绝）；ready=目录完整可用"
    )
    ready: bool
    paused: bool = Field(description="补建是否被演示/运维开关暂停")
    scanned: int = Field(description="游标扫描已处理的路段数")
    pending_changes: int = Field(description="补建期间发生、待后台复核的路段变更数")
    total_roads: int = Field(description="当前路段总数（实时）")
    scan_cursor: str = Field(description="扫描游标（已处理到的路段 ID）")
    owner: str | None = Field(description="当前持有补建租约的进程（hostname:pid）")
    started_at: str | None
    updated_at: str | None
    ready_at: str | None


class HealthResponse(BaseModel):
    status: Literal["ok"]
    db: str
    journal_mode: str


BatchResult.model_rebuild()
