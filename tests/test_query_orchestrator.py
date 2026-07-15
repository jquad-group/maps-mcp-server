"""Tests for the query orchestrator — the find_within spatial filtering.

Uses fake Valhalla/Nominatim clients so we can assert the exact filter
behavior without network calls. The two key correctness properties:

1. A **distance** budget filters by road distance (or crow-flies).
2. A **time** budget uses isochrone polygon membership + matrix durations.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.services.nominatim_client import GeocodeResult
from src.services.poi_store import POI
from src.services.query_orchestrator import aresolve_location, find_within
from src.services.valhalla_client import MatrixCell, ValhallaError


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeValhalla:
    """Fake Valhalla that returns canned isochrone + matrix responses."""

    def __init__(self, matrix_rows: list[list[MatrixCell]] | None = None,
                 isochrone_rings: list[list[tuple[float, float]]] | None = None):
        self._matrix = matrix_rows or []
        # isochrone_rings: list of (lat, lon) rings defining the polygon.
        self._iso_rings = isochrone_rings or []
        self.matrix_calls: list[Any] = []
        self.isochrone_calls: list[dict[str, Any]] = []

    async def isochrone(self, lat: float, lon: float, *, minutes: int, costing: str = "auto"):
        self.isochrone_calls.append({"lat": lat, "lon": lon, "minutes": minutes})
        # Build a GeoJSON FeatureCollection from our canned rings.
        coords = [[[lon, lat] for lat, lon in ring] for ring in self._iso_rings]
        return {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {"type": "Polygon", "coordinates": coords},
                    "properties": {"time": minutes},
                }
            ],
        }

    async def sources_to_targets(self, sources, targets, *, costing: str = "auto"):
        self.matrix_calls.append({"sources": sources, "targets": targets})
        return self._matrix

    async def route(self, origin, destination, *, costing: str = "auto"):
        return {"trip": {"summary": {"length": 100.0, "time": 3600}}}

    async def health(self) -> bool:
        return True


class FakeNominatim:
    async def geocode(self, query: str, countrycodes: str = "de") -> GeocodeResult:
        return GeocodeResult(lat=49.45, lon=11.08, display_name=query)

    async def reverse_geocode(self, lat: float, lon: float) -> GeocodeResult:
        return GeocodeResult(lat=lat, lon=lon, display_name=f"{lat}, {lon}")

    async def health(self) -> bool:
        return True


def _poi(pid: str, lat: float, lon: float) -> POI:
    return POI(place_id=pid, name=f"Hotel {pid}", street="", zip="", city="",
               country="Deutschland", lat=lat, lon=lon)


def _center() -> GeocodeResult:
    return GeocodeResult(lat=49.45, lon=11.08, display_name="Nuremberg")


# ---------------------------------------------------------------------------
# aresolve_location
# ---------------------------------------------------------------------------


class TestAResolveLocation:
    def test_dict_passthrough(self):
        result = asyncio.run(
            aresolve_location({"lat": 10.0, "lon": 20.0}, nominatim=FakeNominatim())
        )
        assert result.lat == 10.0
        assert result.lon == 20.0

    def test_string_geocoded(self):
        result = asyncio.run(
            aresolve_location("Nuremberg", nominatim=FakeNominatim())
        )
        assert result.lat == pytest.approx(49.45)


# ---------------------------------------------------------------------------
# Distance budgets
# ---------------------------------------------------------------------------


class TestFindWithinDistance:
    def test_road_distance_filters(self):
        # 3 POIs, matrix returns [near, far, near-ish] from the center.
        pois = [_poi("a", 49.4, 11.0), _poi("b", 50.0, 11.0), _poi("c", 49.5, 11.1)]
        matrix = [
            [MatrixCell(distance_km=30.0, time_seconds=1800)],   # a
            [MatrixCell(distance_km=120.0, time_seconds=7200)],  # b
            [MatrixCell(distance_km=45.0, time_seconds=2400)],   # c
        ]
        val = FakeValhalla(matrix_rows=matrix)
        result = asyncio.run(
            find_within(_center(), "50 km", pois, valhalla=val, metric="road")
        )
        assert result["budget"]["kind"] == "distance"
        assert result["match_count"] == 2
        assert result["total_evaluated"] == 3
        names = [m["name"] for m in result["matches"]]
        assert "Hotel a" in names and "Hotel c" in names and "Hotel b" not in names

    def test_road_distance_sorted_nearest_first(self):
        pois = [_poi("a", 49.4, 11.0), _poi("c", 49.5, 11.1)]
        matrix = [
            [MatrixCell(distance_km=45.0, time_seconds=2400)],  # a
            [MatrixCell(distance_km=30.0, time_seconds=1800)],  # c
        ]
        val = FakeValhalla(matrix_rows=matrix)
        result = asyncio.run(
            find_within(_center(), "50 km", pois, valhalla=val)
        )
        assert [m["name"] for m in result["matches"]] == ["Hotel c", "Hotel a"]

    def test_crow_metric_uses_haversine_no_matrix(self):
        # Near: ~0.1 deg ~ 11 km. Far: ~0.5 deg ~ 55 km.
        pois = [_poi("near", 49.46, 11.08), _poi("far", 49.90, 11.08)]
        val = FakeValhalla()  # no matrix configured -> would error if called
        result = asyncio.run(
            find_within(_center(), "50 km", pois, valhalla=val, metric="crow")
        )
        assert result["metric"] == "crow"
        assert result["match_count"] == 1
        assert result["matches"][0]["name"] == "Hotel near"
        # No matrix call should have been made for crow distance.
        assert val.matrix_calls == []

    def test_empty_candidates_returns_empty(self):
        val = FakeValhalla()
        result = asyncio.run(find_within(_center(), "50 km", [], valhalla=val))
        assert result["matches"] == []
        assert result["total_evaluated"] == 0

    def test_matrix_chunking(self):
        # 6 POIs, max_matrix_size=2 -> 3 matrix calls.
        pois = [_poi(f"p{i}", 49.4, 11.0) for i in range(6)]
        # Each call returns one row per target; FakeValhalla returns the
        # SAME canned matrix for every call, so we size it for chunks of 2.
        chunk_matrix = [
            [MatrixCell(distance_km=10.0, time_seconds=600)],
            [MatrixCell(distance_km=20.0, time_seconds=1200)],
        ]
        val = FakeValhalla(matrix_rows=chunk_matrix)
        result = asyncio.run(
            find_within(_center(), "50 km", pois, valhalla=val, max_matrix_size=2)
        )
        assert len(val.matrix_calls) == 3
        assert result["match_count"] == 6  # all under 50km in this fake


# ---------------------------------------------------------------------------
# Time budgets
# ---------------------------------------------------------------------------


class TestFindWithinTime:
    def test_isochrone_filters_by_polygon_membership(self):
        # Build a small square polygon around (49.45, 11.08) ~0.2 deg wide.
        # Ring as (lat, lon) tuples; the orchestrator converts to GeoJSON.
        ring = [(49.35, 10.88), (49.35, 11.28), (49.55, 11.28), (49.55, 10.88)]
        pois = [
            _poi("inside1", 49.45, 11.10),  # inside polygon
            _poi("inside2", 49.40, 11.00),  # inside polygon
            _poi("outside", 49.80, 11.50),  # well outside
        ]
        # Matrix durations for the 2 inside POIs only.
        matrix = [
            [MatrixCell(distance_km=5.0, time_seconds=600)],   # inside1
            [MatrixCell(distance_km=15.0, time_seconds=1200)],  # inside2
        ]
        val = FakeValhalla(matrix_rows=matrix, isochrone_rings=[ring])
        result = asyncio.run(find_within(_center(), "1 hour", pois, valhalla=val))
        assert result["budget"]["kind"] == "time"
        # One isochrone call.
        assert len(val.isochrone_calls) == 1
        assert val.isochrone_calls[0]["minutes"] == 60
        assert result["match_count"] == 2
        names = {m["name"] for m in result["matches"]}
        assert names == {"Hotel inside1", "Hotel inside2"}
        # Each match reports a duration.
        for m in result["matches"]:
            assert "time_seconds" in m
            assert m["inside_isochrone"] is True

    def test_isochrone_no_matches_returns_empty(self):
        # Polygon far away from all POIs.
        ring = [(40.0, 5.0), (40.0, 6.0), (41.0, 6.0), (41.0, 5.0)]
        pois = [_poi("a", 49.45, 11.08)]
        val = FakeValhalla(matrix_rows=[], isochrone_rings=[ring])
        result = asyncio.run(find_within(_center(), "1 hour", pois, valhalla=val))
        assert result["match_count"] == 0
        # Matrix should NOT have been called (no inside POIs).
        assert val.matrix_calls == []

    def test_time_budget_uses_minutes_rounding(self):
        # "45 min" -> minutes=45
        ring = [(49.0, 10.0), (49.0, 12.0), (50.0, 12.0), (50.0, 10.0)]
        pois = [_poi("a", 49.45, 11.08)]
        matrix = [[MatrixCell(distance_km=5.0, time_seconds=600)]]
        val = FakeValhalla(matrix_rows=matrix, isochrone_rings=[ring])
        asyncio.run(find_within(_center(), "45 min", pois, valhalla=val))
        assert val.isochrone_calls[0]["minutes"] == 45

    def test_time_matches_sorted_by_duration(self):
        ring = [(49.0, 10.0), (49.0, 12.0), (50.0, 12.0), (50.0, 10.0)]
        pois = [_poi("slow", 49.45, 11.50), _poi("fast", 49.45, 11.10)]
        # Matrix order follows the inside-POI order passed in.
        matrix = [
            [MatrixCell(distance_km=20.0, time_seconds=2400)],  # slow
            [MatrixCell(distance_km=5.0, time_seconds=600)],    # fast
        ]
        val = FakeValhalla(matrix_rows=matrix, isochrone_rings=[ring])
        result = asyncio.run(find_within(_center(), "1 hour", pois, valhalla=val))
        assert [m["name"] for m in result["matches"]] == ["Hotel fast", "Hotel slow"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestBudgetParsingErrors:
    def test_invalid_budget_raises_value_error(self):
        val = FakeValhalla()
        with pytest.raises(ValueError):
            asyncio.run(find_within(_center(), "banana", [], valhalla=val))
