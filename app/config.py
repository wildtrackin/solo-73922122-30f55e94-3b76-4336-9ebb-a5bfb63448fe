from __future__ import annotations

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


@dataclass
class Settings:
    db_path: str = field(
        default_factory=lambda: os.environ.get("GIS_DB_PATH", "/data/gis.db")
    )
    busy_timeout_s: float = field(
        default_factory=lambda: _float("GIS_BUSY_TIMEOUT_S", 30.0)
    )
    max_commit_attempts: int = field(
        default_factory=lambda: _int("GIS_MAX_COMMIT_ATTEMPTS", 12)
    )
    backfill_chunk: int = field(
        default_factory=lambda: _int("GIS_BACKFILL_CHUNK", 200)
    )
    backfill_lease_s: float = field(
        default_factory=lambda: _float("GIS_BACKFILL_LEASE_S", 5.0)
    )
    backfill_step_interval_ms: int = field(
        default_factory=lambda: _int("GIS_BACKFILL_STEP_INTERVAL_MS", 50)
    )

settings = Settings()
