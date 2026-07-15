"""Tests for the pure spatial helpers in src.tools.geo."""

import pytest

from src.tools.geo import (
    Budget,
    haversine_km,
    parse_within,
    point_in_polygon,
    polygon_contains_point,
)


class TestHaversine:
    def test_same_point_is_zero(self):
        assert haversine_km(49.45, 11.08, 49.45, 11.08) == pytest.approx(0.0, abs=1e-6)

    def test_known_distance_nuremberg_wiesloch(self):
        # Nuremberg (49.4521, 11.0767) -> Wiesloch (49.3010, 8.7056)
        # crow-flies is ~172 km (verified independently).
        d = haversine_km(49.4521, 11.0767, 49.3010, 8.7056)
        assert 160 < d < 185

    def test_symmetric(self):
        a = haversine_km(50.0, 10.0, 51.0, 11.0)
        b = haversine_km(51.0, 11.0, 50.0, 10.0)
        assert a == pytest.approx(b)

    def test_antipodal_finite(self):
        # Opposite points on Earth — distance should be ~half circumference.
        d = haversine_km(0.0, 0.0, 0.0, 180.0)
        assert 20000 < d < 22000


class TestPointInPolygon:
    def test_inside_square(self):
        # Unit square as (lat, lon) ring.
        square = [(0, 0), (0, 10), (10, 10), (10, 0)]
        assert point_in_polygon(5, 5, square) is True

    def test_outside_square(self):
        square = [(0, 0), (0, 10), (10, 10), (10, 0)]
        assert point_in_polygon(15, 5, square) is False

    def test_on_edge_boundary(self):
        # A point exactly on a horizontal edge — ray-casting is inclusive of
        # the lower-y boundary; we just assert it doesn't crash and returns
        # a bool.
        square = [(0, 0), (0, 10), (10, 10), (10, 0)]
        assert isinstance(point_in_polygon(0, 5, square), bool)

    def test_triangle(self):
        tri = [(0, 0), (0, 10), (10, 0)]
        assert point_in_polygon(2, 2, tri) is True
        assert point_in_polygon(8, 8, tri) is False

    def test_too_few_points(self):
        assert point_in_polygon(1, 1, [(0, 0), (1, 1)]) is False

    def test_polygon_with_hole(self):
        outer = [(0, 0), (0, 10), (10, 10), (10, 0)]
        hole = [(3, 3), (3, 7), (7, 7), (7, 3)]
        # Center is in the hole.
        assert polygon_contains_point(5, 5, [outer, hole]) is False
        # Just inside the outer ring but outside the hole.
        assert polygon_contains_point(1, 1, [outer, hole]) is True
        # Outside everything.
        assert polygon_contains_point(15, 15, [outer, hole]) is False

    def test_closed_ring_not_repeated(self):
        # Ring with explicit closing point should still work.
        square = [(0, 0), (0, 10), (10, 10), (10, 0), (0, 0)]
        assert point_in_polygon(5, 5, square) is True


class TestParseWithin:
    @pytest.mark.parametrize(
        "text,km",
        [
            ("50 km", 50.0),
            ("50km", 50.0),
            ("50 Kilometer", 50.0),
            ("50 kilometres", 50.0),
            ("0.5 km", 0.5),
        ],
    )
    def test_distance_km(self, text, km):
        b = parse_within(text)
        assert b.kind == "distance"
        assert b.km == pytest.approx(km)

    def test_miles_converted_to_km(self):
        b = parse_within("30 miles")
        assert b.kind == "distance"
        # 30 miles ~ 48.28 km
        assert b.km == pytest.approx(48.28, abs=0.5)

    @pytest.mark.parametrize(
        "text,seconds",
        [
            ("1 hour", 3600.0),
            ("1h", 3600.0),
            ("2 hours", 7200.0),
            ("45 minutes", 2700.0),
            ("45 min", 2700.0),
            ("30m", 1800.0),
        ],
    )
    def test_time(self, text, seconds):
        b = parse_within(text)
        assert b.kind == "time"
        assert b.time_seconds == pytest.approx(seconds)

    def test_human_distance(self):
        assert parse_within("50 km").human == "50 km"

    def test_human_time_minutes(self):
        assert parse_within("45 min").human == "45 minutes"

    def test_human_time_hours(self):
        assert parse_within("1 hour").human == "1 hour"
        assert parse_within("2 hour").human == "2 hours"

    def test_whitespace_tolerant(self):
        b = parse_within("   50   km   ")
        assert b.kind == "distance"
        assert b.km == pytest.approx(50.0)

    def test_case_insensitive(self):
        assert parse_within("50 KM").kind == "distance"
        assert parse_within("1 HOUR").kind == "time"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            parse_within("banana")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            parse_within("")

    def test_budget_dataclass_fields(self):
        b = parse_within("1 hour")
        assert isinstance(b, Budget)
        assert b.raw == "1 hour"
        assert b.value == 3600.0
