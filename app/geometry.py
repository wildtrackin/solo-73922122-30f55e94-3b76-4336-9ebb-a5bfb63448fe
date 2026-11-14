"""Pure segment-assembly helpers (no FastAPI, no SQL).

Rules
-----
* A road is assembled only when *every* referenced point exists, in the order
  declared; the result is a GeoJSON LineString.
* If one or more referenced points are missing, no partial line is emitted at
  all and the missing ids are returned in road order.  Re-supplying the
  points restores assembly automatically on the next batch.
"""
from __future__ import annotations

from typing import Mapping, Sequence


def missing_point_ids(point_ids: Sequence[str], known: set[str]) -> list[str]:
    """Referenced ids that do not exist, in road order, de-duplicated."""
    missing: list[str] = []
    seen: set[str] = set()
    for pid in point_ids:
        if pid not in known and pid not in seen:
            seen.add(pid)
            missing.append(pid)
    return missing


def assemble(
    point_ids: Sequence[str], coords: Mapping[str, tuple[float, float]]
) -> tuple[dict | None, list[str]]:
    """Return ``(geojson_or_None, missing_ids)``.

    ``coords`` maps point id -> (lon, lat) in WGS84.
    """
    missing = missing_point_ids(point_ids, set(coords))
    if missing:
        return None, missing
    return (
        {
            "type": "LineString",
            "coordinates": [[coords[pid][0], coords[pid][1]] for pid in point_ids],
        },
        [],
    )
