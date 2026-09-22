"""Longitude/latitude polygon geometry with antimeridian (±180°) support.

Boundary rule (FIXED for the whole service)
-------------------------------------------
A point lying exactly on a polygon edge or vertex (within 1e-9°) counts as
**inside** — ``boundary_inclusive``. The rule is not configurable; it is
stamped on every geofence row and recorded in the geofence audit trail so
alert decisions can always be explained after the fact.

Antimeridian handling
---------------------
Rings may cross the ±180° meridian. Before the point-in-polygon test each
ring is *unwrapped* into a continuous longitude frame (consecutive vertices
kept within 180° of each other by shifting with multiples of 360°), and the
test point is shifted into the same frame. A fence drawn across the Pacific
therefore behaves like an ordinary polygon, with no special casing at the
call site.
"""
from __future__ import annotations

import math

BOUNDARY_RULE = "boundary_inclusive"
BOUNDARY_RULE_DESCRIPTION = (
    "boundary_inclusive: points exactly on a polygon edge or vertex "
    "(within 1e-9 degrees) count as inside; holes exclude their interior "
    "but their edges count as inside as well"
)

_BOUNDARY_EPS_DEG = 1e-9
_MAX_LON_SPAN_DEG = 360.0

Position = tuple[float, float]  # (longitude, latitude)


def normalize_ring(points) -> list[Position]:
    """Coerce to float ``(lon, lat)`` pairs and drop a duplicated closing vertex."""
    ring = [(float(lon), float(lat)) for lon, lat in points]
    if len(ring) > 1 and ring[0] == ring[-1]:
        ring = ring[:-1]
    return ring


def _unwrap_ring(ring: list[Position]) -> list[Position]:
    """Make longitudes continuous along the ring (handles ±180° crossings)."""
    unwrapped = [ring[0]]
    for lon, lat in ring[1:]:
        prev = unwrapped[-1][0]
        adjusted = lon
        while adjusted - prev > 180.0:
            adjusted -= 360.0
        while adjusted - prev <= -180.0:
            adjusted += 360.0
        unwrapped.append((adjusted, lat))
    return unwrapped


def _normalize_lon(lon: float, ring_lons: list[float]) -> float:
    """Shift ``lon`` by multiples of 360° into the ring's longitude frame."""
    center = (min(ring_lons) + max(ring_lons)) / 2.0
    adjusted = lon
    while adjusted < center - 180.0:
        adjusted += 360.0
    while adjusted > center + 180.0:
        adjusted -= 360.0
    return adjusted


def _on_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> bool:
    """True if (px, py) lies on segment (a, b) within the boundary tolerance."""
    dx, dy = bx - ax, by - ay
    length = math.hypot(dx, dy)
    if length == 0.0:  # degenerate edge: treat as a vertex coincidence check
        return math.hypot(px - ax, py - ay) <= _BOUNDARY_EPS_DEG
    cross = dx * (py - ay) - dy * (px - ax)
    if abs(cross) / length > _BOUNDARY_EPS_DEG:
        return False
    dot = (px - ax) * dx + (py - ay) * dy
    return -_BOUNDARY_EPS_DEG <= dot <= length * length + _BOUNDARY_EPS_DEG


def _ring_boundary(ring: list[Position], lon: float, lat: float) -> bool:
    n = len(ring)
    return any(
        _on_segment(lon, lat, ring[i][0], ring[i][1], ring[(i + 1) % n][0], ring[(i + 1) % n][1])
        for i in range(n)
    )


def _ring_interior(ring: list[Position], lon: float, lat: float) -> bool:
    """Even-odd ray casting towards increasing longitude.

    Boundary cases are resolved by the caller's ``_ring_boundary`` pre-check,
    so the half-open ``(y1 > lat) != (y2 > lat)`` convention is deterministic.
    """
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > lat) != (y2 > lat):
            x_cross = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)
            if lon < x_cross:
                inside = not inside
    return inside


class Polygon:
    """Outer ring plus optional holes, each in an unwrapped-longitude frame."""

    def __init__(self, outer: list[Position], holes=()) -> None:
        if len(outer) < 3:
            raise ValueError("outer ring must have at least 3 distinct vertices")
        self.outer = _unwrap_ring(outer)
        self._check_span(self.outer, "outer")
        self._outer_lons = [lon for lon, _ in self.outer]
        self.holes: list[list[Position]] = []
        self._hole_lons: list[list[float]] = []
        for hole in holes:
            if len(hole) < 3:
                raise ValueError("each hole must have at least 3 distinct vertices")
            unwrapped = _unwrap_ring(hole)
            self._check_span(unwrapped, "hole")
            self.holes.append(unwrapped)
            self._hole_lons.append([lon for lon, _ in unwrapped])

    @staticmethod
    def _check_span(ring: list[Position], label: str) -> None:
        lons = [lon for lon, _ in ring]
        if max(lons) - min(lons) > _MAX_LON_SPAN_DEG:
            raise ValueError(f"{label} ring spans more than 360 degrees of longitude")

    def contains(self, lon: float, lat: float) -> bool:
        """Fixed boundary rule: exactly-on-edge points count as inside."""
        rings = (self.outer, *self.holes)
        ring_lons = (self._outer_lons, *self._hole_lons)
        for ring, lons in zip(rings, ring_lons):
            if _ring_boundary(ring, _normalize_lon(lon, lons), lat):
                return True
        if not _ring_interior(self.outer, _normalize_lon(lon, self._outer_lons), lat):
            return False
        for ring, lons in zip(self.holes, self._hole_lons):
            if _ring_interior(ring, _normalize_lon(lon, lons), lat):
                return False
        return True
