"""Latitude/longitude polygon geometry with holes and antimeridian support.

Boundary rule (fixed, versioned and recorded with every fence and audit row):

* a point on the **outer ring boundary is inside** the fence;
* a point on a **hole boundary is outside** the fence (holes are closed
  regions subtracted from the outer ring);
* otherwise the point is inside iff the ray-casting test puts it inside the
  outer ring and outside every hole.

Rings crossing the ±180° antimeridian are handled by *unwrapping* ring
longitudes (consecutive jumps greater than 180° are shifted by ±360°); a query
point is tested at longitude ``lon``, ``lon + 360`` and ``lon - 360``.
"""
from __future__ import annotations

from typing import Sequence

# Bumped if the containment semantics ever change; persisted with each fence
# and written into audit details so historical decisions stay explainable.
BOUNDARY_RULE = "outer_included_holes_excluded_v1"

# Degrees. ~0.1 mm at the equator; exact vertex hits compare equal anyway.
_EPS = 1e-9

# A polygon coordinate is (latitude, longitude), both in degrees.
Coord = tuple[float, float]
Polygon = dict  # {"outer": list[Coord], "holes": list[list[Coord]]}


# ---------------------------------------------------------------------------
# Ring helpers
# ---------------------------------------------------------------------------

def unwrap_longitudes(longitudes: Sequence[float]) -> list[float]:
    """Shift successive longitudes so no edge jumps more than 180°.

    ``180`` and ``-180`` are the same meridian (a 360° jump becomes 0), while
    an edge from ``179`` to ``-179`` is a 2° crossing of the antimeridian.
    """
    unwrapped = [longitudes[0]]
    for lon in longitudes[1:]:
        delta = lon - unwrapped[-1]
        if delta > 180.0:
            delta -= 360.0
        elif delta < -180.0:
            delta += 360.0
        unwrapped.append(unwrapped[-1] + delta)
    return unwrapped


def ring_crosses_antimeridian(ring: Sequence[Coord]) -> bool:
    """True if any ring edge (including the closing edge) jumps over ±180°."""
    for i in range(len(ring)):
        a = ring[i][1]
        b = ring[i - 1][1]
        if abs(a - b) > 180.0:
            return True
    return False


def signed_area(ring: Sequence[Coord]) -> float:
    """Shoelace area on the unwrapped longitude plane (squared degrees)."""
    lats = [p[0] for p in ring]
    lons = unwrap_longitudes([p[1] for p in ring])
    total = 0.0
    for i in range(len(ring)):
        j = i - 1
        total += lons[j] * lats[i] - lons[i] * lats[j]
    return total / 2.0


def validate_polygon(polygon: Polygon) -> None:
    """Validate a user-supplied polygon, raising ``ValueError`` on problems."""
    outer = polygon.get("outer")
    if not outer:
        raise ValueError("polygon.outer must contain at least one ring")
    holes = polygon.get("holes") or []
    _validate_ring(outer, "outer")
    for index, hole in enumerate(holes):
        _validate_ring(hole, f"holes[{index}]")


def _validate_ring(ring: Sequence[Coord], label: str) -> None:
    if len(ring) < 4:
        raise ValueError(f"polygon.{label}: a ring needs at least 4 points (closed)")
    first, last = ring[0], ring[-1]
    if abs(first[0] - last[0]) > _EPS or abs(first[1] - last[1]) > _EPS:
        raise ValueError(
            f"polygon.{label}: ring is not closed, first point {first} must equal last {last}"
        )
    distinct = {(round(lat, 9), round(lon, 9)) for lat, lon in ring[:-1]}
    if len(distinct) < 3:
        raise ValueError(f"polygon.{label}: ring needs at least 3 distinct vertices")
    if abs(signed_area(ring)) <= _EPS:
        raise ValueError(f"polygon.{label}: ring is degenerate (zero area)")


# ---------------------------------------------------------------------------
# Point-in-polygon
# ---------------------------------------------------------------------------

def _on_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> bool:
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) > _EPS:
        return False
    return (
        min(ax, bx) - _EPS <= px <= max(ax, bx) + _EPS
        and min(ay, by) - _EPS <= py <= max(ay, by) + _EPS
    )


def _ray_cast_inside(lat: float, lon: float, lats: Sequence[float], lons: Sequence[float]) -> bool:
    """Standard ray-to-the-east casting. Caller must handle boundary points."""
    inside = False
    j = len(lats) - 1
    for i in range(len(lats)):
        if (lats[i] > lat) != (lats[j] > lat):
            cross_lon = lons[j] + (lat - lats[j]) * (lons[i] - lons[j]) / (
                lats[i] - lats[j]
            )
            if lon < cross_lon:
                inside = not inside
        j = i
    return inside


def _classify_ring(
    lat: float, lon: float, ring: Sequence[Coord]
) -> tuple[bool, bool]:
    """Return ``(on_boundary, inside)`` for one (unwrapped) ring.

    The query is tried at three longitude worlds so antimeridian-spanning
    rings match points expressed on either side of the ±180° cut.
    """
    lats = [p[0] for p in ring]
    lons = unwrap_longitudes([p[1] for p in ring])
    n = len(ring)
    for shift in (0.0, 360.0, -360.0):
        qx = lon + shift
        j = n - 1
        for i in range(n):
            if _on_segment(qx, lat, lons[j], lats[j], lons[i], lats[i]):
                return True, False
            j = i
        if _ray_cast_inside(lat, qx, lats, lons):
            return False, True
    return False, False


def polygon_contains(lat: float, lon: float, polygon: Polygon) -> bool:
    """Fixed-rule containment test (see module docstring / ``BOUNDARY_RULE``)."""
    outer_boundary, outer_inside = _classify_ring(lat, lon, polygon["outer"])
    if outer_boundary:
        return True
    for hole in polygon.get("holes") or []:
        hole_boundary, hole_inside = _classify_ring(lat, lon, hole)
        if hole_boundary:
            return False
        if hole_inside:
            return False
    return outer_inside
