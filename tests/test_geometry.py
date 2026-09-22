"""Unit tests for polygon containment, holes and antimeridian handling."""
from __future__ import annotations

import pytest

from app import geometry
from app.geometry import BOUNDARY_RULE, polygon_contains, unwrap_longitudes, validate_polygon

SQUARE = {
    "outer": [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)],
    "holes": [],
}
SQUARE_WITH_HOLE = {
    "outer": [(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0), (0.0, 0.0)],
    "holes": [
        [(3.0, 3.0), (3.0, 7.0), (7.0, 7.0), (7.0, 3.0), (3.0, 3.0)],
    ],
}
# Rectangle straddling the antimeridian: lon 179E .. 179W, lat 0..10.
ANTIMERIDIAN = {
    "outer": [(0.0, 179.0), (0.0, -179.0), (10.0, -179.0), (10.0, 179.0), (0.0, 179.0)],
    "holes": [],
}


def test_rule_is_versioned():
    assert BOUNDARY_RULE == "outer_included_holes_excluded_v1"


def test_basic_inside_and_outside():
    assert polygon_contains(5.0, 5.0, SQUARE) is True
    assert polygon_contains(-1.0, 5.0, SQUARE) is False
    assert polygon_contains(5.0, 11.0, SQUARE) is False
    assert polygon_contains(5.0, -0.001, SQUARE) is False


def test_outer_boundary_is_inside():
    # Vertex and edge midpoint of the outer ring are both inside (fixed rule).
    assert polygon_contains(0.0, 0.0, SQUARE) is True
    assert polygon_contains(0.0, 5.0, SQUARE) is True
    assert polygon_contains(5.0, 10.0, SQUARE) is True


def test_hole_excludes_interior_and_boundary():
    assert polygon_contains(8.0, 8.0, SQUARE_WITH_HOLE) is True
    assert polygon_contains(5.0, 5.0, SQUARE_WITH_HOLE) is False
    # Hole boundary is excluded (fixed rule, opposite of the outer ring).
    assert polygon_contains(3.0, 3.0, SQUARE_WITH_HOLE) is False
    assert polygon_contains(5.0, 7.0, SQUARE_WITH_HOLE) is False


def test_antimeridian_crossing_polygon():
    assert geometry.ring_crosses_antimeridian(ANTIMERIDIAN["outer"]) is True
    # 180/-180 is the same meridian running through the middle of the strip.
    assert polygon_contains(5.0, 180.0, ANTIMERIDIAN) is True
    assert polygon_contains(5.0, -180.0, ANTIMERIDIAN) is True
    # Both sides just inside the strip.
    assert polygon_contains(5.0, 179.5, ANTIMERIDIAN) is True
    assert polygon_contains(5.0, -179.5, ANTIMERIDIAN) is True
    # Outside.
    assert polygon_contains(5.0, 170.0, ANTIMERIDIAN) is False
    assert polygon_contains(5.0, -170.0, ANTIMERIDIAN) is False
    assert polygon_contains(-1.0, -180.0, ANTIMERIDIAN) is False


def test_unwrap_longitudes():
    assert unwrap_longitudes([179.0, -179.0, -179.0, 179.0]) == [179.0, 181.0, 181.0, 179.0]
    assert unwrap_longitudes([-179.0, 179.0]) == [-179.0, -181.0]
    # 180 -> -180 is not a jump: same meridian.
    assert unwrap_longitudes([180.0, -180.0]) == [180.0, 180.0]


@pytest.mark.parametrize(
    "polygon",
    [
        {"outer": [(0, 0), (0, 1), (1, 1)], "holes": []},  # not closed / too short
        {"outer": [(0, 0), (0, 10), (10, 10), (10, 0)], "holes": []},  # unclosed
        {"outer": [(0, 0), (1, 1), (2, 2), (0, 0)], "holes": []},  # degenerate
        {
            "outer": [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)],
            "holes": [[(1, 1), (1, 2), (1, 3), (1, 1)]],  # collinear hole
        },
    ],
)
def test_invalid_polygons_rejected(polygon):
    with pytest.raises(ValueError):
        validate_polygon(polygon)


def test_valid_polygon_accepted():
    validate_polygon(SQUARE)
    validate_polygon(SQUARE_WITH_HOLE)
    validate_polygon(ANTIMERIDIAN)
