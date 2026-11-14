"""Pure lon/lat-planar geometry helpers (no SQL, no FastAPI).

The range query treats WGS84 lon/lat as planar coordinates, which is exactly
what the map viewport expects: the window is an axis-aligned rectangle that
does not cross the 180° meridian, and a road hits it when its persisted
LineString intersects the *closed* rectangle.

A road whose vertices all lie outside the window still hits when any of its
segments crosses the rectangle (e.g. a straight road passing through the
viewport) - vertex containment alone would miss those.
"""
from __future__ import annotations

from typing import Sequence

# (min_lon, min_lat, max_lon, max_lat)
Rect = tuple[float, float, float, float]

Coord = Sequence[float]  # [lon, lat]


def geometry_bbox(geometry: dict) -> Rect:
    """Bounding box of a persisted GeoJSON LineString."""
    coords = geometry["coordinates"]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    return (min(lons), min(lats), max(lons), max(lats))


def point_in_rect(lon: float, lat: float, rect: Rect) -> bool:
    """Closed-rectangle containment: points on the edge count as inside."""
    min_lon, min_lat, max_lon, max_lat = rect
    return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat


def _orient(a: Coord, b: Coord, c: Coord) -> float:
    """Cross product of AB x AC; sign gives the turn direction."""
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Coord, b: Coord, p: Coord) -> bool:
    """True when collinear ``p`` lies within the bounding box of ``ab``."""
    return (
        min(a[0], b[0]) <= p[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= p[1] <= max(a[1], b[1])
    )


def segments_intersect(p1: Coord, p2: Coord, p3: Coord, p4: Coord) -> bool:
    """Inclusive segment-segment intersection (touching/overlapping counts)."""
    d1 = _orient(p3, p4, p1)
    d2 = _orient(p3, p4, p2)
    d3 = _orient(p1, p2, p3)
    d4 = _orient(p1, p2, p4)
    if ((d1 > 0) != (d2 > 0) and d1 != 0 and d2 != 0) and (
        (d3 > 0) != (d4 > 0) and d3 != 0 and d4 != 0
    ):
        return True
    if d1 == 0 and _on_segment(p3, p4, p1):
        return True
    if d2 == 0 and _on_segment(p3, p4, p2):
        return True
    if d3 == 0 and _on_segment(p1, p2, p3):
        return True
    if d4 == 0 and _on_segment(p1, p2, p4):
        return True
    return False


def segment_intersects_rect(a: Coord, b: Coord, rect: Rect) -> bool:
    """True when segment ``ab`` touches or crosses the closed rectangle."""
    min_lon, min_lat, max_lon, max_lat = rect
    # cheap reject: segment bbox disjoint from rect
    if (
        max(a[0], b[0]) < min_lon
        or min(a[0], b[0]) > max_lon
        or max(a[1], b[1]) < min_lat
        or min(a[1], b[1]) > max_lat
    ):
        return False
    if point_in_rect(a[0], a[1], rect) or point_in_rect(b[0], b[1], rect):
        return True
    edges = (
        ((min_lon, min_lat), (max_lon, min_lat)),
        ((max_lon, min_lat), (max_lon, max_lat)),
        ((max_lon, max_lat), (min_lon, max_lat)),
        ((min_lon, max_lat), (min_lon, min_lat)),
    )
    return any(segments_intersect(a, b, e1, e2) for e1, e2 in edges)


def linestring_intersects_rect(coordinates: Sequence[Coord], rect: Rect) -> bool:
    """True when a LineString intersects the closed rectangle.

    Covers both cases: any vertex inside the window, and windows pierced by
    a segment whose endpoints are both outside.
    """
    for c in coordinates:
        if point_in_rect(c[0], c[1], rect):
            return True
    for i in range(len(coordinates) - 1):
        if segment_intersects_rect(coordinates[i], coordinates[i + 1], rect):
            return True
    return False
